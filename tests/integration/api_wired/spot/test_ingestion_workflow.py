"""Integration tests for the ingestion workflow orchestration layer.

Separate from test_ingestion_service.py (which tests config CRUD).
This file focuses on:
- POST .../method/run endpoint (direct pipeline execution)
- POST /internal/activities/ingestion/list-periodic
- Concurrency guard (Redis SET NX)

Test-specific data extensions (created and cleaned up within each test):
- Transient ingestion_configs rows for Imazon catalog datasets and
  synthetic test URNs.
- Transient dataspoke.events rows from actual ingestion runs.

Prerequisites:
- PostgreSQL accessible via DATASPOKE_DEV_PG_HOST/PORT
- DataHub GMS accessible via DATASPOKE_DATAHUB_GMS_URL
- Redis accessible via DATASPOKE_REDIS_HOST/PORT
- Dummy data ingested via conftest.py Python utilities (catalog schema)
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import _auth_headers
from tests.integration.api_wired.spot.conftest import (
    EXAMPLE_KAFKA_IDENTIFIER,
    EXAMPLE_KAFKA_LOCATOR,
    EXAMPLE_PG_AUTH,
    EXAMPLE_PG_IDENTIFIER,
    EXAMPLE_PG_LOCATOR,
    delete_ingestion_config_db,
    delete_ingestion_events_db,
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


# ── Test cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_ingestion_via_public_api(
    http_client, async_session: AsyncSession, datahub_client
):
    """POST run on a configured dataset executes the pipeline, records events,
    and emits schema metadata to DataHub.

    Setup: PUT ingestion config for title_master (platform=postgres).
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
                "platform": "postgres",
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
                "platform": "postgres",
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
    """POST ingestion/list-periodic returns only URNs matching the requested schedule tier.

    Setup: PUT 4 configs (A/B: active, schedule_tier="daily";
           C: active, schedule_tier="weekly"; D: is_active=False).
    Action: POST /internal/activities/ingestion/list-periodic {"schedule_tier": "daily"}.
    Assertions: Result contains A and B; does not contain C or D.
    Cleanup: DELETE all test configs.
    """
    urn_a = _urn("periodic_a")
    urn_b = _urn("periodic_b")
    urn_c = _urn("periodic_c")
    urn_d = _urn("periodic_d")
    headers = _auth_headers()

    try:
        # A: active, schedule_tier="daily"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_a}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_a,
                "platform": "postgres",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_tier": "daily",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config A failed: {resp.text}"

        # B: active, schedule_tier="daily"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_b}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_b,
                "platform": "postgres",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_tier": "daily",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config B failed: {resp.text}"

        # C: active, schedule_tier="weekly" (different tier — should be excluded)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_c}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_c,
                "platform": "postgres",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_tier": "weekly",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config C failed: {resp.text}"

        # D: inactive (excluded from periodic list)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_d}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": urn_d,
                "platform": "postgres",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config D failed: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/ingestion/list-periodic",
            json={"schedule_tier": "daily"},
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
                "platform": "postgres",
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


# ── Kafka platform tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_kafka_ingestion(
    http_client, async_session: AsyncSession, datahub_client
):
    """PUT a kafka config and POST run — verify schema lands in DataHub.

    Validates that platform=kafka (locator with bootstrap_servers,
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
                "platform": "kafka",
                "locator": EXAMPLE_KAFKA_LOCATOR,
                "identifier": EXAMPLE_KAFKA_IDENTIFIER,
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # Verify the stored config has the expected kafka shape
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 200, f"GET config failed: {resp.text}"
        config_body = resp.json()
        assert config_body["platform"] == "kafka"
        assert config_body["locator"]["bootstrap_servers"] == EXAMPLE_KAFKA_LOCATOR["bootstrap_servers"]
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
        assert body["detail"]["platform"] == "kafka"
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
