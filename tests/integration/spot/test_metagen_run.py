"""Spot tests for Metadata Generation — run-method gating and event endpoints.

Concerns covered (6 test functions across 2 groups):

Run-method gating (Group 3):
  test_metagen_run_disabled_conf_non_dry_run_returns_409_METAGEN_DISABLED
  test_metagen_run_dry_run_permitted_when_disabled
  test_metagen_run_concurrent_returns_409_METAGEN_RUNNING
  test_metagen_run_empty_scope_completes_with_zero_items

Event endpoints (Group 6):
  test_metagen_global_event_list_envelope_filters_by_time
  test_metagen_dataset_event_list_envelope

NOTE (concurrent run test): The MetagenService serialises concurrency via a
Redis cache lock ("metagen:running:singleton"), not a DB table — there is no
metagen_runs table.  The test pre-sets the Redis key directly via the
redis_client fixture to simulate an in-progress run, then calls POST run.

spec: USE_CASE_en.md §UC4 (L552-776)
spec: BACKEND.md §UC4 Metadata Generation — run pipeline, event catalogue
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import urllib.parse
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.util.metagen import seed_metagen_event

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master
# into DataHub + PG before any tests run.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Primary test dataset — catalog.title_master (Imazon UC4 table).
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_TEST_URN2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.book_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")
_ENCODED_URN2 = urllib.parse.quote(_TEST_URN2, safe="")


# ── Group 3: Run-method gating ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_run_disabled_conf_non_dry_run_returns_409_METAGEN_DISABLED(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Non-dry run with is_enabled=false returns 409 METAGEN_DISABLED.

    No METAGEN.RUN_COMPLETE event must be emitted for this rejected call.

    spec: USE_CASE_en.md §UC4 L774 — disabled guard: non-dry run rejected
      when conf.is_enabled=false
    spec: BACKEND.md L949 — error-code table: METAGEN_DISABLED -> 409
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    event_url = "/api/v1/spoke/common/metagen/event"

    try:
        # Ensure conf exists with is_enabled=false
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 3,
            },
        )

        # Capture current time before the rejected POST so we can assert no
        # RUN_COMPLETE event was emitted *by this call*.
        time_before_post = datetime.now(tz=UTC)

        run_resp = await api_client.post(
            run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 409, (
            f"Non-dry run with is_enabled=false must return 409; "
            f"got {run_resp.status_code} {run_resp.text}. "
            "spec: BACKEND.md L949 — METAGEN_DISABLED -> 409"
        )
        run_body = run_resp.json()
        # Error code must be METAGEN_DISABLED
        assert "METAGEN_DISABLED" in str(run_body), (
            f"409 response must carry METAGEN_DISABLED code; got {run_body!r}. "
            "spec: BACKEND.md L949 — error-code table"
        )

        # No RUN_COMPLETE event must have been emitted for this rejected call.
        # Assert by time: no METAGEN.RUN_COMPLETE event has occurred_at >= time_before_post.
        # spec: BACKEND.md §UC4 — METAGEN.RUN_COMPLETE only emitted on completed runs
        ev_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert ev_resp.status_code == 200
        events_after = ev_resp.json().get("events", [])
        stale_run_complete = [
            e for e in events_after
            if e["event_type"] == "METAGEN.RUN_COMPLETE"
            and datetime.fromisoformat(e["occurred_at"]) >= time_before_post
        ]
        assert len(stale_run_complete) == 0, (
            f"No METAGEN.RUN_COMPLETE event should be emitted when run was rejected "
            f"(occurred_at >= {time_before_post.isoformat()}); "
            f"found {stale_run_complete!r}. spec: BACKEND.md §UC4"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_run_dry_run_permitted_when_disabled(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """dry_run=true is permitted even when is_enabled=false; event detail has dry_run=true.

    Counts shape for dry runs: {items_considered, candidates_proposed}.

    spec: USE_CASE_en.md §UC4 L660-662 — dry_run=true bypasses the disabled guard
    spec: BACKEND.md §766 — dry-run response counts shape: items_considered,
      candidates_proposed (not candidates_added); dry_run=true in RUN_COMPLETE detail
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    event_url = "/api/v1/spoke/common/metagen/event"

    try:
        # Conf disabled, dataset_filter scoped to _TEST_URN
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 3,
            },
        )

        run_resp = await api_client.post(
            run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert run_resp.status_code == 200, (
            f"dry_run=true with is_enabled=false must return 200; "
            f"got {run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L660-662"
        )
        run_body = run_resp.json()
        run_id = run_body.get("run_id")

        # dry_run must be echoed as True
        assert run_body.get("dry_run") is True, (
            f"MetagenRunResponse dry_run must be True; got {run_body.get('dry_run')!r}. "
            "spec: BACKEND.md §766"
        )
        # Dry-run counts shape: items_considered + candidates_proposed (NOT candidates_added)
        counts = run_body.get("counts", {})
        assert isinstance(counts, dict), (
            "MetagenRunResponse counts must be dict. spec: BACKEND.md §766"
        )
        assert "items_considered" in counts, (
            "Dry-run counts must contain 'items_considered'. "
            "spec: BACKEND.md §766 — dry-run counts shape"
        )
        assert "candidates_proposed" in counts, (
            "Dry-run counts must contain 'candidates_proposed' (not candidates_added). "
            "spec: BACKEND.md §766 — dry-run counts shape"
        )
        assert "candidates_added" not in counts, (
            "Dry-run counts must NOT contain 'candidates_added'. "
            "spec: BACKEND.md §766 — dry-run counts shape"
        )
        # unresolved_urns: present and is a list (may be empty for zero-scope dry run)
        assert isinstance(run_body.get("unresolved_urns"), list), (
            f"MetagenRunResponse unresolved_urns must be a list; "
            f"got {run_body.get('unresolved_urns')!r}. "
            "spec: BACKEND.md §UC4 — MetagenRunResponse schema"
        )
        # producer_iterations and debate_outcome keys must be present
        # (may be None for a dry run with empty in-scope; spec does not require populated values)
        assert "producer_iterations" in run_body, (
            "MetagenRunResponse must carry producer_iterations key. "
            "spec: BACKEND.md §UC4 — MetagenRunResponse schema"
        )
        assert "debate_outcome" in run_body, (
            "MetagenRunResponse must carry debate_outcome key. "
            "spec: BACKEND.md §UC4 — MetagenRunResponse schema"
        )

        # RUN_COMPLETE event must be emitted for dry run; bind to this run's run_id.
        # spec: BACKEND.md §766 — RUN_COMPLETE emitted unconditionally (dry and non-dry)
        ev_resp = await api_client.get(f"{event_url}?limit=50", headers=admin_headers)
        assert ev_resp.status_code == 200
        events = ev_resp.json().get("events", [])
        dry_run_event = next(
            (
                e for e in events
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e["detail"].get("run_id") == run_id
            ),
            None,
        )
        assert dry_run_event is not None, (
            f"METAGEN.RUN_COMPLETE event for run_id={run_id!r} must be emitted for dry run. "
            "spec: BACKEND.md §766 event catalogue"
        )
        assert dry_run_event["detail"].get("dry_run") is True, (
            f"RUN_COMPLETE detail dry_run must be True; "
            f"got {dry_run_event['detail'].get('dry_run')!r}. "
            "spec: BACKEND.md §766 event catalogue"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_run_concurrent_returns_409_METAGEN_RUNNING(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    redis_client,
) -> None:
    """A run while another is in-progress returns 409 METAGEN_RUNNING.

    The MetagenService serialises runs via a Redis cache lock (set_nx on
    'metagen:running:singleton'). We pre-set that Redis key directly via the
    redis_client fixture to simulate an in-progress run before calling POST run.
    The service's set_nx will then return False and raise
    ConflictError('METAGEN_RUNNING').

    spec: USE_CASE_en.md §UC4 L659-660 — concurrent run guard
    spec: BACKEND.md L949 — error-code table: METAGEN_RUNNING -> 409
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    lock_key = "metagen:running:singleton"
    fake_token = f"spot-test-concurrent-{uuid.uuid4().hex[:8]}"

    try:
        # Ensure conf exists with is_enabled=true so METAGEN_DISABLED is not raised first.
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 3,
            },
        )

        # Pre-set the Redis lock key to simulate a running job.
        # set_nx returns True if key was absent (we acquired it),
        # or False if already held (another test is running — still fine,
        # the POST run will 409 either way).
        await redis_client.set_nx(lock_key, fake_token, ttl_seconds=60)

        run_resp = await api_client.post(run_url, headers=admin_headers)
        assert run_resp.status_code == 409, (
            f"POST run while lock is held must return 409; "
            f"got {run_resp.status_code} {run_resp.text}. "
            "spec: BACKEND.md L949 — METAGEN_RUNNING -> 409"
        )
        assert "METAGEN_RUNNING" in str(run_resp.json()), (
            f"409 response must carry METAGEN_RUNNING code; got {run_resp.json()!r}. "
            "spec: BACKEND.md L949 — error-code table"
        )

    finally:
        # Release the Redis lock and clean up conf.
        with suppress(Exception):
            await redis_client.delete(lock_key)
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_run_empty_scope_completes_with_zero_items(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Run with no enabled boundary in scope returns RUN_COMPLETE with items_considered=0.

    Enable conf with dataset_filter scoped to _TEST_URN but do NOT set any
    boundary (or ensure it is absent). The intersection of DataHub URN set
    and metagen_boundary.is_enabled=true rows is empty -> items_considered=0.

    spec: USE_CASE_en.md §UC4 L613 — boundary controls per-dataset scope;
      absent boundary means dataset not in scope
    spec: BACKEND.md §UC4 — RUN_COMPLETE emitted even for zero-item runs;
      counts.items_considered == 0
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    event_url = "/api/v1/spoke/common/metagen/event"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    try:
        # Enable conf but scope to _TEST_URN; explicitly delete any boundary
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 3,
            },
        )
        # Ensure no boundary exists (suppress 404 if absent)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)

        run_resp = await api_client.post(run_url, headers=admin_headers)
        assert run_resp.status_code == 200, (
            f"POST run with no enabled boundary must return 200; "
            f"got {run_resp.status_code} {run_resp.text}. "
            "spec: BACKEND.md §UC4"
        )
        run_body = run_resp.json()
        run_id = run_body.get("run_id")
        assert run_body.get("status") == "success", (
            f"run status must be 'success'; got {run_body.get('status')!r}. "
            "spec: BACKEND.md §UC4"
        )
        counts = run_body.get("counts", {})
        assert isinstance(counts, dict), (
            "MetagenRunResponse counts must be dict. spec: BACKEND.md §UC4"
        )
        assert "items_considered" in counts, (
            "counts must contain 'items_considered'. spec: BACKEND.md §UC4"
        )
        assert counts["items_considered"] == 0, (
            f"items_considered must be 0 with no enabled boundary; "
            f"got {counts.get('items_considered')!r}. "
            "spec: USE_CASE_en.md §UC4 L613 — absent boundary -> not in scope"
        )

        # RUN_COMPLETE event recorded with items_considered=0; bind to this run's run_id.
        ev_resp = await api_client.get(f"{event_url}?limit=50", headers=admin_headers)
        assert ev_resp.status_code == 200
        events = ev_resp.json().get("events", [])
        run_complete = next(
            (
                e for e in events
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e["detail"].get("run_id") == run_id
            ),
            None,
        )
        assert run_complete is not None, (
            f"METAGEN.RUN_COMPLETE for run_id={run_id!r} must be emitted for zero-item run. "
            "spec: BACKEND.md §UC4 event catalogue"
        )
        assert run_complete["detail"].get("counts", {}).get("items_considered") == 0, (
            f"RUN_COMPLETE detail counts.items_considered must be 0; "
            f"got {run_complete['detail'].get('counts')!r}. "
            "spec: BACKEND.md §UC4"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


# ── Group 6: Event endpoints ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_global_event_list_envelope_filters_by_time(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/event with 'after' param filters events by occurred_at.

    Seeds two METAGEN.RUN_COMPLETE events with controlled occurred_at timestamps;
    verifies that the 'after' query parameter on the global event endpoint
    correctly excludes events older than the cutoff.

    NOTE: The global metagen event endpoint uses 'after' (not 'from'/'to') as
    its time-filter parameter — per route signature at
    src/api/routers/spoke/common/metagen.py L139.

    spec: USE_CASE_en.md §UC4 L682 — GET /spoke/common/metagen/event (global event history)
    spec: API.md §Standard Envelope — events, offset, limit, total_count
    spec: src/api/routers/spoke/common/metagen.py L137-178 — GET /metagen/event
      with optional 'after' query param; EventListResponse envelope
    """
    event_url = "/api/v1/spoke/common/metagen/event"

    # Two timestamps: older (yesterday) and newer (5s ago)
    now = datetime.now(tz=UTC)
    older_time = now - timedelta(days=1)
    newer_time = now - timedelta(seconds=5)

    older_event_id: str | None = None
    newer_event_id: str | None = None

    try:
        # Seed two METAGEN.RUN_COMPLETE events — entity_type='metagen' required
        # by the global endpoint's WHERE clause.
        older_event_id = await seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id="singleton",
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "dry_run": False, "counts": {}},
            occurred_at=older_time,
        )
        newer_event_id = await seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id="singleton",
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "dry_run": False, "counts": {}},
            occurred_at=newer_time,
        )

        # GET all (no time filter) — verify envelope shape
        all_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert all_resp.status_code == 200, (
            f"GET /metagen/event failed: {all_resp.status_code}"
        )
        all_body = all_resp.json()
        assert "events" in all_body and isinstance(all_body["events"], list), (
            "EventListResponse must have 'events' list. spec: API.md §Standard Envelope"
        )
        assert "total_count" in all_body and isinstance(all_body["total_count"], int), (
            "EventListResponse must have 'total_count'. spec: API.md §Standard Envelope"
        )
        assert "offset" in all_body and "limit" in all_body, (
            "EventListResponse must have offset and limit. spec: API.md §Standard Envelope"
        )

        # Both seeded events must appear in unfiltered list
        all_ids = {e["id"] for e in all_body["events"]}
        assert older_event_id in all_ids, (
            f"Older seeded event {older_event_id!r} not found in unfiltered list."
        )
        assert newer_event_id in all_ids, (
            f"Newer seeded event {newer_event_id!r} not found in unfiltered list."
        )

        # GET with 'after' set to a cutoff between the two events
        # (older_time + 30min < cutoff < newer_time)
        cutoff = (older_time + timedelta(minutes=30)).isoformat()
        filtered_resp = await api_client.get(
            f"{event_url}?after={urllib.parse.quote(cutoff, safe='')}&limit=100",
            headers=admin_headers,
        )
        assert filtered_resp.status_code == 200, (
            f"GET /metagen/event?after failed: {filtered_resp.status_code}"
        )
        filtered_ids = {e["id"] for e in filtered_resp.json().get("events", [])}
        assert newer_event_id in filtered_ids, (
            f"Newer event must appear with after filter; got {filtered_ids!r}. "
            "spec: src/api/routers/spoke/common/metagen.py L151"
        )
        assert older_event_id not in filtered_ids, (
            f"Older event must be excluded by after filter; got {filtered_ids!r}. "
            "spec: src/api/routers/spoke/common/metagen.py L151"
        )

    finally:
        for eid in (older_event_id, newer_event_id):
            if eid is not None:
                with suppress(Exception):
                    await async_session.execute(
                        text("DELETE FROM dataspoke.events WHERE id = CAST(:id AS uuid)"),
                        {"id": eid},
                    )
                    await async_session.commit()


@pytest.mark.asyncio
async def test_metagen_dataset_event_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /data/{urn}/event/metagen returns METAGEN.* events for that URN only.

    Seeds a METAGEN.CANDIDATE_APPROVE event for _TEST_URN and one for _TEST_URN2;
    verifies:
      - Proper EventListResponse envelope (events, offset, limit, total_count)
      - Only _TEST_URN events appear (entity_id filter applied)
      - Only METAGEN.* event_types returned (prefix filter)
      - 'from' query param in the future excludes the seeded event

    spec: USE_CASE_en.md §UC4 L689 — GET /spoke/common/data/{urn}/event/metagen
      (per-dataset metagen events: METAGEN.CANDIDATE_APPROVE, METAGEN.CANDIDATE_REJECT)
    spec: API.md §Standard Envelope — events, offset, limit, total_count
    spec: src/api/routers/spoke/common/data/metagen.py L187-232 —
      GET /{dataset_urn}/event/metagen with from/to params
    spec: src/shared/events.py — METAGEN_CANDIDATE_APPROVE event type constant
    """
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    now = datetime.now(tz=UTC)
    event_id_1: str | None = None
    event_id_2: str | None = None

    try:
        # Seed METAGEN.CANDIDATE_APPROVE for _TEST_URN
        event_id_1 = await seed_metagen_event(
            async_session,
            entity_type="dataset",
            entity_id=_TEST_URN,
            event_type="METAGEN.CANDIDATE_APPROVE",
            detail={
                "item_id": "dataset.description",
                "candidate_id": str(uuid.uuid4()),
                "reason": "spot test",
            },
            occurred_at=now - timedelta(seconds=10),
        )
        # Seed same event type for _TEST_URN2 (must NOT appear on URN1's endpoint)
        event_id_2 = await seed_metagen_event(
            async_session,
            entity_type="dataset",
            entity_id=_TEST_URN2,
            event_type="METAGEN.CANDIDATE_APPROVE",
            detail={
                "item_id": "dataset.description",
                "candidate_id": str(uuid.uuid4()),
                "reason": "other urn",
            },
            occurred_at=now - timedelta(seconds=5),
        )

        # GET per-dataset events for _TEST_URN
        resp = await api_client.get(f"{dataset_event_url}?limit=50", headers=admin_headers)
        assert resp.status_code == 200, (
            f"GET per-dataset metagen events failed: {resp.status_code} {resp.text}. "
            "spec: src/api/routers/spoke/common/data/metagen.py L187"
        )
        body = resp.json()

        # Envelope keys
        assert "events" in body and isinstance(body["events"], list), (
            "EventListResponse must have 'events' list. spec: API.md §Standard Envelope"
        )
        assert "total_count" in body and isinstance(body["total_count"], int), (
            "EventListResponse must have 'total_count'. spec: API.md §Standard Envelope"
        )
        assert "offset" in body and "limit" in body, (
            "EventListResponse must have offset and limit. spec: API.md §Standard Envelope"
        )

        returned_ids = {e["id"] for e in body["events"]}
        # _TEST_URN event must appear
        assert event_id_1 in returned_ids, (
            f"Seeded event for _TEST_URN must appear in per-dataset events; "
            f"got {returned_ids!r}"
        )
        # _TEST_URN2 event must NOT appear on _TEST_URN endpoint
        assert event_id_2 not in returned_ids, (
            f"Event for _TEST_URN2 must NOT appear on _TEST_URN per-dataset endpoint; "
            f"got {returned_ids!r}. "
            "spec: src/api/routers/spoke/common/data/metagen.py L200 — "
            "entity_id == dataset_urn filter"
        )

        # All returned events must be METAGEN.* types
        for ev in body["events"]:
            assert ev["event_type"].startswith("METAGEN."), (
                f"Per-dataset metagen event endpoint must only return METAGEN.* events; "
                f"got {ev['event_type']!r}. "
                "spec: src/api/routers/spoke/common/data/metagen.py L201"
            )

        # 'from' filter in the future excludes the seeded event
        future_from = (now + timedelta(hours=1)).isoformat()
        filtered_resp = await api_client.get(
            f"{dataset_event_url}?from={urllib.parse.quote(future_from, safe='')}&limit=50",
            headers=admin_headers,
        )
        assert filtered_resp.status_code == 200
        filtered_ids = {e["id"] for e in filtered_resp.json().get("events", [])}
        assert event_id_1 not in filtered_ids, (
            f"Seeded event must be excluded by from= filter set in the future; "
            f"got {filtered_ids!r}. "
            "spec: src/api/routers/spoke/common/data/metagen.py L206-208"
        )

    finally:
        for eid in (event_id_1, event_id_2):
            if eid is not None:
                with suppress(Exception):
                    await async_session.execute(
                        text("DELETE FROM dataspoke.events WHERE id = CAST(:id AS uuid)"),
                        {"id": eid},
                    )
                    await async_session.commit()


@pytest.mark.asyncio
async def test_metagen_dry_run_with_origin_filter_does_not_raise(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Dry-run with dataset_filter={"origin": "DEV"} completes without error.

    When no DataHub datasets match origin+boundary intersection, the resolver
    returns an empty scope cleanly (swallow_enumerate_errors=True for UC4).
    The run must succeed with empty scope, not raise a 500.

    spec: spec/feature/BACKEND.md §UC4 dataset_filter — resolver returns empty scope
          cleanly when no datasets match the origin filter.
    spec: USE_CASE_en.md §UC4 §Run semantics — dry_run=true with empty scope completes.
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"

    try:
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV"},
                "result_limit": 3,
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT metagen conf with origin filter failed: {put_resp.status_code} {put_resp.text}"
        )

        run_resp = await api_client.post(
            run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert run_resp.status_code == 200, (
            f"Dry-run with origin=DEV filter must return 200; "
            f"got {run_resp.status_code}: {run_resp.text}. "
            "spec: BACKEND.md §UC4 dataset_filter — empty scope must not raise"
        )
        body = run_resp.json()
        assert body.get("dry_run") is True, (
            "Run response dry_run must be True. spec: USE_CASE_en.md §UC4"
        )
        assert isinstance(body.get("unresolved_urns"), list), (
            "Run response must have unresolved_urns list. spec: USE_CASE_en.md §UC4"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)
