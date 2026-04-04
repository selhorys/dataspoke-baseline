"""Integration tests for the ingestion workflow orchestration layer.

Separate from test_ingestion_service.py (which tests config CRUD).
This file focuses on:
- POST .../method/run endpoint (direct pipeline execution)
- POST /internal/activities/ingestion/list-periodic
- POST /internal/activities/ingestion/sync-periodic-flows
- Concurrency guard (Redis SET NX)

Test-specific data extensions (created and cleaned up within each test):
- Transient ingestion_configs rows for Imazon catalog datasets and
  synthetic test URNs.
- Transient dataspoke.events rows from actual ingestion runs.
- Dynamically generated Kestra flows (ingestion-periodic-*).

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Kestra port-forwarded to localhost:9205
- Redis port-forwarded to localhost:9202
- Dummy data ingested via conftest.py Python utilities (catalog schema)
"""

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from src.workflows.kestra.client import KestraClient
from src.workflows.ingestion import schedule_to_flow_id
from tests.integration.conftest import _auth_headers
from tests.integration.api_wired.spot.conftest import (
    EXAMPLE_KAFKA_IDENTIFIER,
    EXAMPLE_KAFKA_LOCATOR,
    EXAMPLE_PG_AUTH,
    EXAMPLE_PG_IDENTIFIER,
    EXAMPLE_PG_LOCATOR,
    delete_ingestion_config_db,
    delete_ingestion_events_db,
    delete_kestra_flow,
    make_ingestion_urn,
)

# Triggers module_dummy_data fixture to reset and re-ingest the catalog schema
# in both PostgreSQL and DataHub before this module's tests run.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])
# Ensure Kafka topics have seed messages for schema inference
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])

# Canonical Imazon dataset URNs registered by the dummy data fixture
_CATALOG_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.catalog.title_master,DEV)"
)
_EDITIONS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.catalog.editions,DEV)"
)
_GENRE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.catalog.genre_hierarchy,DEV)"
)
_KAFKA_ORDERS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,"
    "example_kafka.imazon.orders.events,DEV)"
)


def _urn(suffix: str) -> str:
    return make_ingestion_urn(suffix)


@pytest_asyncio.fixture
async def kestra_client():
    """Function-scoped Kestra client (avoids event-loop mismatch with module-scoped fixture)."""
    client = KestraClient(
        base_url=os.environ.get("DATASPOKE_KESTRA_URL", "http://localhost:9205"),
        namespace=os.environ.get("DATASPOKE_KESTRA_NAMESPACE", "dataspoke"),
        username=os.environ.get("DATASPOKE_KESTRA_USER", ""),
        password=os.environ.get("DATASPOKE_KESTRA_PASSWORD", ""),
    )
    yield client
    await client.close()


# ── Test cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_ingestion_via_public_api(
    http_client, async_session: AsyncSession, datahub_client
):
    """POST run on a configured dataset executes the pipeline, records events,
    and emits schema metadata to DataHub.

    Setup: PUT ingestion config for title_master (source_type=POSTGRESQL).
    Action: POST .../method/run with dry_run=false.
    Assertions: 200, run_id present, status == "success",
                GET events returns total_count >= 1,
                DataHub has SchemaMetadataClass with fields.
    Cleanup: DELETE config + events.
    """
    from datahub.metadata.schema_classes import DatasetPropertiesClass, SchemaMetadataClass

    dataset_urn = _CATALOG_URN
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200, f"Run failed: {resp.text}"
        body = resp.json()
        assert "run_id" in body
        assert body["status"] == "success", f"Ingestion failed: {body.get('detail')}"
        assert body["detail"]["entities_ingested"] >= 1

        # Check side-effect events
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        assert events_body["total_count"] >= 2
        event_types = [e["event_type"] for e in events_body["events"]]
        assert "INGESTION.CONFIG_CREATE" in event_types
        assert "INGESTION.COMPLETE" in event_types

        # Verify metadata landed in DataHub
        schema = await datahub_client.get_aspect(dataset_urn, SchemaMetadataClass)
        assert schema is not None, "SchemaMetadataClass not found in DataHub after ingestion"
        assert len(schema.fields) > 0, "No schema fields emitted to DataHub"

        props = await datahub_client.get_aspect(dataset_urn, DatasetPropertiesClass)
        assert props is not None, "DatasetPropertiesClass not found in DataHub after ingestion"
        assert "dataspoke-ingestion" in (props.customProperties or {}).get("source", "")

    finally:
        await delete_ingestion_events_db(async_session, dataset_urn)
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_ingestion_dry_run(
    http_client, async_session: AsyncSession
):
    """POST run with dry_run=true succeeds and records a dry_run event.

    Setup: PUT ingestion config for title_master.
    Action: POST .../method/run with dry_run=true.
    Assertions: 200, status == "success", event detail contains "dry_run": true.
    Cleanup: DELETE config + events.
    """
    dataset_urn = _CATALOG_URN
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
            headers=headers,
            json={"dry_run": True},
        )
        assert resp.status_code == 200, f"Dry run failed: {resp.text}"
        body = resp.json()
        assert body["status"] == "success", f"Ingestion failed: {body.get('detail')}"
        assert body["detail"]["dry_run"] is True

        # Check side-effect events
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        assert events_body["total_count"] >= 2
        event_types = [e["event_type"] for e in events_body["events"]]
        assert "INGESTION.CONFIG_CREATE" in event_types
        assert "INGESTION.COMPLETE" in event_types

    finally:
        await delete_ingestion_events_db(async_session, dataset_urn)
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_periodic_datasets(
    http_client, async_session: AsyncSession
):
    """POST ingestion/list-periodic returns only URNs matching the requested schedule.

    Setup: PUT 4 configs (A/B: periodic, schedule="0 2 * * *";
           C: periodic, schedule="0 6 * * *"; D: periodic=false).
    Action: POST /internal/activities/ingestion/list-periodic {"schedule": "0 2 * * *"}.
    Assertions: Result contains A and B; does not contain C or D.
    Cleanup: DELETE all test configs.
    """
    urn_a = _urn("periodic_a")
    urn_b = _urn("periodic_b")
    urn_c = _urn("periodic_c")
    urn_d = _urn("periodic_d")
    headers = _auth_headers()

    try:
        # A: periodic, schedule="0 2 * * *"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_a}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_a,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_cron": "0 2 * * *",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config A failed: {resp.text}"

        # B: periodic, schedule="0 2 * * *"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_b}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_b,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_cron": "0 2 * * *",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config B failed: {resp.text}"

        # C: periodic, schedule="0 6 * * *" (different schedule)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_c}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_c,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_cron": "0 6 * * *",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config C failed: {resp.text}"

        # D: periodic=false (not periodic)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_d}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_d,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config D failed: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/ingestion/list-periodic",
            json={"schedule_cron": "0 2 * * *"},
        )
        assert resp.status_code == 200, f"ingestion/list-periodic failed: {resp.text}"
        result = resp.json()

        assert urn_a in result, f"Expected {urn_a} in result: {result}"
        assert urn_b in result, f"Expected {urn_b} in result: {result}"
        assert urn_c not in result, f"Did not expect {urn_c} in result: {result}"
        assert urn_d not in result, f"Did not expect {urn_d} in result: {result}"

        # Check side-effect events — each config PUT should emit CONFIG_CREATE
        for urn in (urn_a, urn_b, urn_c, urn_d):
            resp = await http_client.get(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/event",
                headers=headers,
            )
            assert resp.status_code == 200
            event_types = [e["event_type"] for e in resp.json()["events"]]
            assert "INGESTION.CONFIG_CREATE" in event_types, (
                f"Expected CONFIG_CREATE event for {urn}, got {event_types}"
            )

    finally:
        for urn in (urn_a, urn_b, urn_c, urn_d):
            await delete_ingestion_events_db(async_session, urn)
            await delete_ingestion_config_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_creates_flows_per_schedule(
    http_client, async_session: AsyncSession, kestra_client
):
    """Sync endpoint generates one Kestra flow per unique schedule.

    Setup: 3 real catalog datasets — title_master + editions share "0 2 * * *",
           genre_hierarchy gets "0 6 * * *".
    Action: POST /internal/activities/ingestion/sync-periodic-flows.
    Assertions: Two flows registered in Kestra (one per schedule),
                both retrievable via kestra_client.get_flow().
    Cleanup: Delete generated flows + test configs.
    """
    flow_id_02 = schedule_to_flow_id("0 2 * * *")
    flow_id_06 = schedule_to_flow_id("0 6 * * *")
    headers = _auth_headers()

    try:
        # title_master + editions: schedule="0 2 * * *"
        for urn in (_CATALOG_URN, _EDITIONS_URN):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "source_type": "POSTGRESQL",
                    "locator": EXAMPLE_PG_LOCATOR,
                    "identifier": EXAMPLE_PG_IDENTIFIER,
                    "auth": EXAMPLE_PG_AUTH,
                    "is_active": True,
                    "schedule_cron": "0 2 * * *",
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # genre_hierarchy: schedule="0 6 * * *"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{_GENRE_URN}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": _GENRE_URN,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_cron": "0 6 * * *",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/ingestion/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"sync failed: {resp.text}"
        body = resp.json()
        assert flow_id_02 in body.get("created", []), f"Expected {flow_id_02} in created: {body}"
        assert flow_id_06 in body.get("created", []), f"Expected {flow_id_06} in created: {body}"

        # Verify Kestra has both flows
        flow_02 = await kestra_client.get_flow(flow_id_02)
        assert flow_02 is not None, f"Flow {flow_id_02} not found in Kestra"
        flow_06 = await kestra_client.get_flow(flow_id_06)
        assert flow_06 is not None, f"Flow {flow_id_06} not found in Kestra"

        # Sanity: trigger both flows and verify full round-trip
        exec_02 = await kestra_client.trigger_and_wait(
            flow_id_02, timeout_seconds=120,
        )
        assert exec_02.status.value == "SUCCESS", (
            f"Flow {flow_id_02} execution failed: {exec_02}"
        )

        exec_06 = await kestra_client.trigger_and_wait(
            flow_id_06, timeout_seconds=120,
        )
        assert exec_06.status.value == "SUCCESS", (
            f"Flow {flow_id_06} execution failed: {exec_06}"
        )

        # Check side-effect events — config creation + ingestion runs
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            resp = await http_client.get(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/event",
                headers=headers,
            )
            assert resp.status_code == 200
            event_types = [e["event_type"] for e in resp.json()["events"]]
            assert "INGESTION.CONFIG_CREATE" in event_types, (
                f"Expected CONFIG_CREATE event for {urn}, got {event_types}"
            )
            assert "INGESTION.COMPLETE" in event_types, (
                f"Expected COMPLETE event for {urn} after flow run, got {event_types}"
            )

    finally:
        await delete_kestra_flow(kestra_client, flow_id_02)
        await delete_kestra_flow(kestra_client, flow_id_06)
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            await delete_ingestion_events_db(async_session, urn)
            await delete_ingestion_config_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_removes_stale_flows(
    http_client, async_session: AsyncSession, kestra_client
):
    """Sync removes flows whose schedules are no longer in the configs.

    Setup: PUT config for title_master (periodic, "0 3 * * *"), sync to create flow.
    Action: DELETE the config, then sync again.
    Assertions: The flow for "0 3 * * *" is no longer in Kestra.
    Cleanup: Delete any remaining test configs.
    """
    flow_id_03 = schedule_to_flow_id("0 3 * * *")
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{_CATALOG_URN}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": _CATALOG_URN,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_cron": "0 3 * * *",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # First sync — creates the flow
        resp = await http_client.post(
            "/internal/activities/ingestion/sync-periodic-flows",
        )
        assert resp.status_code == 200
        flow_before = await kestra_client.get_flow(flow_id_03)
        assert flow_before is not None, f"Flow {flow_id_03} was not created on first sync"

        # Delete the config
        await delete_ingestion_config_db(async_session, _CATALOG_URN)
        await async_session.commit()

        # Second sync — should delete the stale flow
        resp = await http_client.post(
            "/internal/activities/ingestion/sync-periodic-flows",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert flow_id_03 in body.get("deleted", []), f"Expected {flow_id_03} in deleted: {body}"

        # Verify flow no longer exists
        flow_after = await kestra_client.get_flow(flow_id_03)
        assert flow_after is None, f"Flow {flow_id_03} still exists after second sync"

        # Check side-effect events — config creation event should exist
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{_CATALOG_URN}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "INGESTION.CONFIG_CREATE" in event_types

    finally:
        await delete_kestra_flow(kestra_client, flow_id_03)
        await delete_ingestion_events_db(async_session, _CATALOG_URN)
        await delete_ingestion_config_db(async_session, _CATALOG_URN)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_updates_on_schedule_change(
    http_client, async_session: AsyncSession, kestra_client
):
    """Patching a dataset's schedule creates the new flow and retains the old one if still needed.

    Setup: 3 datasets on "0 2 * * *", sync -> one flow.
    Action: PATCH genre_hierarchy to "0 6 * * *", sync again.
    Assertions:
    - Flow for "0 2 * * *" still exists (title_master + editions remain).
    - New flow for "0 6 * * *" exists.
    - ingestion/list-periodic for "0 2 * * *" returns 2 URNs (not genre_hierarchy).
    - ingestion/list-periodic for "0 6 * * *" returns only genre_hierarchy.
    Cleanup: Delete generated flows + test configs.
    """
    flow_id_02 = schedule_to_flow_id("0 2 * * *")
    flow_id_06 = schedule_to_flow_id("0 6 * * *")
    headers = _auth_headers()

    try:
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "source_type": "POSTGRESQL",
                    "locator": EXAMPLE_PG_LOCATOR,
                    "identifier": EXAMPLE_PG_IDENTIFIER,
                    "auth": EXAMPLE_PG_AUTH,
                    "is_active": True,
                    "schedule_cron": "0 2 * * *",
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # First sync — one flow for "0 2 * * *"
        resp = await http_client.post(
            "/internal/activities/ingestion/sync-periodic-flows",
        )
        assert resp.status_code == 200

        # PATCH genre_hierarchy to a different schedule
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{_GENRE_URN}/attr/ingestion/conf",
            headers=headers,
            json={"schedule_cron": "0 6 * * *"},
        )
        assert resp.status_code == 200, f"PATCH failed: {resp.text}"

        # Second sync — should add flow for "0 6 * * *", keep "0 2 * * *"
        resp = await http_client.post(
            "/internal/activities/ingestion/sync-periodic-flows",
        )
        assert resp.status_code == 200

        # Both flows exist in Kestra
        flow_02 = await kestra_client.get_flow(flow_id_02)
        assert flow_02 is not None, f"Flow {flow_id_02} not found after schedule change"
        flow_06 = await kestra_client.get_flow(flow_id_06)
        assert flow_06 is not None, f"Flow {flow_id_06} not found after schedule change"

        # ingestion/list-periodic for "0 2 * * *" returns title_master and editions only
        resp = await http_client.post(
            "/internal/activities/ingestion/list-periodic",
            json={"schedule_cron": "0 2 * * *"},
        )
        assert resp.status_code == 200
        urns_02 = resp.json()
        assert _CATALOG_URN in urns_02
        assert _EDITIONS_URN in urns_02
        assert _GENRE_URN not in urns_02

        # ingestion/list-periodic for "0 6 * * *" returns only genre_hierarchy
        resp = await http_client.post(
            "/internal/activities/ingestion/list-periodic",
            json={"schedule_cron": "0 6 * * *"},
        )
        assert resp.status_code == 200
        urns_06 = resp.json()
        assert _GENRE_URN in urns_06
        assert _CATALOG_URN not in urns_06
        assert _EDITIONS_URN not in urns_06

        # Check side-effect events — all URNs have CONFIG_CREATE, genre has CONFIG_UPDATE from PATCH
        for urn in (_CATALOG_URN, _EDITIONS_URN):
            resp = await http_client.get(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/event",
                headers=headers,
            )
            assert resp.status_code == 200
            event_types = [e["event_type"] for e in resp.json()["events"]]
            assert "INGESTION.CONFIG_CREATE" in event_types, (
                f"Expected CONFIG_CREATE event for {urn}, got {event_types}"
            )

        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{_GENRE_URN}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        genre_event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "INGESTION.CONFIG_CREATE" in genre_event_types
        assert "INGESTION.CONFIG_UPDATE" in genre_event_types

    finally:
        await delete_kestra_flow(kestra_client, flow_id_02)
        await delete_kestra_flow(kestra_client, flow_id_06)
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            await delete_ingestion_events_db(async_session, urn)
            await delete_ingestion_config_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_concurrency_guard_prevents_duplicate(
    http_client, async_session: AsyncSession, redis_client
):
    """Concurrent run requests for the same dataset are rejected with 409.

    Setup: PUT ingestion config for title_master.
    Action: Kick off the first run while immediately submitting a second.
    Assertions: Second request returns 409 with error_code INGESTION_RUNNING.
    Cleanup: Wait for first run to complete, DELETE config + events.
    """
    dataset_urn = _CATALOG_URN
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # Fire both requests concurrently; the second should race into a locked state
        async def _run():
            return await http_client.post(
                f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
                headers=headers,
                json={"dry_run": False},
            )

        resp1, resp2 = await asyncio.gather(_run(), _run(), return_exceptions=True)

        # One must succeed, the other must fail with 409
        statuses = {
            getattr(resp1, "status_code", None),
            getattr(resp2, "status_code", None),
        }
        assert 200 in statuses, f"No 200 among {statuses}"
        assert 409 in statuses, f"No 409 among {statuses} — concurrency guard not triggered"

        # Verify the 409 response carries the expected error code
        conflict_resp = resp1 if getattr(resp1, "status_code", None) == 409 else resp2
        body = conflict_resp.json()
        assert body.get("error_code") == "INGESTION_RUNNING", f"Unexpected error body: {body}"

        # Check side-effect events — config creation + one successful ingestion run
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "INGESTION.CONFIG_CREATE" in event_types
        assert "INGESTION.COMPLETE" in event_types

    finally:
        # The Redis lock key may still be set if the test run left it locked; clean up.
        lock_key = f"ingestion:running:{dataset_urn}"
        try:
            await redis_client.delete(lock_key)
        except Exception:
            pass
        await delete_ingestion_events_db(async_session, dataset_urn)
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()


# ── Kafka source type tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_kafka_ingestion(
    http_client, async_session: AsyncSession, datahub_client
):
    """PUT a KAFKA config and POST run — verify schema lands in DataHub.

    Validates that source_type=KAFKA (locator with bootstrap_servers,
    identifier with topic/cluster, no auth) works end-to-end: config
    creation, pipeline execution, and DataHub aspect emission.

    Setup: PUT ingestion config for a Kafka topic (no auth required).
    Action: POST .../method/run with dry_run=false.
    Assertions: 200, status == "success", entities_ingested >= 1,
                DataHub has SchemaMetadataClass with inferred fields.
    Cleanup: DELETE config + events.
    """
    from datahub.metadata.schema_classes import DatasetPropertiesClass, SchemaMetadataClass

    dataset_urn = _KAFKA_ORDERS_URN
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "source_type": "KAFKA",
                "locator": EXAMPLE_KAFKA_LOCATOR,
                "identifier": EXAMPLE_KAFKA_IDENTIFIER,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # Verify the stored config has the expected KAFKA shape
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 200, f"GET config failed: {resp.text}"
        config_body = resp.json()
        assert config_body["source_type"] == "KAFKA"
        assert config_body["locator"]["bootstrap_servers"] == "localhost:9104"
        assert config_body["identifier"]["topic"] == "imazon.orders.events"
        assert config_body["auth"] is None

        # Run real ingestion (not dry_run)
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200, f"Kafka run failed: {resp.text}"
        body = resp.json()
        assert body["status"] == "success", f"Ingestion failed: {body.get('detail')}"
        assert body["detail"]["source_type"] == "KAFKA"
        assert body["detail"]["entities_ingested"] >= 1

        # Verify metadata landed in DataHub
        schema = await datahub_client.get_aspect(dataset_urn, SchemaMetadataClass)
        assert schema is not None, "SchemaMetadataClass not found in DataHub after Kafka ingestion"
        assert len(schema.fields) > 0, "No schema fields inferred from Kafka messages"

        props = await datahub_client.get_aspect(dataset_urn, DatasetPropertiesClass)
        assert props is not None, "DatasetPropertiesClass not found in DataHub after Kafka ingestion"
        assert props.name == "imazon.orders.events"

        # Check side-effect events
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        assert events_body["total_count"] >= 2
        event_types = [e["event_type"] for e in events_body["events"]]
        assert "INGESTION.CONFIG_CREATE" in event_types
        assert "INGESTION.COMPLETE" in event_types

    finally:
        await delete_ingestion_events_db(async_session, dataset_urn)
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_mixed_source_types_in_periodic_sync(
    http_client, async_session: AsyncSession, kestra_client
):
    """Periodic sync groups configs by schedule regardless of source_type.

    Setup: PUT POSTGRESQL config (title_master) and KAFKA config (transient URN),
           both periodic with the same schedule.
    Action: POST ingestion/sync-periodic-flows.
    Assertions:
    - One flow created for the shared schedule.
    - ingestion/list-periodic returns both URNs.
    Cleanup: Delete flow + configs.
    """
    schedule = "0 4 * * *"
    flow_id = schedule_to_flow_id(schedule)
    pg_urn = _CATALOG_URN
    kafka_urn = _urn("kafka_periodic")
    headers = _auth_headers()

    try:
        # POSTGRESQL config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{pg_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": pg_urn,
                "source_type": "POSTGRESQL",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_cron": schedule,
            },
        )
        assert resp.status_code in (200, 201), f"PUT PG config failed: {resp.text}"

        # KAFKA config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{kafka_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": kafka_urn,
                "source_type": "KAFKA",
                "locator": EXAMPLE_KAFKA_LOCATOR,
                "identifier": EXAMPLE_KAFKA_IDENTIFIER,
                "is_active": True,
                "schedule_cron": schedule,
            },
        )
        assert resp.status_code in (200, 201), f"PUT Kafka config failed: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/ingestion/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"sync failed: {resp.text}"
        body = resp.json()
        assert flow_id in body.get("created", []), (
            f"Expected {flow_id} in created: {body}"
        )

        # Both URNs should appear for the shared schedule
        resp = await http_client.post(
            "/internal/activities/ingestion/list-periodic",
            json={"schedule_cron": schedule},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert pg_urn in result, f"Expected PG URN in result: {result}"
        assert kafka_urn in result, f"Expected Kafka URN in result: {result}"

        # Check side-effect events — CONFIG_CREATE for both source types
        for urn in (pg_urn, kafka_urn):
            resp = await http_client.get(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/event",
                headers=headers,
            )
            assert resp.status_code == 200
            event_types = [e["event_type"] for e in resp.json()["events"]]
            assert "INGESTION.CONFIG_CREATE" in event_types, (
                f"Expected CONFIG_CREATE event for {urn}, got {event_types}"
            )

    finally:
        await delete_kestra_flow(kestra_client, flow_id)
        for urn in (pg_urn, kafka_urn):
            await delete_ingestion_events_db(async_session, urn)
            await delete_ingestion_config_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_type, locator, identifier, auth",
    [
        pytest.param(
            "POSTGRESQL",
            EXAMPLE_PG_LOCATOR,
            {
                "database": EXAMPLE_PG_IDENTIFIER["database"],
                "schema_name": "catalog",
                "table": "nonexistent_table_xyz",
            },
            EXAMPLE_PG_AUTH,
            id="postgresql-nonexistent-table",
        ),
        pytest.param(
            "KAFKA",
            EXAMPLE_KAFKA_LOCATOR,
            {
                "topic": "nonexistent.topic.xyz",
                "cluster": EXAMPLE_KAFKA_IDENTIFIER.get("cluster", "example_kafka"),
            },
            None,
            id="kafka-nonexistent-topic",
        ),
    ],
)
async def test_run_ingestion_nonexistent_source(
    http_client, async_session: AsyncSession,
    source_type, locator, identifier, auth,
):
    """Non-dry-run ingestion against a non-existent source target fails.

    Setup: PUT config pointing to a valid connection but non-existent target
           (PG table / Kafka topic).
    Action: POST .../method/run with dry_run=false.
    Assertions: 200, status == "error", entities_ingested == 0,
                errors non-empty, INGESTION.FAIL event recorded.
    Cleanup: DELETE config + events.
    """
    dataset_urn = _urn(f"nonexistent_{source_type.lower()}")
    headers = _auth_headers()

    try:
        payload = {
            "dataset_urn": dataset_urn,
            "source_type": source_type,
            "locator": locator,
            "identifier": identifier,
            "is_active": False,
        }
        if auth is not None:
            payload["auth"] = auth

        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json=payload,
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200, f"Run request failed: {resp.text}"
        body = resp.json()
        assert body["status"] == "error", f"Expected error status, got: {body}"
        assert body["detail"]["entities_ingested"] == 0
        assert body["detail"].get("errors"), "Expected errors in detail"

        # Verify INGESTION.FAIL event recorded
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events = resp.json()["events"]
        event_types = [e["event_type"] for e in events]
        assert "INGESTION.FAIL" in event_types, (
            f"Expected INGESTION.FAIL event, got: {event_types}"
        )
    finally:
        await delete_ingestion_events_db(async_session, dataset_urn)
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()
