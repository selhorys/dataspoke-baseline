"""Integration tests for the validation workflow orchestration layer.

Separate from test_validation_service.py (which tests config CRUD).
This file focuses on:
- POST .../attr/validation/method/run endpoint (full pipeline execution)
- POST /internal/activities/validation/list-periodic
- Concurrency guard (Redis SET NX)

Test-specific data extensions (created and cleaned up within each test):
- Transient validation_configs rows for Imazon catalog datasets.
- Transient dataspoke.events rows from actual validation runs.

Prerequisites:
- PostgreSQL accessible via DATASPOKE_DEV_PG_HOST/PORT
- DataHub GMS accessible via DATASPOKE_DATAHUB_GMS_URL
- Redis accessible via DATASPOKE_REDIS_HOST/PORT
- Dummy data ingested via conftest.py Python utilities (catalog schema)
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.api_wired.spot.conftest import (
    delete_dataset_registry_db,
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
                "schedule_tier": "daily",
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
    """POST validation/list-periodic returns only URNs matching the requested schedule tier.

    Setup: PUT 4 configs:
           A/B: is_active=True + schedule_tier="daily"
           C: is_active=True + schedule_tier="weekly" (different tier)
           D: is_active=False (non-periodic, should be excluded)
    Action: POST /internal/activities/validation/list-periodic {"schedule_tier": "daily"}.
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

        # A: active, schedule_tier="daily"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_a}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_a,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_tier": "daily",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config A failed: {resp.text}"

        # B: active, schedule_tier="daily"
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_b}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_b,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_tier": "daily",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config B failed: {resp.text}"

        # C: active, schedule_tier="weekly" (different tier)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{urn_c}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": urn_c,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_tier": "weekly",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config C failed: {resp.text}"

        # D: non-periodic (is_active defaults to false — excluded from periodic list)
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
            json={"schedule_tier": "daily"},
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
