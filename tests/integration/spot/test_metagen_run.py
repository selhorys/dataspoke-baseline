"""Spot tests for Metadata Generation — per-conf run gating, multi-conf isolation, events.

Concerns covered:

Per-conf run gating:
  test_metagen_run_disabled_conf_non_dry_run_returns_409_metagen_disabled
  test_metagen_run_dry_run_permitted_when_disabled
  test_metagen_run_missing_conf_returns_404
  test_metagen_run_concurrent_same_conf_returns_409_metagen_running
  test_metagen_run_empty_scope_completes_with_zero_items

Multi-conf isolation:
  test_two_confs_coexist_with_isolated_events_and_budgets

Event endpoints:
  test_metagen_conf_event_filters_by_conf_id
  test_metagen_conf_event_time_range_narrows_to_the_inclusive_window
  test_metagen_event_time_range_bounds_are_inclusive
  test_metagen_global_event_union_across_confs
  test_metagen_dataset_event_list_envelope

Confs are created over the REST collection API; budget/event isolation is the
concern, which the api-wired pipeline reaches only with two confs already
present — so the multi-conf seeding lives here.

spec: API.md §Metadata Generation (/spoke/metagen)
spec: feature/BACKEND.md §Metadata Generation Service — per-conf lock, fan-out, events
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

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_TEST_URN2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

_CONF_URL = "/api/v1/spoke/metagen/conf"


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _create_conf(
    api_client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    is_enabled: bool,
    dataset_filter: dict,  # type: ignore[type-arg]
    name: str | None = None,
) -> str:
    """Create a conf via REST and return its id. Inlined payload for readability."""
    resp = await api_client.post(
        _CONF_URL,
        headers=headers,
        json={
            "name": name or _unique_name("spot"),
            "is_enabled": is_enabled,
            "dataset_filter": dataset_filter,
            "result_limit": 3,
        },
    )
    assert resp.status_code == 201, f"conf create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _delete_conf(
    api_client: httpx.AsyncClient, headers: dict[str, str], conf_id: str
) -> None:
    with suppress(Exception):
        await api_client.delete(f"{_CONF_URL}/{conf_id}", headers=headers)


# ── Per-conf run gating ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_run_disabled_conf_non_dry_run_returns_409_metagen_disabled(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Non-dry run on a disabled conf returns 409 METAGEN_DISABLED; no RUN_COMPLETE emitted.

    spec: feature/BACKEND.md §Disabled-config rejection — non-dry run rejected when
      conf.is_enabled=false (409 METAGEN_DISABLED).
    """
    conf_id = await _create_conf(
        api_client, admin_headers, is_enabled=False, dataset_filter={"dataset_urns": [_TEST_URN]}
    )
    try:
        time_before = datetime.now(tz=UTC)
        run_resp = await api_client.post(
            f"{_CONF_URL}/{conf_id}/method/run", headers=admin_headers
        )
        assert run_resp.status_code == 409, (
            f"Disabled conf non-dry run must return 409; got {run_resp.status_code} {run_resp.text}"
        )
        assert run_resp.json()["error_code"] == "METAGEN_DISABLED"

        # No RUN_COMPLETE emitted for this conf by the rejected call.
        ev_resp = await api_client.get(
            f"{_CONF_URL}/{conf_id}/event?limit=100", headers=admin_headers
        )
        assert ev_resp.status_code == 200
        stale = [
            e
            for e in ev_resp.json().get("events", [])
            if e["event_type"] == "METAGEN.RUN_COMPLETE"
            and datetime.fromisoformat(e["occurred_at"]) >= time_before
        ]
        assert not stale, (
            "No RUN_COMPLETE may be emitted for a rejected disabled run. "
            "spec: feature/BACKEND.md §Event Catalogue"
        )
    finally:
        await _delete_conf(api_client, admin_headers, conf_id)


@pytest.mark.asyncio
async def test_metagen_run_dry_run_permitted_when_disabled(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """dry_run=true is permitted on a disabled conf; counts use candidates_proposed.

    spec: feature/BACKEND.md §Metadata Generation Service §Disabled-config rejection —
      dry-run permitted regardless of is_enabled.
    spec: feature/BACKEND.md §Event Catalogue — METAGEN dry-run counts are
      {items_considered, candidates_proposed}.
    """
    conf_id = await _create_conf(
        api_client, admin_headers, is_enabled=False, dataset_filter={"dataset_urns": [_TEST_URN]}
    )
    try:
        run_resp = await api_client.post(
            f"{_CONF_URL}/{conf_id}/method/run?dry_run=true", headers=admin_headers
        )
        assert run_resp.status_code == 200, (
            f"dry_run on disabled conf must return 200; got {run_resp.status_code} {run_resp.text}"
        )
        body = run_resp.json()
        assert body["dry_run"] is True
        assert body["conf_id"] == conf_id, "Run response must carry the conf_id it ran"
        counts = body["counts"]
        assert "items_considered" in counts and "candidates_proposed" in counts
        assert "candidates_added" not in counts, (
            "Dry-run counts must not include candidates_added. "
            "spec: feature/BACKEND.md §Event Catalogue — METAGEN dry-run count keys"
        )
    finally:
        await _delete_conf(api_client, admin_headers, conf_id)


@pytest.mark.asyncio
async def test_metagen_run_missing_conf_returns_404(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST run on a non-existent conf returns 404 METAGEN_CONF_NOT_FOUND.

    spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    missing_id = str(uuid.uuid4())
    resp = await api_client.post(
        f"{_CONF_URL}/{missing_id}/method/run", headers=admin_headers
    )
    assert resp.status_code == 404, (
        f"Run on missing conf must return 404; got {resp.status_code} {resp.text}"
    )
    assert resp.json()["error_code"] == "METAGEN_CONF_NOT_FOUND"


@pytest.mark.asyncio
async def test_metagen_run_concurrent_same_conf_returns_409_metagen_running(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    redis_client,
) -> None:
    """A run while the same conf's lock is held returns 409 METAGEN_RUNNING.

    The lock key is per-conf (metagen:running:{conf_id}). We switch the API to
    real Redis, pre-set that conf's lock, and assert the POST 409s.

    spec: feature/BACKEND.md §Concurrency — per-conf lock; 409 METAGEN_RUNNING.
    """
    admin_conf_url = "/api/v1/admin/conf"
    fake_token = f"spot-concurrent-{uuid.uuid4().hex[:8]}"

    switch = await api_client.patch(
        admin_conf_url, headers=admin_headers, json={"stub_redis_client": False}
    )
    assert switch.status_code == 200, f"stub_redis_client=false setup failed: {switch.text}"

    conf_id = await _create_conf(
        api_client, admin_headers, is_enabled=True, dataset_filter={"dataset_urns": [_TEST_URN]}
    )
    lock_key = f"metagen:running:{conf_id}"
    try:
        await redis_client.set_nx(lock_key, fake_token, ttl_seconds=60)

        run_resp = await api_client.post(
            f"{_CONF_URL}/{conf_id}/method/run", headers=admin_headers
        )
        assert run_resp.status_code == 409, (
            f"Run while per-conf lock held must return 409; "
            f"got {run_resp.status_code} {run_resp.text}"
        )
        assert run_resp.json()["error_code"] == "METAGEN_RUNNING"
    finally:
        with suppress(Exception):
            await redis_client.delete(lock_key)
        with suppress(Exception):
            await api_client.patch(
                admin_conf_url, headers=admin_headers, json={"stub_redis_client": True}
            )
        await _delete_conf(api_client, admin_headers, conf_id)


@pytest.mark.asyncio
async def test_metagen_run_empty_scope_completes_with_zero_items(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A run whose conf matches a dataset with no enabled boundary completes with
    items_considered=0 and a RUN_COMPLETE event.

    spec: feature/BACKEND.md §Generation Pipeline step 1 — empty in-scope set still
      completes successfully with all counts at zero.
    """
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    conf_id = await _create_conf(
        api_client, admin_headers, is_enabled=True, dataset_filter={"dataset_urns": [_TEST_URN]}
    )
    try:
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)

        run_resp = await api_client.post(
            f"{_CONF_URL}/{conf_id}/method/run", headers=admin_headers
        )
        assert run_resp.status_code == 200, (
            f"Empty-scope run must return 200; got {run_resp.status_code} {run_resp.text}"
        )
        body = run_resp.json()
        assert body["status"] == "success"
        assert body["counts"]["items_considered"] == 0, (
            "items_considered must be 0 with no enabled boundary. "
            "spec: feature/BACKEND.md §Generation Pipeline step 1"
        )

        ev_resp = await api_client.get(
            f"{_CONF_URL}/{conf_id}/event?limit=50", headers=admin_headers
        )
        assert ev_resp.status_code == 200
        run_complete = next(
            (
                e
                for e in ev_resp.json().get("events", [])
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e["detail"].get("run_id") == body["run_id"]
            ),
            None,
        )
        assert run_complete is not None, (
            "RUN_COMPLETE must be emitted for a zero-item run. "
            "spec: feature/BACKEND.md §Event Catalogue"
        )
        assert run_complete["detail"]["counts"]["items_considered"] == 0
    finally:
        await _delete_conf(api_client, admin_headers, conf_id)


# ── Multi-conf isolation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_confs_coexist_with_isolated_events_and_budgets(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Two confs over different dataset groups coexist; each run's events are scoped
    to its own conf, and the cross-conf union returns both.

    spec: feature/BACKEND.md §Metadata Generation Service — many named confs coexist,
      each with its own run trigger and event feed; all feed one global review queue.
    spec: API.md §Metadata Generation — /conf/{conf_id}/event is per-conf;
      /event is the cross-conf union.
    """
    conf_a = await _create_conf(
        api_client, admin_headers, is_enabled=True, dataset_filter={"dataset_urns": [_TEST_URN]}
    )
    conf_b = await _create_conf(
        api_client, admin_headers, is_enabled=True, dataset_filter={"dataset_urns": [_TEST_URN2]}
    )
    try:
        # Dry-run each conf so a RUN_COMPLETE event lands for each (no LLM/persist needed).
        run_a = await api_client.post(
            f"{_CONF_URL}/{conf_a}/method/run?dry_run=true", headers=admin_headers
        )
        run_b = await api_client.post(
            f"{_CONF_URL}/{conf_b}/method/run?dry_run=true", headers=admin_headers
        )
        assert run_a.status_code == 200 and run_b.status_code == 200
        run_a_id = run_a.json()["run_id"]
        run_b_id = run_b.json()["run_id"]

        # Conf A's event feed contains A's run, not B's.
        ev_a = await api_client.get(
            f"{_CONF_URL}/{conf_a}/event?limit=100", headers=admin_headers
        )
        a_run_ids = {e["detail"].get("run_id") for e in ev_a.json()["events"]}
        assert run_a_id in a_run_ids, "Conf A's event feed must include A's run"
        assert run_b_id not in a_run_ids, (
            "Conf A's event feed must not include conf B's run. "
            "spec: feature/BACKEND.md §Event Catalogue — per-conf event isolation"
        )

        # The cross-conf union contains both runs.
        ev_all = await api_client.get(
            "/api/v1/spoke/metagen/event?limit=100", headers=admin_headers
        )
        union_run_ids = {e["detail"].get("run_id") for e in ev_all.json()["events"]}
        assert {run_a_id, run_b_id} <= union_run_ids, (
            "Cross-conf /event union must include both confs' runs. "
            "spec: API.md §Metadata Generation — /metagen/event cross-conf union"
        )
    finally:
        await _delete_conf(api_client, admin_headers, conf_a)
        await _delete_conf(api_client, admin_headers, conf_b)


# ── Event endpoints ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_conf_event_filters_by_conf_id(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/conf/{conf_id}/event returns only that conf's metagen events.

    Seeds two RUN_COMPLETE events under two distinct conf ids; verifies the per-conf
    endpoint scopes by entity_id=conf_id.

    spec: API.md §Metadata Generation — per-conf event feed filtered by conf_id.
    spec: feature/BACKEND_SCHEMA.md §events — entity_type='metagen', entity_id=conf_id.
    """
    from tests.integration.util.metagen import seed_metagen_event

    conf_id_1 = str(uuid.uuid4())
    conf_id_2 = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    eid_1: str | None = None
    eid_2: str | None = None
    try:
        eid_1 = await seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id=conf_id_1,
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "conf_id": conf_id_1, "dry_run": False},
            occurred_at=now - timedelta(seconds=10),
        )
        eid_2 = await seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id=conf_id_2,
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "conf_id": conf_id_2, "dry_run": False},
            occurred_at=now - timedelta(seconds=5),
        )

        resp = await api_client.get(
            f"{_CONF_URL}/{conf_id_1}/event?limit=100", headers=admin_headers
        )
        assert resp.status_code == 200
        ids = {e["id"] for e in resp.json()["events"]}
        assert eid_1 in ids, "Conf 1's event must appear on its per-conf feed"
        assert eid_2 not in ids, (
            "Conf 2's event must not appear on conf 1's per-conf feed. "
            "spec: API.md §Metadata Generation — per-conf event filter"
        )
    finally:
        for eid in (eid_1, eid_2):
            if eid is not None:
                with suppress(Exception):
                    await async_session.execute(
                        text("DELETE FROM dataspoke.events WHERE id = CAST(:id AS uuid)"),
                        {"id": eid},
                    )
                    await async_session.commit()


@pytest.mark.asyncio
async def test_metagen_conf_event_time_range_narrows_to_the_inclusive_window(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/conf/{conf_id}/event?from=&to= returns only events inside the window.

    Seeds five events one to two seconds apart across a four-second window — one before
    `from`, one exactly at `from`, one strictly interior, one exactly at `to`, one after
    `to` — and requests that window. Every leg is load-bearing:

    - the two out-of-window events are absence assertions, not subset containment: a
      handler that ignores the window returns the whole feed, which a containment check
      would happily accept;
    - the at-`from` and at-`to` events make this the exact-bound test for this route,
      failing an exclusive ``>`` / ``<``. It has to live here because the two metagen event
      handlers are duplicated code, so the sibling test on ``/metagen/event`` does not
      cover this one;
    - the bounds being *distinct* (four seconds apart, not one instant) is what discriminates
      a pair swapped between the operators: under ``<= from`` / ``>= to`` the predicate
      selects nothing and the three in-window assertions fail. A degenerate
      ``from == to`` window cannot see that mutation, because the swap collapses to
      equality and still returns the single event;
    - ``total_count`` proves the count query carries the same window as the rows query.

    spec: API.md §Query Parameters — `from` is the "Start of time-range filter,
      inclusive", `to` the "End of time-range filter, inclusive"; both are "used on
      `result` and `event` endpoints".
    spec: API.md §Meta-Classifier Conventions (`event`) — event endpoints "Supports
      `from`/`to` for time-range filtering".
    spec: feature/FRONTEND_METAGEN.md §Conf create / detail — the conf run-history events
      table renders "with a `datetime` RangePicker driving `from`/`to`".
    """
    from tests.integration.util.metagen import seed_metagen_event

    conf_id = str(uuid.uuid4())
    # Whole-second timestamps so the request bounds land on the stored values exactly; a
    # fixed past anchor keeps the window clear of events other runs emit at "now".
    from_at = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=30)
    to_at = from_at + timedelta(seconds=4)
    seeded: dict[str, str] = {}
    try:
        for leg, occurred_at in (
            ("before_from", from_at - timedelta(seconds=1)),
            ("at_from", from_at),
            ("interior", from_at + timedelta(seconds=2)),
            ("at_to", to_at),
            ("after_to", to_at + timedelta(seconds=1)),
        ):
            seeded[leg] = await seed_metagen_event(
                async_session,
                entity_type="metagen",
                entity_id=conf_id,
                event_type="METAGEN.RUN_COMPLETE",
                detail={"run_id": str(uuid.uuid4()), "conf_id": conf_id, "leg": leg},
                occurred_at=occurred_at,
            )

        # `…Z` is the wire form spec/API.md §Date/Time states and the frontend client sends.
        resp = await api_client.get(
            f"{_CONF_URL}/{conf_id}/event"
            f"?from={urllib.parse.quote(from_at.isoformat().replace('+00:00', 'Z'), safe='')}"
            f"&to={urllib.parse.quote(to_at.isoformat().replace('+00:00', 'Z'), safe='')}"
            "&limit=100",
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
        body = resp.json()
        ids = {e["id"] for e in body["events"]}

        assert seeded["at_from"] in ids, (
            "An event whose occurred_at equals `from` exactly must be returned. "
            "spec: API.md §Query Parameters — `from` is the start of the time-range "
            "filter, inclusive"
        )
        assert seeded["interior"] in ids, (
            "An event strictly inside the window must be returned. "
            "spec: API.md §Meta-Classifier Conventions — event endpoints support from/to"
        )
        assert seeded["at_to"] in ids, (
            "An event whose occurred_at equals `to` exactly must be returned. "
            "spec: API.md §Query Parameters — `to` is the end of the time-range "
            "filter, inclusive"
        )
        assert seeded["before_from"] not in ids, (
            "An event before `from` must be filtered out, not returned. "
            "spec: API.md §Query Parameters — `from` is the start of the time-range filter"
        )
        assert seeded["after_to"] not in ids, (
            "An event after `to` must be filtered out, not returned. "
            "spec: API.md §Query Parameters — `to` is the end of the time-range filter"
        )
        assert body["total_count"] == 3, (
            "total_count must count the windowed rows, not the whole feed; "
            f"got {body['total_count']} for a window holding exactly three seeded events. "
            "spec: API.md §Query Parameters — from/to filter the `event` endpoint"
        )
    finally:
        for eid in seeded.values():
            with suppress(Exception):
                await async_session.execute(
                    text("DELETE FROM dataspoke.events WHERE id = CAST(:id AS uuid)"),
                    {"id": eid},
                )
                await async_session.commit()


@pytest.mark.asyncio
async def test_metagen_event_time_range_bounds_are_inclusive(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/event?from=&to= treats both bounds as inclusive.

    Seeds four events one second apart on the cross-conf feed: one before `from`, one
    whose ``occurred_at`` equals `from` exactly, one equal to `to` exactly, and one after
    `to`. The at-`from` event is the assertion that separates an inclusive `>=` lower
    bound from an exclusive `>` one; the two out-of-window events are the injected proof
    that the bounds are enforced at all rather than the filter being a no-op.

    spec: API.md §Query Parameters — `from` is the "Start of time-range filter,
      inclusive"; `to` is the "End of time-range filter, inclusive".
    spec: feature/FRONTEND_METAGEN.md §Components — `MetagenEventTable` is bound to the
      cross-conf `…/event` feed, "paired with a `datetime` RangePicker … for the
      `from`/`to` window".
    """
    from tests.integration.util.metagen import seed_metagen_event

    conf_id = str(uuid.uuid4())
    # Whole-second timestamps so the request bounds land on the stored values exactly.
    from_at = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=30)
    to_at = from_at + timedelta(seconds=2)
    before_at = from_at - timedelta(seconds=1)
    after_at = to_at + timedelta(seconds=1)
    seeded: dict[str, str] = {}
    try:
        for leg, occurred_at in (
            ("before_from", before_at),
            ("at_from", from_at),
            ("at_to", to_at),
            ("after_to", after_at),
        ):
            seeded[leg] = await seed_metagen_event(
                async_session,
                entity_type="metagen",
                entity_id=conf_id,
                event_type="METAGEN.RUN_COMPLETE",
                detail={"run_id": str(uuid.uuid4()), "conf_id": conf_id, "leg": leg},
                occurred_at=occurred_at,
            )

        # `…Z` is the wire form spec/API.md §Date/Time states and the frontend client sends.
        resp = await api_client.get(
            "/api/v1/spoke/metagen/event"
            f"?from={urllib.parse.quote(from_at.isoformat().replace('+00:00', 'Z'), safe='')}"
            f"&to={urllib.parse.quote(to_at.isoformat().replace('+00:00', 'Z'), safe='')}"
            "&limit=100",
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
        body = resp.json()
        ids = {e["id"] for e in body["events"]}

        assert seeded["at_from"] in ids, (
            "An event whose occurred_at equals `from` exactly must be included. "
            "spec: API.md §Query Parameters — `from` is the start of the time-range "
            "filter, inclusive"
        )
        assert seeded["at_to"] in ids, (
            "An event whose occurred_at equals `to` exactly must be included. "
            "spec: API.md §Query Parameters — `to` is the end of the time-range "
            "filter, inclusive"
        )
        assert seeded["before_from"] not in ids, (
            "An event one second before `from` must be excluded — otherwise the "
            "inclusive-bound assertions above pass on an unfiltered feed"
        )
        assert seeded["after_to"] not in ids, (
            "An event one second after `to` must be excluded — otherwise the "
            "inclusive-bound assertions above pass on an unfiltered feed"
        )
        # The count query must carry the same window as the rows query, else an
        # unwindowed total_count would slip past the id-only assertions above. Valid
        # because the 2-second window holds far fewer rows than the limit=100 page, so
        # the page is not truncated and the two numbers are comparable.
        assert body["total_count"] == len(body["events"]), (
            f"total_count {body['total_count']} must match the {len(body['events'])} "
            "returned rows — a count query without the from/to window overstates it. "
            "spec: API.md §Query Parameters — from/to filter the `event` endpoint"
        )
    finally:
        for eid in seeded.values():
            with suppress(Exception):
                await async_session.execute(
                    text("DELETE FROM dataspoke.events WHERE id = CAST(:id AS uuid)"),
                    {"id": eid},
                )
                await async_session.commit()


@pytest.mark.asyncio
async def test_metagen_global_event_union_across_confs(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/event returns the cross-conf union of all confs' run events.

    spec: API.md §Metadata Generation — GET /metagen/event is the cross-conf union;
      filters by entity_type='metagen'.
    """
    from tests.integration.util.metagen import seed_metagen_event

    conf_id_1 = str(uuid.uuid4())
    conf_id_2 = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    eid_1: str | None = None
    eid_2: str | None = None
    try:
        eid_1 = await seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id=conf_id_1,
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "conf_id": conf_id_1},
            occurred_at=now - timedelta(seconds=10),
        )
        eid_2 = await seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id=conf_id_2,
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "conf_id": conf_id_2},
            occurred_at=now - timedelta(seconds=5),
        )

        cutoff = (now - timedelta(minutes=1)).isoformat()
        resp = await api_client.get(
            "/api/v1/spoke/metagen/event"
            f"?from={urllib.parse.quote(cutoff, safe='')}&limit=100",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "events" in body and "total_count" in body
        ids = {e["id"] for e in body["events"]}
        assert {eid_1, eid_2} <= ids, (
            "Cross-conf /event union must contain both confs' events. "
            "spec: API.md §Metadata Generation — /metagen/event union"
        )
    finally:
        for eid in (eid_1, eid_2):
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
    """GET /data/{urn}/event/metagen returns METAGEN.* candidate events for that URN only.

    spec: API.md §Data Resource — per-dataset metagen events
      (METAGEN.CANDIDATE_APPROVE / CANDIDATE_REJECT) filtered by entity_id=urn.
    """
    from tests.integration.util.metagen import seed_metagen_event

    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"
    now = datetime.now(tz=UTC)
    eid_1: str | None = None
    eid_2: str | None = None
    try:
        eid_1 = await seed_metagen_event(
            async_session,
            entity_type="dataset",
            entity_id=_TEST_URN,
            event_type="METAGEN.CANDIDATE_APPROVE",
            detail={"item_id": "dataset.description", "candidate_id": str(uuid.uuid4())},
            occurred_at=now - timedelta(seconds=10),
        )
        eid_2 = await seed_metagen_event(
            async_session,
            entity_type="dataset",
            entity_id=_TEST_URN2,
            event_type="METAGEN.CANDIDATE_APPROVE",
            detail={"item_id": "dataset.description", "candidate_id": str(uuid.uuid4())},
            occurred_at=now - timedelta(seconds=5),
        )

        resp = await api_client.get(f"{dataset_event_url}?limit=50", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "events" in body and "total_count" in body
        ids = {e["id"] for e in body["events"]}
        assert eid_1 in ids, "Seeded event for the target URN must appear"
        assert eid_2 not in ids, (
            "Another URN's event must not appear on this dataset's feed. "
            "spec: API.md §Data Resource — entity_id filter"
        )
        for ev in body["events"]:
            assert ev["event_type"].startswith("METAGEN."), (
                "Per-dataset metagen feed must return only METAGEN.* events"
            )
    finally:
        for eid in (eid_1, eid_2):
            if eid is not None:
                with suppress(Exception):
                    await async_session.execute(
                        text("DELETE FROM dataspoke.events WHERE id = CAST(:id AS uuid)"),
                        {"id": eid},
                    )
                    await async_session.commit()
