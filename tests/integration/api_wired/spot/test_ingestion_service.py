"""Integration tests for IngestionService config CRUD against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient ingestion_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient dataspoke.events rows for event pagination tests.

Prerequisites:
- PostgreSQL accessible via DATASPOKE_DEV_PG_HOST/PORT
- DataHub GMS accessible via DATASPOKE_DATAHUB_GMS_URL
- Dummy data ingested via conftest.py Python utilities
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import (
    _auth_headers,
    cleanup_events,
    seed_events,
)
from tests.integration.api_wired.spot.conftest import (
    EXAMPLE_PG_AUTH,
    EXAMPLE_PG_IDENTIFIER,
    EXAMPLE_PG_LOCATOR,
    delete_ingestion_config_db,
    delete_ingestion_events_db,
    make_ingestion_urn,
)


def _urn(suffix: str) -> str:
    return make_ingestion_urn(suffix)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_config_crud_via_http(
    http_client, async_session: AsyncSession,
):
    """PUT -> GET -> PATCH -> GET -> DELETE -> GET (404)."""
    dataset_urn = _urn("crud_test")
    headers = _auth_headers()

    try:
        # PUT - create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "platform": "postgres",
                "locator": EXAMPLE_PG_LOCATOR,
                "identifier": EXAMPLE_PG_IDENTIFIER,
                "auth": EXAMPLE_PG_AUTH,
                "is_active": True,
                "schedule_tier": "daily",
            },
        )
        assert resp.status_code in (200, 201)
        body = resp.json()
        assert body["dataset_urn"] == dataset_urn
        assert body["platform"] == "postgres"
        assert body["is_active"] is True
        assert body["schedule_tier"] == "daily"
        config_id = body["id"]

        # GET - read config
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == config_id

        # PATCH - update schedule tier
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={"schedule_tier": "weekly"},
        )
        assert resp.status_code == 200
        assert resp.json()["schedule_tier"] == "weekly"

        # GET via ingestion router
        resp = await http_client.get(
            f"/api/v1/spoke/common/ingestion/{dataset_urn}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["schedule_tier"] == "weekly"

        # DELETE
        resp = await http_client.delete(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 204

        # GET after delete -> 404
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 404
    finally:
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_ingestion_configs(
    http_client, async_session: AsyncSession,
):
    """PUT 2 configs -> GET list -> verify pagination."""
    urn1 = _urn("list_test_1")
    urn2 = _urn("list_test_2")
    headers = _auth_headers()

    try:
        for urn in (urn1, urn2):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "platform": "postgres",
                    "locator": EXAMPLE_PG_LOCATOR,
                    "identifier": EXAMPLE_PG_IDENTIFIER,
                    "auth": EXAMPLE_PG_AUTH,
                    "is_active": False,
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        resp = await http_client.get(
            "/api/v1/spoke/common/ingestion",
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
            await delete_ingestion_config_db(async_session, urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_ingestion_dry_run(
    http_client, async_session: AsyncSession,
):
    """PUT config -> POST run dry_run=true -> verify events recorded directly (no Airflow DAG)."""
    dataset_urn = _urn("run_test")
    headers = _auth_headers()

    try:
        # Create config
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

        # Run with dry_run=true (direct pipeline — no Airflow DAG involved)
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
            headers=headers,
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["detail"]["dry_run"] is True

        # Check events were recorded via canonical path
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_count"] >= 1
    finally:
        await delete_ingestion_events_db(async_session, dataset_urn)
        await delete_ingestion_config_db(async_session, dataset_urn)
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_ingestion_not_found(http_client):
    """POST run for unconfigured URN -> 404 INGESTION_CONFIG_NOT_FOUND.

    IngestionService.run() raises EntityNotFoundError when no config
    exists for the requested dataset URN. The router translates this to 404.
    """
    fake_urn = _urn("nonexistent")
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/ingestion/method/run",
        headers=_auth_headers(),
        json={"dry_run": False},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_events_pagination(
    http_client, async_session: AsyncSession,
):
    """Seed 3 events -> GET with limit=2 -> verify pagination."""
    dataset_urn = _urn("events_test")
    headers = _auth_headers()

    event_ids = await seed_events(
        async_session,
        entity_type="dataset",
        entity_id=dataset_urn,
        event_type="INGESTION.COMPLETE",
    )

    try:
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["events"]) == 2

        # Verify the dedicated ingestion router still returns the config detail
        # (the /event sub-path is canonical-only; dedicated router has list + detail only)
        resp = await http_client.get(
            f"/api/v1/spoke/common/ingestion/{dataset_urn}",
            headers=headers,
        )
        # 404 is expected here since no config exists for this synthetic test URN
        assert resp.status_code in (200, 404)
    finally:
        await cleanup_events(async_session, event_ids)
