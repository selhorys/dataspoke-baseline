"""Integration tests for MetricsService against dev-env infrastructure.

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Dummy data ingested via dummy-data-reset.sh && dummy-data-ingest.sh
"""

import json
import os
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.datahub.client import DataHubClient

_datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "http://localhost:9004")
_datahub_frontend_url = os.environ.get("DATASPOKE_DATAHUB_FRONTEND_URL", "http://localhost:9002")
_datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

_DG_PREFIX = "/api/v1/spoke/dg/metric"
_TEST_METRIC_PREFIX = "integration_test.metrics"


@pytest_asyncio.fixture
async def datahub_client():
    import base64

    import requests

    token = _datahub_token
    if not token:
        try:
            resp = requests.post(
                f"{_datahub_frontend_url}/logIn",
                json={"username": "datahub", "password": "datahub"},
                timeout=5,
            )
            resp.raise_for_status()
            cookie = resp.headers.get("Set-Cookie", "")
            if "PLAY_SESSION=" in cookie:
                play_session = cookie.split("PLAY_SESSION=")[1].split(";")[0]
                payload = play_session.split(".")[1]
                payload += "=" * (4 - len(payload) % 4)
                data = json.loads(base64.b64decode(payload))
                token = data.get("data", {}).get("token", "")
        except Exception:
            pytest.skip("Cannot obtain DataHub token (frontend unreachable)")
    return DataHubClient(gms_url=_datahub_gms_url, token=token)


@pytest_asyncio.fixture
async def mock_cache():
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    cache.publish = AsyncMock()
    cache.delete = AsyncMock()
    return cache


@pytest_asyncio.fixture
async def http_client(datahub_client, mock_cache, async_session):
    from src.api.dependencies import get_datahub, get_db, get_redis
    from src.api.main import app

    app.dependency_overrides[get_datahub] = lambda: datahub_client
    app.dependency_overrides[get_redis] = lambda: mock_cache

    async def _override_db():
        yield async_session

    app.dependency_overrides[get_db] = _override_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client

    app.dependency_overrides.clear()


def _auth_headers() -> dict[str, str]:
    from src.api.auth.jwt import create_access_token

    token, _ = create_access_token(
        subject="integration-test-user", groups=["de", "da", "dg"], email="test@example.com"
    )
    return {"Authorization": f"Bearer {token}"}


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metric_config_crud_via_http(http_client, async_session: AsyncSession):
    """PUT → GET → PATCH → DELETE → GET (404)."""
    metric_id = f"{_TEST_METRIC_PREFIX}.crud_test"
    headers = _auth_headers()

    try:
        # PUT - create metric config
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "CRUD Test Metric",
                "description": "Integration test metric",
                "theme": "quality",
                "measurement_query": {"type": "dataset_count"},
                "schedule": "0 * * * *",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "CRUD Test Metric"
        assert body["id"] == metric_id

        # GET - read metric
        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == metric_id

        # GET - read config
        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 * * * *"

        # PATCH - update
        resp = await http_client.patch(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={"schedule": "0 6 * * *"},
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

        # DELETE
        resp = await http_client.delete(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
        )
        assert resp.status_code == 204

        # GET after delete → 404
        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}",
            headers=headers,
        )
        assert resp.status_code == 404
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_metric_run_and_result_persistence(http_client, async_session: AsyncSession):
    """PUT config → POST run → GET results → verify persisted."""
    metric_id = f"{_TEST_METRIC_PREFIX}.run_persist"
    headers = _auth_headers()

    try:
        # Create config
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Run Persist Test",
                "description": "Test run persistence",
                "theme": "quality",
                "measurement_query": {"type": "dataset_count"},
            },
        )
        assert resp.status_code == 200

        # Run
        resp = await http_client.post(
            f"{_DG_PREFIX}/{metric_id}/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200
        run_body = resp.json()
        assert run_body["status"] == "success"
        assert "value" in run_body["detail"]

        # Verify result persisted
        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}/attr/result",
            headers=headers,
        )
        assert resp.status_code == 200
        results_body = resp.json()
        assert results_body["total_count"] >= 1
        result = results_body["results"][0]
        assert "value" in result
        assert "breakdown" in result
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_results WHERE metric_id = :id"),
            {"id": metric_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE entity_id = :id AND entity_type = 'metric'"),
            {"id": metric_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_metric_run_dry_run(http_client, async_session: AsyncSession):
    """POST run (dry_run=true) → verify no result persisted."""
    metric_id = f"{_TEST_METRIC_PREFIX}.run_dry"
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Dry Run Test",
                "description": "Test dry run",
                "theme": "quality",
                "measurement_query": {"type": "dataset_count"},
            },
        )
        assert resp.status_code == 200

        resp = await http_client.post(
            f"{_DG_PREFIX}/{metric_id}/method/run",
            headers=headers,
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        assert resp.json()["detail"]["dry_run"] is True

        # Verify no result persisted
        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}/attr/result",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_activate_deactivate(http_client, async_session: AsyncSession):
    """PUT config → POST deactivate → verify → POST activate → verify."""
    metric_id = f"{_TEST_METRIC_PREFIX}.activate_test"
    headers = _auth_headers()

    try:
        # Create active metric
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Activate Test",
                "description": "Activate/deactivate test",
                "theme": "governance",
                "measurement_query": {"type": "dataset_count"},
                "active": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is True

        # Deactivate
        resp = await http_client.post(
            f"{_DG_PREFIX}/{metric_id}/method/deactivate",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is False

        # Activate
        resp = await http_client.post(
            f"{_DG_PREFIX}/{metric_id}/method/activate",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is True
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE entity_id = :id AND entity_type = 'metric'"),
            {"id": metric_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_events_pagination(http_client, async_session: AsyncSession):
    """Seed events → GET with limit → verify total_count and page size."""
    metric_id = f"{_TEST_METRIC_PREFIX}.events_test"
    headers = _auth_headers()
    event_ids = []

    try:
        for i in range(3):
            eid = uuid.uuid4()
            event_ids.append(eid)
            await async_session.execute(
                text(
                    "INSERT INTO dataspoke.events"
                    " (id, entity_type, entity_id, event_type, status, detail, occurred_at)"
                    " VALUES (:id, :entity_type, :entity_id, :event_type,"
                    " :status, :detail, :occurred_at)"
                ),
                {
                    "id": str(eid),
                    "entity_type": "metric",
                    "entity_id": metric_id,
                    "event_type": "metric.run.completed",
                    "status": "success",
                    "detail": json.dumps({"run_id": str(uuid.uuid4()), "index": i}),
                    "occurred_at": datetime.now(tz=UTC),
                },
            )
        await async_session.commit()

        # Need metric definition to exist for the attr route
        await async_session.execute(
            text(
                "INSERT INTO dataspoke.metric_definitions"
                " (id, title, description, theme, measurement_query, active, alarm_enabled)"
                " VALUES (:id, :title, :desc, :theme, :mq, true, false)"
            ),
            {
                "id": metric_id,
                "title": "Events Test",
                "desc": "test",
                "theme": "quality",
                "mq": json.dumps({"type": "dataset_count"}),
            },
        )
        await async_session.commit()

        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["events"]) == 2
    finally:
        for eid in event_ids:
            await async_session.execute(
                text("DELETE FROM dataspoke.events WHERE id = :id"),
                {"id": str(eid)},
            )
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_metric_attr_endpoint(http_client, async_session: AsyncSession):
    """PUT config → GET attr → verify lightweight view."""
    metric_id = f"{_TEST_METRIC_PREFIX}.attr_test"
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Attr Test",
                "description": "Attr endpoint test",
                "theme": "freshness",
                "measurement_query": {"type": "dataset_count"},
                "alarm_enabled": True,
                "schedule": "*/5 * * * *",
            },
        )
        assert resp.status_code == 200

        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}/attr",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == metric_id
        assert body["title"] == "Attr Test"
        assert body["theme"] == "freshness"
        assert body["alarm_enabled"] is True
        assert body["latest_value"] is None
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_metric_not_found(http_client):
    """GET nonexistent metric → 404."""
    resp = await http_client.get(
        f"{_DG_PREFIX}/nonexistent.metric.id",
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
