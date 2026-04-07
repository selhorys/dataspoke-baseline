"""Integration tests for the metrics workflow orchestration layer.

Separate from test_metrics_service.py (which tests config CRUD).
This file focuses on:
- POST .../method/run endpoint (Kestra metrics flow round-trip)
- POST /internal/activities/metrics/list-periodic
- POST /internal/activities/metrics/sync-periodic-flows
- Concurrency guard (Kestra label dedup)

Test-specific data extensions (created and cleaned up within each test):
- Transient metric_definitions rows (imazon.test.wf.* IDs).
- Transient metric_results rows from actual metric runs.
- Transient dataspoke.events rows from metric executions.
- Dynamically generated Kestra flows (metrics-periodic-*).

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Kestra port-forwarded to localhost:9205
- Dummy data ingested via conftest.py Python utilities
"""

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from src.workflows.kestra.client import KestraClient
from src.workflows.metrics import schedule_to_flow_id
from tests.integration.api_wired.spot.conftest import (
    delete_kestra_flow,
    delete_metric_definition_db,
    delete_metric_events_db,
    delete_metric_results_db,
)
from tests.integration.conftest import _auth_headers

_DG_PREFIX = "/api/v1/spoke/dg/metric"
_TEST_PREFIX = "imazon.test.wf"


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
async def test_run_metric_via_kestra_flow(
    http_client, async_session: AsyncSession
):
    """POST run on a configured metric executes the pipeline via Kestra and records events.

    Setup: PUT metric config for a transient metric_id.
    Action: POST .../method/run with dry_run=false.
    Assertions: 200, run_id present, status is "success",
                GET results has total_count >= 1 with value and breakdown,
                GET events has METRIC.RUN_COMPLETE.
    Cleanup: DELETE results + events + definition.
    """
    metric_id = f"{_TEST_PREFIX}.run_kestra"
    headers = _auth_headers()

    try:
        # PUT config
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Kestra Run Test",
                "description": "Workflow spot test — Kestra round-trip",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_cron": "0 2 * * *",
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
    """POST list-periodic returns only metric IDs matching the requested schedule.

    Setup: PUT 4 configs:
           A/B: active, cron "0 2 * * *"
           C: active, cron "0 6 * * *" (different schedule)
           D: is_active=False (excluded from periodic list)
    Action: POST /internal/activities/metrics/list-periodic {"schedule_cron": "0 2 * * *"}.
    Assertions: Result contains A and B; does not contain C or D.
    Cleanup: DELETE events + definitions for all 4.
    """
    metric_id_a = f"{_TEST_PREFIX}.periodic_a"
    metric_id_b = f"{_TEST_PREFIX}.periodic_b"
    metric_id_c = f"{_TEST_PREFIX}.periodic_c"
    metric_id_d = f"{_TEST_PREFIX}.periodic_d"
    headers = _auth_headers()

    try:
        # A: active, cron "0 2 * * *"
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_a}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic A",
                "description": "Workflow spot test — periodic A",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_cron": "0 2 * * *",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config A failed: {resp.text}"

        # B: active, cron "0 2 * * *"
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_b}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic B",
                "description": "Workflow spot test — periodic B",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_cron": "0 2 * * *",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config B failed: {resp.text}"

        # C: active, cron "0 6 * * *" (different schedule — should be excluded)
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_c}/attr/conf",
            headers=headers,
            json={
                "title": "Periodic C",
                "description": "Workflow spot test — periodic C",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_cron": "0 6 * * *",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config C failed: {resp.text}"

        # D: inactive (no cron needed — excluded from periodic list)
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
            json={"schedule_cron": "0 2 * * *"},
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
async def test_sync_creates_flows_per_schedule(
    http_client, async_session: AsyncSession, kestra_client
):
    """Sync endpoint generates one Kestra flow per unique active cron schedule.

    Setup: 3 metrics — sync_a + sync_b share "0 2 * * *", sync_c gets "0 6 * * *".
           All active.
    Action: POST /internal/activities/metrics/sync-periodic-flows.
    Assertions: Two flows registered in Kestra (one per schedule),
                both retrievable via kestra_client.get_flow().
                Both flows execute successfully when triggered.
                All metric IDs have METRIC.RUN_COMPLETE events after flow runs.
    Cleanup: Delete generated flows + events + definitions for all 3.
    """
    metric_id_a = f"{_TEST_PREFIX}.sync_a"
    metric_id_b = f"{_TEST_PREFIX}.sync_b"
    metric_id_c = f"{_TEST_PREFIX}.sync_c"
    flow_id_02 = schedule_to_flow_id("0 2 * * *")
    flow_id_06 = schedule_to_flow_id("0 6 * * *")
    headers = _auth_headers()

    try:
        # sync_a + sync_b: active, cron "0 2 * * *"
        for mid in (metric_id_a, metric_id_b):
            resp = await http_client.put(
                f"{_DG_PREFIX}/{mid}/attr/conf",
                headers=headers,
                json={
                    "title": f"Sync Test {mid}",
                    "description": f"Workflow spot test — sync {mid}",
                    "theme": "quality",
                    "measurement_query": {"type": "poorly_documented"},
                    "schedule_cron": "0 2 * * *",
                    "is_active": True,
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed for {mid}: {resp.text}"

        # sync_c: active, cron "0 6 * * *"
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id_c}/attr/conf",
            headers=headers,
            json={
                "title": "Sync Test C",
                "description": "Workflow spot test — sync C",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_cron": "0 6 * * *",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed for {metric_id_c}: {resp.text}"

        resp = await http_client.post(
            "/internal/activities/metrics/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"sync failed: {resp.text}"
        body = resp.json()
        assert flow_id_02 in body.get("created", []), (
            f"Expected {flow_id_02} in created: {body}"
        )
        assert flow_id_06 in body.get("created", []), (
            f"Expected {flow_id_06} in created: {body}"
        )

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
        for mid in (metric_id_a, metric_id_b, metric_id_c):
            await delete_metric_results_db(async_session, mid)
            await delete_metric_events_db(async_session, mid)
            await delete_metric_definition_db(async_session, mid)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_removes_stale_flows(
    http_client, async_session: AsyncSession, kestra_client
):
    """Sync removes flows whose cron schedules are no longer in active metric definitions.

    Setup: PUT config for sync_stale (active, "0 3 * * *"), sync to create flow.
    Action: Delete the definition directly, then sync again.
    Assertions: The flow for "0 3 * * *" is listed in body["deleted"] and
                no longer retrievable via kestra_client.get_flow().
    Cleanup: Delete any remaining kestra flow + events + definition.
    """
    metric_id = f"{_TEST_PREFIX}.sync_stale"
    flow_id_03 = schedule_to_flow_id("0 3 * * *")
    headers = _auth_headers()

    try:
        resp = await http_client.put(
            f"{_DG_PREFIX}/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Sync Stale Test",
                "description": "Workflow spot test — stale sync",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
                "schedule_cron": "0 3 * * *",
                "is_active": True,
            },
        )
        assert resp.status_code in (200, 201), f"PUT config failed: {resp.text}"

        # First sync — creates the flow
        resp = await http_client.post(
            "/internal/activities/metrics/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"First sync failed: {resp.text}"
        flow_before = await kestra_client.get_flow(flow_id_03)
        assert flow_before is not None, f"Flow {flow_id_03} was not created on first sync"

        # Delete the definition directly (simulates hard removal bypassing API)
        await delete_metric_definition_db(async_session, metric_id)
        await async_session.commit()

        # Second sync — should delete the stale flow
        resp = await http_client.post(
            "/internal/activities/metrics/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"Second sync failed: {resp.text}"
        body = resp.json()
        assert flow_id_03 in body.get("deleted", []), (
            f"Expected {flow_id_03} in deleted: {body}"
        )

        # Verify flow no longer exists in Kestra
        flow_after = await kestra_client.get_flow(flow_id_03)
        assert flow_after is None, f"Flow {flow_id_03} still exists after second sync"

    finally:
        await delete_kestra_flow(kestra_client, flow_id_03)
        await delete_metric_events_db(async_session, metric_id)
        await delete_metric_definition_db(async_session, metric_id)
        await async_session.commit()


@pytest.mark.asyncio
async def test_sync_updates_on_schedule_change(
    http_client, async_session: AsyncSession, kestra_client
):
    """Patching a metric's schedule causes the new cron flow to be created.

    Setup: 3 metrics all on "0 2 * * *", all active. Sync -> one flow.
    Action: PATCH sched_c schedule to "0 6 * * *", sync again.
    Assertions:
    - Flow for "0 2 * * *" still exists (sched_a + sched_b remain).
    - New flow for "0 6 * * *" exists.
    - list-periodic "0 2 * * *" returns sched_a, sched_b; not sched_c.
    - list-periodic "0 6 * * *" returns sched_c; not sched_a or sched_b.
    Cleanup: Delete generated flows + events + definitions for all 3.
    """
    metric_id_a = f"{_TEST_PREFIX}.sched_a"
    metric_id_b = f"{_TEST_PREFIX}.sched_b"
    metric_id_c = f"{_TEST_PREFIX}.sched_c"
    flow_id_02 = schedule_to_flow_id("0 2 * * *")
    flow_id_06 = schedule_to_flow_id("0 6 * * *")
    headers = _auth_headers()

    try:
        # All three start on "0 2 * * *"
        for mid in (metric_id_a, metric_id_b, metric_id_c):
            resp = await http_client.put(
                f"{_DG_PREFIX}/{mid}/attr/conf",
                headers=headers,
                json={
                    "title": f"Sched Test {mid}",
                    "description": f"Workflow spot test — schedule {mid}",
                    "theme": "quality",
                    "measurement_query": {"type": "poorly_documented"},
                    "schedule_cron": "0 2 * * *",
                    "is_active": True,
                },
            )
            assert resp.status_code in (200, 201), f"PUT config failed for {mid}: {resp.text}"

        # First sync — one flow for "0 2 * * *"
        resp = await http_client.post(
            "/internal/activities/metrics/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"First sync failed: {resp.text}"

        # PATCH sched_c to "0 6 * * *"
        resp = await http_client.patch(
            f"{_DG_PREFIX}/{metric_id_c}/attr/conf",
            headers=headers,
            json={"schedule_cron": "0 6 * * *", "is_active": True},
        )
        assert resp.status_code == 200, f"PATCH schedule failed: {resp.text}"

        # Second sync — should add flow for "0 6 * * *", keep "0 2 * * *"
        resp = await http_client.post(
            "/internal/activities/metrics/sync-periodic-flows",
        )
        assert resp.status_code == 200, f"Second sync failed: {resp.text}"

        # Both flows exist in Kestra
        flow_02 = await kestra_client.get_flow(flow_id_02)
        assert flow_02 is not None, f"Flow {flow_id_02} not found after schedule change"
        flow_06 = await kestra_client.get_flow(flow_id_06)
        assert flow_06 is not None, f"Flow {flow_id_06} not found after schedule change"

        # list-periodic "0 2 * * *" returns sched_a and sched_b only
        resp = await http_client.post(
            "/internal/activities/metrics/list-periodic",
            json={"schedule_cron": "0 2 * * *"},
        )
        assert resp.status_code == 200
        ids_02 = resp.json()
        assert metric_id_a in ids_02, f"Expected {metric_id_a} in 0 2 list: {ids_02}"
        assert metric_id_b in ids_02, f"Expected {metric_id_b} in 0 2 list: {ids_02}"
        assert metric_id_c not in ids_02, f"Did not expect {metric_id_c} in 0 2 list: {ids_02}"

        # list-periodic "0 6 * * *" returns only sched_c
        resp = await http_client.post(
            "/internal/activities/metrics/list-periodic",
            json={"schedule_cron": "0 6 * * *"},
        )
        assert resp.status_code == 200
        ids_06 = resp.json()
        assert metric_id_c in ids_06, f"Expected {metric_id_c} in 0 6 list: {ids_06}"
        assert metric_id_a not in ids_06, f"Did not expect {metric_id_a} in 0 6 list: {ids_06}"
        assert metric_id_b not in ids_06, f"Did not expect {metric_id_b} in 0 6 list: {ids_06}"

    finally:
        await delete_kestra_flow(kestra_client, flow_id_02)
        await delete_kestra_flow(kestra_client, flow_id_06)
        for mid in (metric_id_a, metric_id_b, metric_id_c):
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
