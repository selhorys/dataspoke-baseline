"""Integration tests for the metrics workflow orchestration layer.

Separate from test_metrics_service.py (which tests config CRUD).
This file focuses on:
- POST .../method/run endpoint (Airflow metrics DAG round-trip)
- POST /internal/activities/metrics/list-periodic
- Concurrency guard (Airflow label dedup)

Test-specific data extensions (created and cleaned up within each test):
- Transient metric_definitions rows (imazon.test.wf.* IDs).
- Transient metric_results rows from actual metric runs.
- Transient dataspoke.events rows from metric executions.

Prerequisites:
- PostgreSQL accessible via DATASPOKE_DEV_PG_HOST/PORT
- DataHub GMS accessible via DATASPOKE_DATAHUB_GMS_URL
- Dummy data ingested via conftest.py Python utilities
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.api_wired.spot.conftest import (
    delete_metric_definition_db,
    delete_metric_events_db,
    delete_metric_results_db,
)
from tests.integration.conftest import _auth_headers

_DG_PREFIX = "/api/v1/spoke/dg/metric"
_TEST_PREFIX = "imazon.test.wf"


# ── Test cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_metric_via_airflow_dag(
    http_client, async_session: AsyncSession
):
    """POST run on a configured metric executes the pipeline via Airflow and records events.

    Setup: PUT metric config for a transient metric_id.
    Action: POST .../method/run with dry_run=false.
    Assertions: 200, run_id present, status is "success",
                GET results has total_count >= 1 with value and breakdown,
                GET events has METRIC.RUN_COMPLETE.
    Cleanup: DELETE results + events + definition.
    """
    metric_id = f"{_TEST_PREFIX}.run_airflow"
    headers = _auth_headers()

    try:
        # PUT config
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Airflow Run Test",
                "description": "Workflow spot test — Airflow round-trip",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_tier": "daily",
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # POST run
        resp = await http_client.post(
            f"{_DG_PREFIX}/{metric_id}/method/run",
            headers=headers,
            json={"dry_run": False},
        )
        assert resp.status_code == 200, f"Run failed: {resp.text}"
        body = resp.json()
        assert "run_id" in body
        assert body["status"].lower() == "success"

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

        # Verify side-effect events
        resp = await http_client.get(
            f"{_DG_PREFIX}/{metric_id}/event",
            headers=headers,
        )
        assert resp.status_code == 200
        event_types = [e["event_type"] for e in resp.json()["events"]]
        assert "METRIC.RUN_COMPLETE" in event_types, (
            f"Expected METRIC.RUN_COMPLETE in events, got {event_types}"
        )

    finally:
        await delete_metric_results_db(async_session, metric_id)
        await delete_metric_events_db(async_session, metric_id)
        await delete_metric_definition_db(async_session, metric_id)
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_periodic_metrics(
    http_client, async_session: AsyncSession
):
    """POST list-periodic returns only metric IDs matching the requested schedule tier.

    Setup: PUT 4 configs:
           A/B: active, schedule_tier="daily"
           C: active, schedule_tier="weekly" (different tier)
           D: is_active=False (excluded from periodic list)
    Action: POST /internal/activities/metrics/list-periodic {"schedule_tier": "daily"}.
    Assertions: Result contains A and B; does not contain C or D.
    Cleanup: DELETE events + definitions for all 4.
    """
    metric_id_a = f"{_TEST_PREFIX}.periodic_a"
    metric_id_b = f"{_TEST_PREFIX}.periodic_b"
    metric_id_c = f"{_TEST_PREFIX}.periodic_c"
    metric_id_d = f"{_TEST_PREFIX}.periodic_d"
    headers = _auth_headers()

    try:
        # A: active, schedule_tier="daily"
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_a}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic A",
                "description": "Workflow spot test — periodic A",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_tier": "daily",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config A failed: {resp.text}"

        # B: active, schedule_tier="daily"
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_b}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic B",
                "description": "Workflow spot test — periodic B",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_tier": "daily",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config B failed: {resp.text}"

        # C: active, schedule_tier="weekly" (different tier — should be excluded)
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_c}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic C",
                "description": "Workflow spot test — periodic C",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_tier": "weekly",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config C failed: {resp.text}"

        # D: inactive (no tier needed — excluded from periodic list)
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_d}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic D",
                "description": "Workflow spot test — periodic D",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "is_active": False,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config D failed: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/metrics/list-periodic",
            json={"schedule_tier": "daily"},
        )
        assert resp.status_code == 200, f"list-periodic failed: {resp.text}"
        result = resp.json()

        assert metric_id_a in result, f"Expected {metric_id_a} in result: {result}"
        assert metric_id_b in result, f"Expected {metric_id_b} in result: {result}"
        assert metric_id_c not in result, f"Did not expect {metric_id_c} in result: {result}"
        assert metric_id_d not in result, f"Did not expect {metric_id_d} in result: {result}"

    finally:
        for mid in (metric_id_a, metric_id_b, metric_id_c, metric_id_d):
            await delete_metric_events_db(async_session, mid)
            await delete_metric_definition_db(async_session, mid)
        await async_session.commit()


@pytest.mark.asyncio
async def test_concurrency_guard_prevents_duplicate(
    http_client, async_session: AsyncSession
):
    """Concurrent run requests for the same metric are rejected with 409.

    Setup: PUT metric config for a transient metric_id.
    Action: Fire two concurrent POST .../method/run requests via asyncio.gather.
    Assertions: One 200, one 409 with error_code "METRIC_RUNNING".
    Cleanup: DELETE results + events + definition.
    """
    metric_id = f"{_TEST_PREFIX}.concurrency"
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Concurrency Guard Test",
                "description": "Workflow spot test — concurrency guard",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # Fire both requests concurrently; the second should race into a locked state
        async def _run():
            return await http_client.post(
                f"{_DG_PREFIX}/{metric_id}/method/run",
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
        assert body.get("error_code") == "METRIC_RUNNING", f"Unexpected error body: {body}"

    finally:
        await delete_metric_results_db(async_session, metric_id)
        await delete_metric_events_db(async_session, metric_id)
        await delete_metric_definition_db(async_session, metric_id)
        await async_session.commit()
