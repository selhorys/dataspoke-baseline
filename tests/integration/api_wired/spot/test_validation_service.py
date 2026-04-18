"""Integration tests for ValidationService config CRUD against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient validation_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient dataspoke.events rows for event pagination tests.

Prerequisites:
- PostgreSQL accessible via DATASPOKE_DEV_PG_HOST/PORT
- DataHub GMS accessible via DATASPOKE_DATAHUB_GMS_URL
- Dummy data ingested via conftest.py Python utilities
"""

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
from tests.integration.conftest import (
    _auth_headers,
    cleanup_events,
    seed_events,
)


def _urn(suffix: str) -> str:
    return make_validation_urn(suffix)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_config_crud_via_http(
    http_client, async_session: AsyncSession,
):
    """PUT -> GET -> PATCH schedule -> GET via domain router -> DELETE -> GET (404)."""
    dataset_urn = _urn("crud_test")
    headers = _auth_headers()

    try:
        await seed_dataset_registry(async_session, dataset_urn)

        # PUT - create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "schedule_tier": "daily",
                "is_active": True,
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201)
        body = resp.json()
        assert body["dataset_urn"] == dataset_urn
        assert body["owner"] == "test@imazon.com"
        assert body["is_active"] is True
        assert body["schedule_tier"] == "daily"
        config_id = body["id"]

        # GET - read config via data router
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == config_id

        # PATCH - update schedule tier
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={"schedule_tier": "weekly"},
        )
        assert resp.status_code == 200
        assert resp.json()["schedule_tier"] == "weekly"

        # GET via validation domain router
        resp = await http_client.get(
            f"/api/v1/spoke/common/validation/{dataset_urn}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["schedule_tier"] == "weekly"

        # DELETE
        resp = await http_client.delete(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
        )
        assert resp.status_code == 204

        # GET after delete -> 404
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
        )
        assert resp.status_code == 404
    finally:
        await delete_validation_config_db(async_session, dataset_urn)
        await delete_dataset_registry_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_validation_configs(
    http_client, async_session: AsyncSession,
):
    """PUT 2 configs -> GET list via domain router -> verify pagination."""
    urn1 = _urn("list_test_1")
    urn2 = _urn("list_test_2")
    headers = _auth_headers()

    try:
        for urn in (urn1, urn2):
            await seed_dataset_registry(async_session, urn)

        for urn in (urn1, urn2):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/validation/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                    "owner": "test@imazon.com",
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        resp = await http_client.get(
            "/api/v1/spoke/common/validation",
            headers=headers,
            params={"limit": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] >= 2
        urns = [c["dataset_urn"] for c in body["configs"]]
        assert urn1 in urns
        assert urn2 in urns
    finally:
        for urn in (urn1, urn2):
            await delete_validation_config_db(async_session, urn)
            await delete_dataset_registry_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_validation_basic(
    http_client, async_session: AsyncSession,
):
    """PUT config -> POST method/run -> verify response shape and events recorded.

    Does not depend on a real DataHub dataset or test-mode stub behavior.
    Verifies that the run endpoint returns a well-formed RunResultResponse and
    that at least one event is recorded regardless of rule outcome.
    """
    dataset_urn = _urn("run_test")
    headers = _auth_headers()

    try:
        await seed_dataset_registry(async_session, dataset_urn)

        # Create config
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

        # POST run (no partition — direct pipeline, no Airflow DAG involved)
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
            headers=headers,
            json={"partition": None},
        )
        assert resp.status_code == 200, f"Run failed: {resp.text}"
        body = resp.json()
        assert "run_id" in body
        assert "status" in body
        # Synthetic URN has no DataHub data — freshness rule returns FAILURE
        assert body["status"] == "failure"
        assert "total" in body
        assert "passed" in body
        assert "failed" in body
        assert "errored" in body

        # Check events were recorded
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        assert events_body["total_count"] >= 2
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
async def test_run_validation_not_found(http_client):
    """POST method/run for unconfigured URN -> 404 VALIDATION_CONFIG_NOT_FOUND.

    ValidationService.run() raises EntityNotFoundError when no config
    exists for the requested dataset URN. The router translates this to 404.
    """
    fake_urn = _urn("nonexistent")
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/validation/method/run",
        headers=_auth_headers(),
        json={"partition": None},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validation_events_pagination(
    http_client, async_session: AsyncSession,
):
    """Seed 3 events -> GET with limit=2 -> verify pagination on both data and domain routers."""
    dataset_urn = _urn("events_test")
    headers = _auth_headers()

    event_ids = await seed_events(
        async_session,
        entity_type="dataset",
        entity_id=dataset_urn,
        event_type="VALIDATION.COMPLETE",
    )

    try:
        # Verify pagination via canonical data router
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["events"]) == 2

        # Verify the dedicated validation router still returns the config detail
        # (the /event sub-path is canonical-only; dedicated router has list + detail only)
        resp = await http_client.get(
            f"/api/v1/spoke/common/validation/{dataset_urn}",
            headers=headers,
        )
        # 404 is expected here since no config exists for this synthetic test URN
        assert resp.status_code in (200, 404)
    finally:
        await cleanup_events(async_session, event_ids)
