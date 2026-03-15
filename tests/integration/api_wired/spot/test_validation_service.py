"""Integration tests for ValidationService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient validation_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient validation_results rows from POST run (dry_run=false).
- Transient dataspoke.events rows for event pagination and run tests.

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Temporal port-forwarded to localhost:9205
- Dummy data ingested via conftest.py Python utilities
"""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.workflows.validation import ValidationWorkflow, run_validation_activity
from tests.integration.api_wired.conftest import (
    make_temporal_worker,
    mock_cache,
    mock_qdrant,
)
from tests.integration.conftest import (
    _auth_headers,
    cleanup_events,
    make_test_urn,
    override_app,
    seed_events,
)


def _urn(suffix: str) -> str:
    return make_test_urn("validation", suffix)


_WF_MODULE = "src.workflows.validation"


@pytest_asyncio.fixture
async def temporal_worker(temporal_client, datahub_client, async_session):
    async with make_temporal_worker(
        temporal_client,
        datahub_client,
        db_session=async_session,
        workflow_module=_WF_MODULE,
        workflow_cls=ValidationWorkflow,
        activity_fn=run_validation_activity,
        extra_patches={
            f"{_WF_MODULE}.make_qdrant": mock_qdrant(),
            f"{_WF_MODULE}.make_cache": mock_cache(),
        },
    ) as worker:
        yield worker


@pytest_asyncio.fixture
async def http_client(datahub_client, mock_cache, async_session, temporal_client):
    """HTTP client with real DI providers pointing to dev-env infra."""
    async with override_app(
        datahub=datahub_client,
        redis=mock_cache,
        db=async_session,
        temporal=temporal_client,
    ) as client:
        yield client


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_config_crud_via_http(http_client, async_session: AsyncSession):
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
        assert resp.status_code == 200
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
            text("DELETE FROM dataspoke.validation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_validation_configs(http_client, async_session: AsyncSession):
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
            assert resp.status_code == 200

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
                text("DELETE FROM dataspoke.validation_configs WHERE dataset_urn = :urn"),
                {"urn": urn},
            )
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_validation_dry_run(http_client, async_session: AsyncSession, temporal_worker):
    """PUT config -> POST run (dry_run=true) -> verify result has quality_score."""
    dataset_urn = _urn("run_dry_test")
    headers = _auth_headers()

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
        assert resp.status_code == 200

        # Run with dry_run=true
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
            headers=headers,
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["detail"]["dry_run"] is True
        assert "quality_score" in body["detail"]
        assert "dimensions" in body["detail"]

        # Dry run should not persist results
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/result",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.validation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_validation_persists_result(
    http_client, async_session: AsyncSession, temporal_worker
):
    """PUT config -> POST run (dry_run=false) -> GET results -> verify persisted."""
    dataset_urn = _urn("run_persist_test")
    headers = _auth_headers()

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
        assert resp.status_code == 200

        # Run without dry_run
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200
        run_body = resp.json()
        assert run_body["status"] == "success"

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
        await async_session.execute(
            text("DELETE FROM dataspoke.validation_results WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events WHERE entity_id = :urn AND entity_type = 'dataset' AND event_type LIKE 'validation.%'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.validation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_validation_events_pagination(http_client, async_session: AsyncSession):
    """Seed 3 events -> GET events with limit=2 -> verify total_count=3, returned=2."""
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
async def test_run_validation_config_not_found(http_client, temporal_worker):
    """POST run for unconfigured URN -> 404."""
    fake_urn = _urn("nonexistent")
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/validation/method/run",
        headers=_auth_headers(),
        json={"dry_run": False},
    )
    assert resp.status_code == 404
