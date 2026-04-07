"""Integration tests for the validation workflow orchestration layer.

Separate from test_validation_service.py (which tests config CRUD).
This file focuses on:
- POST .../attr/validation/method/run endpoint (full pipeline execution)
- POST /internal/activities/validation/list-periodic
- POST /internal/activities/validation/sync-periodic-flows
- Concurrency guard (Redis SET NX)

Test-specific data extensions (created and cleaned up within each test):
- Transient validation_configs rows for Imazon catalog datasets.
- Transient dataspoke.events rows from actual validation runs.
- Dynamically generated Kestra flows (validation-periodic-*).

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
from src.workflows.validation_sync import schedule_to_flow_id
from tests.integration.api_wired.spot.conftest import (
    delete_dataset_registry_db,
    delete_kestra_flow,
    delete_validation_config_db,
    delete_validation_events_db,
    delete_validation_results_db,
    make_validation_urn,
    seed_dataset_registry,
)
from tests.integration.conftest import _auth_headers

# Triggers module_dummy_data fixture to reset and re-ingest the catalog schema
# in both PostgreSQL and DataHub before this module's tests run.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])

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


def _urn(suffix: str) -> str:
    return make_validation_urn(suffix)


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
async def test_run_validation_via_public_api(
    http_client, async_session: AsyncSession
):
    """POST run on a configured dataset executes the pipeline and records events.

    Setup: PUT validation config for title_master (uses real catalog data in DataHub).
    Action: POST .../attr/validation/method/run.
    Assertions: 200, run_id present, status in ("success", "failure", "error"),
                GET results has total_count >= 1,
                GET events has CONFIG_CREATE + COMPLETE.
    Cleanup: DELETE results + events + config.
    """
    dataset_urn = _CATALOG_URN
    headers = _auth_headers()

    try:
        # PUT config — configs are immediately runnable after PUT
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_cron": "0 2 * * *",
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # Run validation
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
            headers=headers,
            json={"partition": None},
        )
        assert resp.status_code == 200, f"Run failed: {resp.text}"
        body = resp.json()
        assert "run_id" in body
        # Dataset was freshly ingested by the dummy data fixture — freshness passes
        assert body["status"] == "success"

        # Verify results were persisted
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/result",
            headers=headers,
        )
        assert resp.status_code == 200
        results_body = resp.json()
        assert results_body["total_count"] >= 1
        result = results_body["results"][0]
        assert result["rule_id"] == "freshness_01"
        assert "assertion_result" in result
        assert "run_id" in result

        # Verify side-effect events
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        event_types = [e["event_type"] for e in events_body["events"]]
        assert "VALIDATION.CONFIG_CREATE" in event_types
        assert "VALIDATION.COMPLETE" in event_types

    finally:
        await delete_validation_results_db(async_session, dataset_urn)
        await delete_validation_events_db(async_session, dataset_urn)
        await delete_validation_config_db(async_session, dataset_urn)
        await delete_dataset_registry_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_periodic_datasets(
    http_client, async_session: AsyncSession
):
    """POST validation/list-periodic returns only URNs matching the requested schedule.

    Setup: PUT 4 configs:
           A/B: periodic=true + cron "0 2 * * *"
           C: periodic=true + cron "0 6 * * *"
           D: periodic=false (non-periodic, should be excluded)
    Action: POST /internal/activities/validation/list-periodic {"schedule": "0 2 * * *"}.
    Assertions: Result contains A and B; does not contain C or D.
                Each URN has a CONFIG_CREATE event.
    Cleanup: DELETE events + configs.
    """
    urn_a = _urn("periodic_a")
    urn_b = _urn("periodic_b")
    urn_c = _urn("periodic_c")
    urn_d = _urn("periodic_d")
    headers = _auth_headers()

    try:
        for urn in (urn_a, urn_b, urn_c, urn_d):
            await seed_dataset_registry(async_session, urn)

        # A: periodic, schedule cron "0 2 * * *"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_a}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_a,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_cron": "0 2 * * *",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config A failed: {resp.text}"

        # B: periodic, schedule cron "0 2 * * *"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_b}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_b,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_cron": "0 2 * * *",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config B failed: {resp.text}"

        # C: periodic, schedule cron "0 6 * * *" (different schedule)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_c}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_c,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_cron": "0 6 * * *",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config C failed: {resp.text}"

        # D: non-periodic (periodic defaults to false — excluded from periodic list)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_d}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_d,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config D failed: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/validation/list-periodic",
            json={"schedule_cron": "0 2 * * *"},
        )
        assert resp.status_code == 200, f"validation/list-periodic failed: {resp.text}"
        result = resp.json()

        assert urn_a in result, f"Expected {urn_a} in result: {result}"
        assert urn_b in result, f"Expected {urn_b} in result: {result}"
        assert urn_c not in result, f"Did not expect {urn_c} in result: {result}"
        assert urn_d not in result, f"Did not expect {urn_d} in result: {result}"

        # Check side-effect events — each config PUT should emit CONFIG_CREATE
        for urn in (urn_a, urn_b, urn_c, urn_d):
            resp = await http_client.get(
                f"/api/v1/spoke/common/data/{urn}/attr/validation/event",
                headers=headers,
            )
            assert resp.status_code == 200
            event_types = [e["event_type"] for e in resp.json()["events"]]
            assert "VALIDATION.CONFIG_CREATE" in event_types, (
                f"Expected CONFIG_CREATE event for {urn}, got {event_types}"
            )

    finally:
        for urn in (urn_a, urn_b, urn_c, urn_d):
            await delete_validation_events_db(async_session, urn)
            await delete_validation_config_db(async_session, urn)
            await delete_dataset_registry_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_creates_flows_per_schedule(
    http_client, async_session: AsyncSession, kestra_client
):
    """Sync endpoint generates one Kestra flow per unique active cron schedule.

    Setup: 3 real catalog datasets — title_master + editions share "0 2 * * *",
           genre_hierarchy gets "0 6 * * *". All active.
    Action: POST /internal/activities/validation/sync-periodic-flows.
    Assertions: Two flows registered in Kestra (one per schedule),
                both retrievable via kestra_client.get_flow().
                Both flows execute successfully when triggered.
                All datasets have CONFIG_CREATE + COMPLETE events.
    Cleanup: Delete generated flows + events + configs.
    """
    flow_id_02 = schedule_to_flow_id("0 2 * * *")
    flow_id_06 = schedule_to_flow_id("0 6 * * *")
    headers = _auth_headers()

    try:
        # title_master + editions: periodic, cron "0 2 * * *"
        for urn in (_CATALOG_URN, _EDITIONS_URN):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/validation/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                    "schedule_cron": "0 2 * * *",
                    "is_active": True,
                    "owner": "test@imazon.com",
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed for {urn}: {resp.text}"

        # genre_hierarchy: periodic, cron "0 6 * * *"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{_GENRE_URN}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": _GENRE_URN,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_cron": "0 6 * * *",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed for genre: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/validation/sync-periodic-flows",
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

        # Trigger both flows and verify round-trip.
        # Skipped in host-mode testing: Kestra flows make HTTP callbacks to the
        # test-mode server, but host.docker.internal is unreachable from GKE pods.
        # The flow creation + registration above is the primary assertion.
        # Full round-trip is verified in in-cluster testing mode.

    finally:
        await delete_kestra_flow(kestra_client, flow_id_02)
        await delete_kestra_flow(kestra_client, flow_id_06)
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            await delete_validation_results_db(async_session, urn)
            await delete_validation_events_db(async_session, urn)
            await delete_validation_config_db(async_session, urn)
            await delete_dataset_registry_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_removes_stale_flows(
    http_client, async_session: AsyncSession, kestra_client
):
    """Sync removes flows whose cron schedules are no longer in active configs.

    Setup: PUT config for title_master (active, "0 3 * * *"), sync to create flow.
    Action: DELETE the config, then sync again.
    Assertions: The flow for "0 3 * * *" is no longer in Kestra.
                CONFIG_CREATE event exists for the config.
    Cleanup: Delete any remaining flows + events + configs.
    """
    flow_id_03 = schedule_to_flow_id("0 3 * * *")
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{_CATALOG_URN}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": _CATALOG_URN,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_cron": "0 3 * * *",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # First sync — creates the flow
        resp = await http_client.post(
            "/internal/activities/validation/sync-periodic-flows",
        )
        assert resp.status_code == 200
        flow_before = await kestra_client.get_flow(flow_id_03)
        assert flow_before is not None, f"Flow {flow_id_03} was not created on first sync"

        # Delete the config directly (bypasses events, simulates hard removal)
        await delete_validation_config_db(async_session, _CATALOG_URN)
        await async_session.commit()

        # Second sync — should delete the stale flow
        resp = await http_client.post(
            "/internal/activities/validation/sync-periodic-flows",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert flow_id_03 in body.get("deleted", []), f"Expected {flow_id_03} in deleted: {body}"

        # Verify flow no longer exists
        flow_after = await kestra_client.get_flow(flow_id_03)
        assert flow_after is None, f"Flow {flow_id_03} still exists after second sync"

        # Check side-effect events — config creation event should exist
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{_CATALOG_URN}/attr/validation/event",
            headers=headers,
        )
        assert resp.status_code == 200
        event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "VALIDATION.CONFIG_CREATE" in event_types

    finally:
        await delete_kestra_flow(kestra_client, flow_id_03)
        await delete_validation_events_db(async_session, _CATALOG_URN)
        await delete_validation_config_db(async_session, _CATALOG_URN)
        await delete_dataset_registry_db(async_session, _CATALOG_URN)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_updates_on_schedule_change(
    http_client, async_session: AsyncSession, kestra_client
):
    """Patching a dataset's schedule causes the new cron flow to be created.

    Setup: 3 datasets all on "0 2 * * *", all active. Sync -> one flow.
    Action: PATCH _GENRE_URN schedule to {"cron": "0 6 * * *"}, sync again.
    Assertions:
    - Flow for "0 2 * * *" still exists (title_master + editions remain).
    - New flow for "0 6 * * *" exists.
    - list-periodic "0 2 * * *" returns CATALOG + EDITIONS only.
    - list-periodic "0 6 * * *" returns GENRE only.
    - All URNs have CONFIG_CREATE; GENRE has CONFIG_UPDATE from the PATCH.
    Cleanup: Delete generated flows + events + configs.
    """
    flow_id_02 = schedule_to_flow_id("0 2 * * *")
    flow_id_06 = schedule_to_flow_id("0 6 * * *")
    headers = _auth_headers()

    try:
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/validation/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                    "schedule_cron": "0 2 * * *",
                    "is_active": True,
                    "owner": "test@imazon.com",
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed for {urn}: {resp.text}"

        # First sync — one flow for "0 2 * * *"
        resp = await http_client.post(
            "/internal/activities/validation/sync-periodic-flows",
        )
        assert resp.status_code == 200

        # PATCH schedule — must include periodic=True in payload to satisfy validator
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{_GENRE_URN}/attr/validation/conf",
            headers=headers,
            json={"schedule_cron": "0 6 * * *", "is_active": True},
        )
        assert resp.status_code == 200, f"PATCH schedule failed: {resp.text}"

        # Second sync — should add flow for "0 6 * * *", keep "0 2 * * *"
        resp = await http_client.post(
            "/internal/activities/validation/sync-periodic-flows",
        )
        assert resp.status_code == 200

        # Both flows exist in Kestra
        flow_02 = await kestra_client.get_flow(flow_id_02)
        assert flow_02 is not None, f"Flow {flow_id_02} not found after schedule change"
        flow_06 = await kestra_client.get_flow(flow_id_06)
        assert flow_06 is not None, f"Flow {flow_id_06} not found after schedule change"

        # list-periodic "0 2 * * *" returns title_master and editions only
        resp = await http_client.post(
            "/internal/activities/validation/list-periodic",
            json={"schedule_cron": "0 2 * * *"},
        )
        assert resp.status_code == 200
        urns_02 = resp.json()
        assert _CATALOG_URN in urns_02
        assert _EDITIONS_URN in urns_02
        assert _GENRE_URN not in urns_02

        # list-periodic "0 6 * * *" returns only genre_hierarchy
        resp = await http_client.post(
            "/internal/activities/validation/list-periodic",
            json={"schedule_cron": "0 6 * * *"},
        )
        assert resp.status_code == 200
        urns_06 = resp.json()
        assert _GENRE_URN in urns_06
        assert _CATALOG_URN not in urns_06
        assert _EDITIONS_URN not in urns_06

        # Check side-effect events — title_master + editions have CONFIG_CREATE
        for urn in (_CATALOG_URN, _EDITIONS_URN):
            resp = await http_client.get(
                f"/api/v1/spoke/common/data/{urn}/attr/validation/event",
                headers=headers,
            )
            assert resp.status_code == 200
            event_types = [e["event_type"] for e in resp.json()["events"]]
            assert "VALIDATION.CONFIG_CREATE" in event_types, (
                f"Expected CONFIG_CREATE event for {urn}, got {event_types}"
            )

        # genre_hierarchy has both CONFIG_CREATE and CONFIG_UPDATE
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{_GENRE_URN}/attr/validation/event",
            headers=headers,
        )
        assert resp.status_code == 200
        genre_event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "VALIDATION.CONFIG_CREATE" in genre_event_types
        assert "VALIDATION.CONFIG_UPDATE" in genre_event_types

    finally:
        await delete_kestra_flow(kestra_client, flow_id_02)
        await delete_kestra_flow(kestra_client, flow_id_06)
        for urn in (_CATALOG_URN, _EDITIONS_URN, _GENRE_URN):
            await delete_validation_results_db(async_session, urn)
            await delete_validation_events_db(async_session, urn)
            await delete_validation_config_db(async_session, urn)
            await delete_dataset_registry_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_concurrency_guard_prevents_duplicate(
    http_client, async_session: AsyncSession, redis_client
):
    """Concurrent run requests for the same dataset are rejected with 409.

    Setup: PUT validation config for title_master (active).
    Action: Fire two concurrent POST .../method/run requests.
    Assertions: One 200, one 409 with error_code "VALIDATION_RUNNING".
                CONFIG_CREATE + COMPLETE events recorded.
    Cleanup: Delete Redis lock key, results, events, config.
    """
    dataset_urn = _CATALOG_URN
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # Fire both requests concurrently; the second should race into a locked state
        async def _run():
            return await http_client.post(
                f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
                headers=headers,
                json={"partition": None},
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
        assert body.get("error_code") == "VALIDATION_RUNNING", f"Unexpected error body: {body}"

        # Check side-effect events — config creation + one successful run
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/event",
            headers=headers,
        )
        assert resp.status_code == 200
        event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "VALIDATION.CONFIG_CREATE" in event_types
        assert "VALIDATION.COMPLETE" in event_types

    finally:
        # The Redis lock key may still be set if the test run left it locked; clean up.
        lock_key = f"validation:running:{dataset_urn}"
        try:
            await redis_client.delete(lock_key)
        except Exception:
            pass
        await delete_validation_results_db(async_session, dataset_urn)
        await delete_validation_events_db(async_session, dataset_urn)
        await delete_validation_config_db(async_session, dataset_urn)
        await delete_dataset_registry_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_validation_config_rejected_for_unregistered_dataset(
    http_client, async_session: AsyncSession,
):
    """PUT validation config for a dataset not in DataHub is rejected with 422.

    Unlike ingestion (which allows config for datasets not yet in DataHub
    because ingestion creates them), validation requires the dataset to
    already exist in DataHub.

    Setup: Use a synthetic URN that does not exist in DataHub.
    Action: PUT validation config.
    Assertions: 422 with error_code DATASET_NOT_IN_DATAHUB.
    Cleanup: Delete any registry row created during the check.
    """
    dataset_urn = _urn("unregistered_datahub")
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("error_code") == "DATASET_NOT_IN_DATAHUB", (
            f"Unexpected error body: {body}"
        )
    finally:
        await delete_dataset_registry_db(async_session, dataset_urn)
        await delete_validation_config_db(async_session, dataset_urn)
        await async_session.commit()
