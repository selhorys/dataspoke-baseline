"""Integration tests for MetricsService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient metric_definitions rows via PUT API (Imazon-prefixed test IDs).
- Transient metric_results rows from POST run.
- Transient dataspoke.events rows for event pagination and activate/deactivate tests.

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Dummy data ingested via conftest.py Python utilities
"""

import json

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import (
    _auth_headers,
    cleanup_events,
    override_app,
    seed_events,
)

_DG_PREFIX = "/api/v1/spoke/dg/metric"
_TEST_METRIC_PREFIX = "imazon.test.metrics"


@pytest_asyncio.fixture
async def http_client(datahub_client, mock_cache, async_session):
    async with override_app(datahub=datahub_client, redis=mock_cache, db=async_session) as client:
        yield client


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

    event_ids = await seed_events(
        async_session,
        entity_type="metric",
        entity_id=metric_id,
        event_type="metric.run.completed",
    )

    try:
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
        await cleanup_events(async_session, event_ids)
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
