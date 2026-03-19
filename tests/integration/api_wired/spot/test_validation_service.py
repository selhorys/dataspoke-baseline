"""Integration tests for ValidationService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient validation_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient validation_results rows from POST run (dry_run=false).
- Transient dataspoke.events rows for event pagination and run tests.

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Kestra port-forwarded to localhost:9205
- Dummy data ingested via conftest.py Python utilities
"""

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import (
    _auth_headers,
    cleanup_events,
    emit_test_dataset,
    make_test_urn,
    seed_events,
    soft_delete_test_dataset,
)


def _urn(suffix: str) -> str:
    return make_test_urn("validation", suffix)


@pytest_asyncio.fixture
async def http_client(activity_server):
    """HTTP client pointing at the real activity server."""
    async with httpx.AsyncClient(
        base_url=f"http://localhost:{activity_server.port}",
        timeout=120.0,
    ) as client:
        yield client


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_config_crud_via_http(
    http_client, async_session: AsyncSession,
):
    """PUT -> GET -> PATCH -> GET -> DELETE -> GET (404)."""
    dataset_urn = _urn("crud_test")
    headers = _auth_headers()

    try:
        # PUT - create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": {"freshness": {"max_age_hours": 24}},
                "schedule": "0 0 * * *",
                "sla_target": {"freshness_hours": 12},
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201)
        body = resp.json()
        assert body["dataset_urn"] == dataset_urn
        assert body["owner"] == "test@imazon.com"
        assert body["sla_target"] == {"freshness_hours": 12}
        config_id = body["id"]

        # GET - read config
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == config_id

        # PATCH - update schedule
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={"schedule": "0 6 * * *"},
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

        # GET via validation router
        resp = await http_client.get(
            f"/api/v1/spoke/common/validation/{dataset_urn}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

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
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.validation_configs"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_validation_configs(
    http_client, async_session: AsyncSession,
):
    """PUT 2 configs -> GET list -> verify pagination."""
    urn1 = _urn("list_test_1")
    urn2 = _urn("list_test_2")
    headers = _auth_headers()

    try:
        for urn in (urn1, urn2):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/validation/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "rules": {"freshness": {"max_age_hours": 24}},
                    "owner": "test@imazon.com",
                },
            )
            assert resp.status_code in (200, 201)

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
            await async_session.execute(
                text(
                    "DELETE FROM dataspoke.validation_configs"
                    " WHERE dataset_urn = :urn"
                ),
                {"urn": urn},
            )
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_validation_dry_run(
    http_client, async_session: AsyncSession, activity_server,
    datahub_client,
):
    """PUT config -> POST run (dry_run=true) -> verify result."""
    dataset_urn = _urn("run_dry_test")
    headers = _auth_headers()

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="run_dry_test",
        wait_seconds=1.0,
    )

    try:
        # Create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": {"freshness": {"max_age_hours": 24}},
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201)

        # Run with dry_run=true (goes through real Kestra)
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
            headers=headers,
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"].lower() == "success"

        # Dry run should not persist results
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/result",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0
    finally:
        await soft_delete_test_dataset(datahub_client, dataset_urn)
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.validation_configs"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_validation_persists_result(
    http_client, async_session: AsyncSession, activity_server,
    datahub_client,
):
    """PUT config -> POST run (dry_run=false) -> GET results -> verify."""
    dataset_urn = _urn("run_persist_test")
    headers = _auth_headers()

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="run_persist_test",
        wait_seconds=1.0,
    )

    try:
        # Create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": {"freshness": {"max_age_hours": 24}},
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201)

        # Run without dry_run (goes through real Kestra)
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200
        run_body = resp.json()
        assert run_body["status"].lower() == "success"

        # Verify result persisted
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/result",
            headers=headers,
        )
        assert resp.status_code == 200
        results_body = resp.json()
        assert results_body["total_count"] >= 1
        result = results_body["results"][0]
        assert "quality_score" in result
        assert "dimensions" in result
    finally:
        await soft_delete_test_dataset(datahub_client, dataset_urn)
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.validation_results"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events"
                " WHERE entity_id = :urn"
                " AND entity_type = 'dataset'"
                " AND event_type LIKE 'validation.%'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.validation_configs"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_validation_events_pagination(
    http_client, async_session: AsyncSession,
):
    """Seed 3 events -> GET events -> verify pagination."""
    dataset_urn = _urn("events_test")
    headers = _auth_headers()

    event_ids = await seed_events(
        async_session,
        entity_type="dataset",
        entity_id=dataset_urn,
        event_type="validation.completed",
    )

    try:
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["events"]) == 2

        # Also test via validation router
        resp = await http_client.get(
            f"/api/v1/spoke/common/validation/{dataset_urn}/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
    finally:
        await cleanup_events(async_session, event_ids)


@pytest.mark.asyncio
async def test_run_validation_config_not_found(http_client):
    """POST run for unconfigured URN -> error.

    The data router triggers Kestra without checking config first.
    """
    fake_urn = _urn("nonexistent")
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/validation/method/run",
        headers=_auth_headers(),
        json={"dry_run": False},
    )
    assert resp.status_code in (404, 500)
