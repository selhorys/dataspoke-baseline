"""Spot tests for Metadata Generation (UC4) — reshaped surface.

Concerns covered (21 test functions across 6 groups):

Singleton conf CRUD:
  test_metagen_global_conf_roundtrip_put_get_patch_delete
  test_metagen_global_conf_get_when_unset_returns_null_body
  test_metagen_global_conf_put_invalid_result_limit_422
  test_metagen_global_conf_put_invalid_dataset_urn_422

Per-dataset boundary CRUD:
  test_metagen_boundary_roundtrip_put_get_patch_delete
  test_metagen_boundary_get_unknown_urn_returns_null_body
  test_metagen_boundary_put_allowed_validation_422

Run-method gating:
  test_metagen_run_disabled_conf_non_dry_run_returns_409_METAGEN_DISABLED
  test_metagen_run_dry_run_permitted_when_disabled
  test_metagen_run_concurrent_returns_409_METAGEN_RUNNING
  test_metagen_run_empty_scope_completes_with_zero_items

Item endpoints (raw-SQL seeded):
  test_metagen_items_list_global_paginated_envelope
  test_metagen_items_list_filters_dataset_kind_status
  test_metagen_item_detail_by_composite_id

Candidate review (raw-SQL seeded):
  test_metagen_candidate_approve_flips_status_and_emits_event
  test_metagen_candidate_approve_demotes_prior_approved_sibling
  test_metagen_candidate_reject_emits_event
  test_metagen_candidate_reject_approved_returns_409_METAGEN_CANNOT_REJECT_APPROVED
  test_metagen_candidate_review_without_enabled_boundary_returns_422_METAGEN_DATASET_NOT_IN_BOUNDARY

Event endpoints:
  test_metagen_global_event_list_envelope_filters_by_time
  test_metagen_dataset_event_list_envelope

Per-item budget rules (result_limit, overwrite_pending FIFO eviction) are
covered at the unit level in tests/unit/backend/metagen/test_service.py
(_apply_per_item_budget); integration coverage would be redundant.

NOTE (concurrent run test): The MetagenService serialises concurrency via a
Redis cache lock ("metagen:running:singleton"), not a DB table — there is no
metagen_runs table.  The test pre-sets the Redis key directly via the
redis_client fixture to simulate an in-progress run, then calls POST run.

spec: USE_CASE_en.md §UC4 (L552-776)
spec: BACKEND.md §UC4 Metadata Generation — singleton conf, boundary,
      run pipeline, mutable approval, partial unique index on approved
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import json
import urllib.parse
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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

# ── Raw-SQL seed/cleanup helpers ──────────────────────────────────────────────
# Inline at top of this file per spec/TESTING.md §Spot vs Api-Wired Integration Tests
# and plan §Raw-SQL helpers. Not extracted to shared util.


async def _seed_metagen_item(
    session: AsyncSession,
    *,
    dataset_urn: str,
    item_id: str,
    kind: str = "dataset.description",
    field_path: str | None = None,
) -> None:
    """Insert a metagen_items row (composite PK: dataset_urn, item_id).

    Uses ON CONFLICT DO NOTHING so callers can safely call this multiple
    times for the same (dataset_urn, item_id) pair.

    spec: src/shared/db/models.py — MetagenItem composite PK (dataset_urn, item_id)
    spec: BACKEND.md §UC4 — item kind in {dataset.description, column.description}
    """
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_items"
            " (dataset_urn, item_id, kind, field_path)"
            " VALUES (:urn, :item_id, :kind, :fp)"
            " ON CONFLICT (dataset_urn, item_id) DO NOTHING"
        ),
        {"urn": dataset_urn, "item_id": item_id, "kind": kind, "fp": field_path},
    )
    await session.commit()


async def _seed_metagen_candidate(
    session: AsyncSession,
    *,
    dataset_urn: str,
    item_id: str,
    value: str,
    status: str = "llm_approved",
    confidence: float = 0.85,
    created_at: datetime | None = None,
) -> str:
    """Insert a metagen_candidates row; ensures parent item row exists first.

    Returns the new candidate_id as a str (UUID hex).

    spec: src/shared/db/models.py — MetagenCandidate PK candidate_id UUID;
      FK (dataset_urn, item_id) -> metagen_items;
      partial unique index: UNIQUE (dataset_urn, item_id) WHERE status='approved'
    spec: BACKEND.md §UC4 — candidate status in {llm_approved, approved, rejected}
    """
    # Ensure parent item row exists.
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_items"
            " (dataset_urn, item_id, kind)"
            " VALUES (:urn, :item_id, 'dataset.description')"
            " ON CONFLICT (dataset_urn, item_id) DO NOTHING"
        ),
        {"urn": dataset_urn, "item_id": item_id},
    )

    candidate_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ts = created_at or datetime.now(tz=UTC)

    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_candidates"
            " (candidate_id, dataset_urn, item_id, run_id, value,"
            "  confidence_score, status, evidence, created_at)"
            " VALUES (:candidate_id, :urn, :item_id, :run_id, :value,"
            "         :confidence, :status, '{}'::jsonb, :created_at)"
        ),
        {
            "candidate_id": candidate_id,
            "urn": dataset_urn,
            "item_id": item_id,
            "run_id": run_id,
            "value": value,
            "confidence": confidence,
            "status": status,
            "created_at": ts,
        },
    )
    await session.commit()
    return str(candidate_id)


async def _seed_metagen_event(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    detail: dict,
    occurred_at: datetime,
) -> str:
    """Insert a row into dataspoke.events.  Returns the event id as str.

    spec: src/shared/db/models.py — Event table schema
    spec: src/shared/events.py — event_type constants (METAGEN.* prefix)
    """
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dataspoke.events"
            " (id, entity_type, entity_id, event_type, status, detail, occurred_at)"
            " VALUES (:id, :etype, :eid, :evtype, 'success',"
            "         CAST(:detail AS jsonb), :occurred_at)"
        ),
        {
            "id": event_id,
            "etype": entity_type,
            "eid": entity_id,
            "evtype": event_type,
            "detail": json.dumps(detail),
            "occurred_at": occurred_at,
        },
    )
    await session.commit()
    return str(event_id)


async def _delete_metagen_state_for_urn(
    session: AsyncSession,
    dataset_urn: str,
) -> None:
    """Cascade-delete all metagen rows for dataset_urn.

    Deletion order (FK chain): embeddings -> candidates -> items -> events.
    Each step wrapped in suppress(Exception) so a single failure does not
    abort later cleanup steps.

    spec: src/shared/db/models.py L267 —
      metagen_candidate_embeddings.candidate_id FK -> metagen_candidates.candidate_id
    spec: TESTING.md §Integration Testing — teardown must not leak state
    """
    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.metagen_candidate_embeddings"
                " WHERE candidate_id IN ("
                "   SELECT candidate_id FROM dataspoke.metagen_candidates"
                "   WHERE dataset_urn = :urn"
                " )"
            ),
            {"urn": dataset_urn},
        )
        await session.commit()

    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.metagen_candidates WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await session.commit()

    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.metagen_items WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await session.commit()

    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.events"
                " WHERE entity_type = 'dataset' AND entity_id = :urn"
                "   AND event_type LIKE 'METAGEN.%'"
            ),
            {"urn": dataset_urn},
        )
        await session.commit()


# ── Group 1: Singleton conf CRUD ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_global_conf_roundtrip_put_get_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT -> GET -> PATCH -> DELETE singleton conf; events observable on /metagen/event.

    spec: USE_CASE_en.md §UC4 L604-608 — global conf fields: is_enabled,
      schedule_tier, dataset_filter, result_limit, overwrite_pending
    spec: BACKEND.md §UC4 — METAGEN.CONFIG_CREATE / CONFIG_UPDATE / CONFIG_DELETE
      events emitted by each mutation; entity_type='metagen', entity_id='singleton'
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    event_url = "/api/v1/spoke/common/metagen/event"

    try:
        # PUT — create singleton conf
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 5,
                "overwrite_pending": False,
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT conf failed: {put_resp.status_code} {put_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        put_body = put_resp.json()
        assert put_body["is_enabled"] is True, (
            f"is_enabled not preserved: {put_body.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert put_body["schedule_tier"] == "daily", (
            f"schedule_tier not preserved: {put_body.get('schedule_tier')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert put_body["result_limit"] == 5, (
            f"result_limit not preserved: {put_body.get('result_limit')!r}. "
            "spec: USE_CASE_en.md §UC4 L605"
        )
        assert put_body["overwrite_pending"] is False, (
            f"overwrite_pending not preserved: {put_body.get('overwrite_pending')!r}. "
            "spec: USE_CASE_en.md §UC4 L606"
        )
        assert put_body["dataset_filter"] == {"dataset_urns": [_TEST_URN]}, (
            f"dataset_filter not preserved: {put_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert "updated_at" in put_body, (
            "MetagenGlobalConfResponse missing updated_at. spec: USE_CASE_en.md §UC4"
        )

        # GET — verify round-trip
        get_resp = await api_client.get(conf_url, headers=admin_headers)
        assert get_resp.status_code == 200, (
            f"GET conf failed: {get_resp.status_code}. spec: USE_CASE_en.md §UC4"
        )
        get_body = get_resp.json()
        assert get_body["result_limit"] == 5, (
            f"GET round-trip result_limit mismatch: {get_body.get('result_limit')!r}"
        )
        assert get_body["schedule_tier"] == "daily", (
            f"GET round-trip schedule_tier mismatch: {get_body.get('schedule_tier')!r}"
        )

        # PATCH — partial update schedule_tier only
        patch_resp = await api_client.patch(
            conf_url,
            headers=admin_headers,
            json={"schedule_tier": "weekly"},
        )
        assert patch_resp.status_code == 200, (
            f"PATCH conf failed: {patch_resp.status_code}. spec: USE_CASE_en.md §UC4"
        )
        patch_body = patch_resp.json()
        assert patch_body["schedule_tier"] == "weekly", (
            f"PATCH schedule_tier not applied: {patch_body.get('schedule_tier')!r}. "
            "spec: USE_CASE_en.md §UC4"
        )
        # Non-patched fields preserved
        assert patch_body["result_limit"] == 5, (
            f"PATCH must preserve non-patched fields; result_limit changed: "
            f"{patch_body.get('result_limit')!r}. spec: USE_CASE_en.md §UC4"
        )

        # Events: CONFIG_CREATE (from PUT) or CONFIG_UPDATE (from PATCH) must appear
        # spec: BACKEND.md §UC4 — _record_metagen_event writes entity_type='metagen'
        ev_resp = await api_client.get(f"{event_url}?limit=50", headers=admin_headers)
        assert ev_resp.status_code == 200
        ev_body = ev_resp.json()
        assert "events" in ev_body and isinstance(ev_body["events"], list), (
            "EventListResponse must have 'events' list. spec: API.md §Standard Envelope"
        )
        event_types = {e["event_type"] for e in ev_body["events"]}
        assert "METAGEN.CONFIG_CREATE" in event_types or "METAGEN.CONFIG_UPDATE" in event_types, (
            f"Expected CONFIG_CREATE or CONFIG_UPDATE event; got {event_types!r}. "
            "spec: BACKEND.md §UC4 — conf mutations emit METAGEN.CONFIG_* events"
        )

        # DELETE — 204 no content
        del_resp = await api_client.delete(conf_url, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"DELETE conf expected 204; got {del_resp.status_code}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_global_conf_get_when_unset_returns_null_body(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /metagen/attr/conf before any PUT returns 200 with null body.

    The route is declared as response_model=MetagenGlobalConfResponse | None,
    so an absent singleton row returns 200 null — not 404.

    spec: src/api/routers/spoke/common/metagen.py L68-73 —
      get_metagen_conf returns None when no row exists
    spec: USE_CASE_en.md §UC4 — conf is optional until first PUT
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"

    # Ensure no conf exists
    with suppress(Exception):
        await api_client.delete(conf_url, headers=admin_headers)

    get_resp = await api_client.get(conf_url, headers=admin_headers)
    assert get_resp.status_code == 200, (
        f"GET conf when unset must return 200; got {get_resp.status_code}. "
        "spec: src/api/routers/spoke/common/metagen.py L68-73"
    )
    body = get_resp.json()
    assert body is None, (
        f"GET conf when unset must return null body; got {body!r}. "
        "spec: src/api/routers/spoke/common/metagen.py L73 — returns None"
    )


@pytest.mark.asyncio
async def test_metagen_global_conf_put_invalid_result_limit_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with result_limit=0 rejected with 422 (Pydantic constraint: ge=1).

    spec: src/api/schemas/metagen.py L41 — result_limit: int = Field(default=3, ge=1, le=20)
    spec: USE_CASE_en.md §UC4 L605 — result_limit must be a positive integer
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"

    resp = await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "is_enabled": False,
            "result_limit": 0,
        },
    )
    assert resp.status_code == 422, (
        f"PUT with result_limit=0 must return 422; got {resp.status_code} {resp.text}. "
        "spec: src/api/schemas/metagen.py L41 — result_limit ge=1"
    )


@pytest.mark.asyncio
async def test_metagen_global_conf_put_invalid_dataset_urn_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with malformed URN in dataset_filter.dataset_urns rejected with 422.

    The service validates each URN via _validate_dataset_filter -> _validate_dataset_urn
    which raises InvalidDatasetUrnError, mapped to 422 INVALID_DATASET_URN.

    spec: src/backend/metagen/service.py L135-142 — _validate_dataset_urn raises
      InvalidDatasetUrnError for URNs not matching ^urn:li:dataset:\\(.+\\)$
    spec: USE_CASE_en.md §UC4 L604 — dataset_filter.dataset_urns must be valid URNs
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"

    resp = await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "is_enabled": False,
            "dataset_filter": {"dataset_urns": ["not-a-valid-urn"]},
        },
    )
    assert resp.status_code == 422, (
        f"PUT with malformed URN must return 422; got {resp.status_code} {resp.text}. "
        "spec: src/backend/metagen/service.py L135-142"
    )


# ── Group 2: Per-dataset boundary CRUD ───────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_boundary_roundtrip_put_get_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT -> GET -> PATCH -> DELETE per-dataset boundary for a URN.

    spec: USE_CASE_en.md §UC4 L609-615 — boundary fields: is_enabled, allowed,
      owner; allowed in {dataset.description, column.description}
    spec: BACKEND.md §UC4 — MetagenBoundaryResponse echoes dataset_urn
    """
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    try:
        # PUT
        put_resp = await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["dataset.description", "column.description"],
                "owner": "test-owner",
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT boundary failed: {put_resp.status_code} {put_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L609"
        )
        put_body = put_resp.json()
        assert put_body["dataset_urn"] == _TEST_URN, (
            f"boundary dataset_urn not echoed: {put_body.get('dataset_urn')!r}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        assert put_body["is_enabled"] is True, (
            f"is_enabled not preserved: {put_body.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        assert set(put_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"allowed not preserved: {put_body.get('allowed')!r}. "
            "spec: USE_CASE_en.md §UC4 L614"
        )
        assert put_body["owner"] == "test-owner", (
            f"owner not preserved: {put_body.get('owner')!r}. spec: USE_CASE_en.md §UC4"
        )
        assert "created_at" in put_body and "updated_at" in put_body, (
            "MetagenBoundaryResponse missing created_at/updated_at. "
            "spec: USE_CASE_en.md §UC4"
        )

        # GET
        get_resp = await api_client.get(boundary_url, headers=admin_headers)
        assert get_resp.status_code == 200, (
            f"GET boundary failed: {get_resp.status_code}. spec: USE_CASE_en.md §UC4"
        )
        get_body = get_resp.json()
        assert get_body["is_enabled"] is True, (
            f"GET round-trip is_enabled mismatch: {get_body.get('is_enabled')!r}"
        )
        assert set(get_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"GET round-trip allowed mismatch: {get_body.get('allowed')!r}"
        )

        # PATCH — disable boundary
        patch_resp = await api_client.patch(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": False},
        )
        assert patch_resp.status_code == 200, (
            f"PATCH boundary failed: {patch_resp.status_code}. spec: USE_CASE_en.md §UC4"
        )
        patch_body = patch_resp.json()
        assert patch_body["is_enabled"] is False, (
            f"PATCH is_enabled not applied: {patch_body.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC4"
        )
        # Non-patched fields preserved
        assert set(patch_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"PATCH must not alter non-patched allowed field: {patch_body.get('allowed')!r}"
        )

        # DELETE — 204
        del_resp = await api_client.delete(boundary_url, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"DELETE boundary expected 204; got {del_resp.status_code}. "
            "spec: USE_CASE_en.md §UC4"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_boundary_get_unknown_urn_returns_null_body(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET boundary for a URN with no row returns 200 with null body.

    The route response_model is MetagenBoundaryResponse | None: when no
    boundary row exists the endpoint returns 200 with a null body, not 404.
    This is the same contract as the global conf get-when-unset endpoint.

    spec: USE_CASE_en.md §UC4 L613 — boundary is optional; absent means
      dataset is not in metagen scope
    spec: src/api/routers/spoke/common/data/metagen.py L57-66 —
      returns None (200 null body) when service returns None
    """
    # Use a URN that will never have a boundary seeded by any other test.
    unknown_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        "example_db.catalog.no_such_table_spot_metagen_test,DEV)"
    )
    encoded = urllib.parse.quote(unknown_urn, safe="")
    boundary_url = f"/api/v1/spoke/common/data/{encoded}/attr/metagen/conf"

    get_resp = await api_client.get(boundary_url, headers=admin_headers)
    assert get_resp.status_code == 200, (
        f"GET boundary for unknown URN must return 200; got {get_resp.status_code}. "
        "spec: src/api/routers/spoke/common/data/metagen.py L57-66"
    )
    body = get_resp.json()
    assert body is None, (
        f"GET boundary for unknown URN must return null body; got {body!r}. "
        "spec: src/api/routers/spoke/common/data/metagen.py L66 — returns None"
    )


@pytest.mark.asyncio
async def test_metagen_boundary_put_allowed_validation_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT boundary with a removed kind ('cross_data.md') in allowed returns 422.

    'cross_data.md' was the pre-reshape action type and is no longer valid.
    The Pydantic schema for MetagenBoundaryPutRequest only accepts
    'dataset.description' | 'column.description' in the allowed list.

    spec: src/api/schemas/metagen.py L77-80 — allowed field:
      list[Literal['dataset.description', 'column.description']]
    spec: USE_CASE_en.md §UC4 L614 — allowed kinds are dataset.description and
      column.description only (cross_data.md removed in reshape commit 3fa7d59)
    """
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    resp = await api_client.put(
        boundary_url,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "allowed": ["cross_data.md"],  # removed kind — must be rejected
        },
    )
    assert resp.status_code == 422, (
        f"PUT boundary with removed kind 'cross_data.md' must return 422; "
        f"got {resp.status_code} {resp.text}. "
        "spec: src/api/schemas/metagen.py L77-80"
    )


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
        ev_resp = await api_client.get(f"{event_url}?limit=200", headers=admin_headers)
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


# ── Group 4: Item endpoints (raw-SQL seeded) ──────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_items_list_global_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/item returns MetagenItemListResponse envelope with pagination keys.

    Seeds two items across two datasets; verifies envelope structure and
    pagination parameters are echoed correctly.

    spec: USE_CASE_en.md §UC4 — item list endpoint
    spec: API.md §Standard Envelope — items, offset, limit, total_count
    spec: src/api/schemas/metagen.py L103-104 — MetagenItemListResponse
    """
    item_list_url = "/api/v1/spoke/common/metagen/item"

    try:
        await _seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="dataset.description",
            kind="dataset.description",
        )
        await _seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN2,
            item_id="dataset.description",
            kind="dataset.description",
        )

        # Paginated GET
        resp = await api_client.get(
            f"{item_list_url}?offset=0&limit=10",
            headers=admin_headers,
        )
        assert resp.status_code == 200, (
            f"GET /metagen/item failed: {resp.status_code} {resp.text}. "
            "spec: USE_CASE_en.md §UC4"
        )
        body = resp.json()

        # Envelope keys
        assert "items" in body and isinstance(body["items"], list), (
            "MetagenItemListResponse must have 'items' list. spec: API.md §Standard Envelope"
        )
        assert "offset" in body, (
            "MetagenItemListResponse must have 'offset'. spec: API.md §Standard Envelope"
        )
        assert "limit" in body, (
            "MetagenItemListResponse must have 'limit'. spec: API.md §Standard Envelope"
        )
        assert "total_count" in body and isinstance(body["total_count"], int), (
            "MetagenItemListResponse must have 'total_count' int. "
            "spec: API.md §Standard Envelope"
        )
        assert body["offset"] == 0, (
            f"offset echo mismatch: {body.get('offset')!r}. spec: API.md §Standard Envelope"
        )
        assert body["limit"] == 10, (
            f"limit echo mismatch: {body.get('limit')!r}. spec: API.md §Standard Envelope"
        )
        assert body["total_count"] >= 2, (
            f"total_count must be >= 2 after seeding two items; got {body.get('total_count')!r}"
        )

        # Each item must have the required summary fields
        for item in body["items"]:
            assert "dataset_urn" in item, "item missing dataset_urn"
            assert "item_id" in item, "item missing item_id"
            assert item["kind"] in (
                "dataset.description",
                "column.description",
            ), f"item kind invalid: {item.get('kind')!r}"
            assert item["status"] in (
                "pending",
                "llm_approved",
                "approved",
            ), f"item status invalid: {item.get('status')!r}"
            assert "candidate_count" in item, "item missing candidate_count"
            assert "composite_id" in item, (
                "item missing composite_id. spec: USE_CASE_en.md §UC4 API Mapping L684"
            )
            assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                f"composite_id format mismatch: {item['composite_id']!r}. "
                "spec: USE_CASE_en.md §UC4 L684"
            )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)
        await _delete_metagen_state_for_urn(async_session, _TEST_URN2)


@pytest.mark.asyncio
async def test_metagen_items_list_filters_dataset_kind_status(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/item with dataset_urn / kind / status filters returns matching subset.

    Seeds:
      _TEST_URN: item_id="dataset.description" (kind=dataset.description,
          one llm_approved candidate -> status=llm_approved)
      _TEST_URN: item_id="column.isbn.description" (kind=column.description,
          zero candidates -> status=pending)
      _TEST_URN2: item_id="dataset.description" (kind=dataset.description)

    Verifies:
      ?dataset_urn=_TEST_URN returns only _TEST_URN items
      ?kind=column.description returns only column-kind items
      ?status=llm_approved includes the dataset.description item with llm_approved cand

    spec: USE_CASE_en.md §UC4 L683 — item list filterable by dataset_urn, kind, status
    spec: src/api/routers/spoke/common/metagen.py L184-207 — filter params
    """
    item_list_url = "/api/v1/spoke/common/metagen/item"

    try:
        # Seed dataset.description item with one llm_approved candidate
        await _seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="dataset.description",
            kind="dataset.description",
        )
        await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="dataset.description",
            value="Imazon title master catalog.",
            status="llm_approved",
        )

        # Seed column.description item with no candidates (status=pending)
        await _seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="column.isbn.description",
            kind="column.description",
            field_path="isbn",
        )

        # Seed an item for URN2 (to confirm dataset_urn filter excludes it)
        await _seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN2,
            item_id="dataset.description",
            kind="dataset.description",
        )

        # Filter by dataset_urn — must return only _TEST_URN items
        encoded_urn = urllib.parse.quote(_TEST_URN, safe="")
        resp_by_urn = await api_client.get(
            f"{item_list_url}?dataset_urn={encoded_urn}&limit=50",
            headers=admin_headers,
        )
        assert resp_by_urn.status_code == 200, (
            f"GET items?dataset_urn failed: {resp_by_urn.status_code}"
        )
        by_urn_items = resp_by_urn.json()["items"]
        urn_set = {i["dataset_urn"] for i in by_urn_items}
        assert urn_set <= {_TEST_URN}, (
            f"dataset_urn filter returned items for other URNs: {urn_set!r}. "
            "spec: src/api/routers/spoke/common/metagen.py L195"
        )

        # Filter by kind=column.description
        resp_by_kind = await api_client.get(
            f"{item_list_url}?dataset_urn={encoded_urn}&kind=column.description&limit=50",
            headers=admin_headers,
        )
        assert resp_by_kind.status_code == 200
        by_kind_items = resp_by_kind.json()["items"]
        assert all(i["kind"] == "column.description" for i in by_kind_items), (
            f"kind filter returned non-column items: {[i['kind'] for i in by_kind_items]!r}. "
            "spec: src/api/routers/spoke/common/metagen.py L196"
        )

        # Filter by status=llm_approved — must include the seeded dataset.description item
        resp_by_status = await api_client.get(
            f"{item_list_url}?dataset_urn={encoded_urn}&status=llm_approved&limit=50",
            headers=admin_headers,
        )
        assert resp_by_status.status_code == 200
        by_status_items = resp_by_status.json()["items"]
        assert all(
            i["status"] in ("llm_approved", "approved") for i in by_status_items
        ), (
            f"status=llm_approved filter returned non-matching items: "
            f"{[i['status'] for i in by_status_items]!r}. "
            "spec: src/api/routers/spoke/common/metagen.py L197"
        )
        item_ids = {i["item_id"] for i in by_status_items}
        assert "dataset.description" in item_ids, (
            f"status=llm_approved filter must include the seeded dataset.description item; "
            f"got {item_ids!r}"
        )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)
        await _delete_metagen_state_for_urn(async_session, _TEST_URN2)


@pytest.mark.asyncio
async def test_metagen_item_detail_by_composite_id(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/item/{urn}::{item_id} returns full item detail with candidate list.

    Seeds one item with two candidates; verifies both appear in the candidates
    list of the detail response.

    spec: USE_CASE_en.md §UC4 L684 — composite_id = '{dataset_urn}::{item_id}'
    spec: src/api/routers/spoke/common/metagen.py L213-229 — composite_id parsing
    spec: src/api/schemas/metagen.py L120 — MetagenItemDetailResponse.candidates
    """
    item_id = "dataset.description"
    composite_id = f"{_TEST_URN}::{item_id}"
    encoded_composite = urllib.parse.quote(composite_id, safe="")
    item_detail_url = f"/api/v1/spoke/common/metagen/item/{encoded_composite}"

    try:
        cid1 = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="First candidate value.",
            status="llm_approved",
            confidence=0.91,
        )
        cid2 = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Second candidate value.",
            status="llm_approved",
            confidence=0.80,
        )

        resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert resp.status_code == 200, (
            f"GET item detail by composite_id failed: {resp.status_code} {resp.text}. "
            "spec: src/api/routers/spoke/common/metagen.py L213"
        )
        body = resp.json()

        # Item fields
        assert body["dataset_urn"] == _TEST_URN, (
            f"detail dataset_urn mismatch: {body.get('dataset_urn')!r}"
        )
        assert body["item_id"] == item_id, (
            f"detail item_id mismatch: {body.get('item_id')!r}"
        )
        assert body["composite_id"] == composite_id, (
            f"detail composite_id mismatch: {body.get('composite_id')!r}. "
            "spec: USE_CASE_en.md §UC4 L684"
        )

        # Both seeded candidates must be present
        assert "candidates" in body and isinstance(body["candidates"], list), (
            "MetagenItemDetailResponse must have 'candidates' list. "
            "spec: src/api/schemas/metagen.py L120"
        )
        returned_ids = {c["candidate_id"] for c in body["candidates"]}
        assert cid1 in returned_ids, (
            f"Seeded candidate {cid1!r} not in detail response. "
            "spec: USE_CASE_en.md §UC4 L617-631"
        )
        assert cid2 in returned_ids, (
            f"Seeded candidate {cid2!r} not in detail response. "
            "spec: USE_CASE_en.md §UC4 L617-631"
        )

        # Each candidate has required fields
        for cand in body["candidates"]:
            assert "candidate_id" in cand, "candidate missing candidate_id"
            assert "value" in cand, "candidate missing value"
            assert "confidence_score" in cand, "candidate missing confidence_score"
            assert cand["status"] in (
                "llm_approved",
                "approved",
                "rejected",
            ), f"candidate status invalid: {cand.get('status')!r}"
            assert "evidence" in cand, "candidate missing evidence"
            assert "created_at" in cand, "candidate missing created_at"

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)


# ── Group 5: Candidate review (raw-SQL seeded) ────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_candidate_approve_flips_status_and_emits_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Approving an llm_approved candidate flips its status to 'approved'.

    Also emits METAGEN.CANDIDATE_APPROVE event on per-dataset event endpoint.

    spec: USE_CASE_en.md §UC4 L649-657 — approve verdict -> status=approved
    spec: BACKEND.md §766-767 — METAGEN.CANDIDATE_APPROVE detail keys:
      item_id, candidate_id, reason
    """
    item_id = "dataset.description"
    unique_reason = f"spot test approve {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    review_prefix = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}"
        f"/attr/metagen/item/{item_id}/candidate"
    )
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    try:
        # Boundary required for review
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        cid = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Approved candidate value.",
            status="llm_approved",
        )

        review_url = f"{review_prefix}/{cid}/method/review"
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "approve", "reason": unique_reason},
        )
        assert review_resp.status_code == 200, (
            f"POST review (approve) failed: {review_resp.status_code} {review_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L649"
        )
        review_body = review_resp.json()
        assert review_body.get("status") == "approved", (
            f"candidate status after approve must be 'approved'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649"
        )
        assert review_body.get("candidate_id") == cid, (
            "candidate_id mismatch in review response. spec: BACKEND.md §766"
        )

        # METAGEN.CANDIDATE_APPROVE event emitted on per-dataset endpoint;
        # bind by candidate_id and unique reason to avoid stale event matches.
        ev_resp = await api_client.get(
            f"{dataset_event_url}?limit=20",
            headers=admin_headers,
        )
        assert ev_resp.status_code == 200
        events = ev_resp.json().get("events", [])
        approve_event = next(
            (
                e for e in events
                if e["event_type"] == "METAGEN.CANDIDATE_APPROVE"
                and e["detail"].get("candidate_id") == cid
            ),
            None,
        )
        assert approve_event is not None, (
            f"METAGEN.CANDIDATE_APPROVE event for candidate_id={cid!r} must be emitted. "
            "spec: BACKEND.md §766 event catalogue"
        )
        ev_detail = approve_event["detail"]
        assert ev_detail["candidate_id"] == cid, (
            f"CANDIDATE_APPROVE detail candidate_id must be {cid!r}; "
            f"got {ev_detail.get('candidate_id')!r}. spec: BACKEND.md §766"
        )
        assert ev_detail["item_id"] == item_id, (
            f"CANDIDATE_APPROVE detail item_id must be {item_id!r}; "
            f"got {ev_detail.get('item_id')!r}. spec: BACKEND.md §766"
        )
        assert ev_detail["reason"] == unique_reason, (
            f"CANDIDATE_APPROVE detail reason must be {unique_reason!r}; "
            f"got {ev_detail.get('reason')!r}. spec: BACKEND.md §766"
        )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_approve_demotes_prior_approved_sibling(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Approving candidate B when candidate A is approved atomically demotes A to llm_approved.

    Covers the mutable approval contract and partial unique index
    UNIQUE (dataset_urn, item_id) WHERE status='approved'.

    spec: USE_CASE_en.md §UC4 L649-657 — "approving a new candidate atomically
      demotes the previously approved sibling"
    spec: BACKEND.md §UC4 — partial unique index enforced; sibling demotion via
      flush + commit pattern in service
    spec: src/backend/metagen/service.py L742-764 — flush demotion before commit
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    review_prefix = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}"
        f"/attr/metagen/item/{item_id}/candidate"
    )
    item_detail_url = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/item/{item_id}"
    )

    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        # Seed candidate A (llm_approved)
        cid_a = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Candidate A value.",
            status="llm_approved",
        )
        # Seed candidate B (llm_approved)
        cid_b = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Candidate B value.",
            status="llm_approved",
        )

        # Approve A first
        resp_a = await api_client.post(
            f"{review_prefix}/{cid_a}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "approve A first"},
        )
        assert resp_a.status_code == 200, (
            f"Approve A failed: {resp_a.status_code} {resp_a.text}"
        )
        assert resp_a.json().get("status") == "approved", (
            f"candidate A must be approved; got {resp_a.json().get('status')!r}"
        )

        # Approve B — must atomically demote A back to llm_approved
        resp_b = await api_client.post(
            f"{review_prefix}/{cid_b}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "approve B to demote A"},
        )
        assert resp_b.status_code == 200, (
            f"Approve B failed: {resp_b.status_code} {resp_b.text}. "
            "spec: BACKEND.md §UC4 — mutable approval must not raise unique constraint error"
        )
        assert resp_b.json().get("status") == "approved", (
            f"candidate B must be approved; got {resp_b.json().get('status')!r}. "
            "spec: USE_CASE_en.md §UC4 L649"
        )

        # GET item detail — A must now be llm_approved again
        detail_resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert detail_resp.status_code == 200, (
            f"GET item detail failed: {detail_resp.status_code}"
        )
        candidates = {
            c["candidate_id"]: c["status"]
            for c in detail_resp.json().get("candidates", [])
        }
        assert candidates.get(cid_b) == "approved", (
            f"candidate B must be approved after demotion; got {candidates.get(cid_b)!r}. "
            "spec: USE_CASE_en.md §UC4 L649"
        )
        assert candidates.get(cid_a) == "llm_approved", (
            f"candidate A must be demoted to llm_approved; got {candidates.get(cid_a)!r}. "
            "spec: BACKEND.md §UC4 — partial unique index UNIQUE (dataset_urn, item_id)"
            " WHERE status='approved' — at most one approved candidate per item"
        )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_reject_emits_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Rejecting an llm_approved candidate flips status to 'rejected' and emits event.

    spec: USE_CASE_en.md §UC4 L649-657 — reject verdict -> status=rejected
    spec: BACKEND.md §766-767 — METAGEN.CANDIDATE_REJECT detail keys:
      item_id, candidate_id, reason
    """
    item_id = "dataset.description"
    unique_reason = f"spot test reject {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        cid = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Rejected candidate value.",
            status="llm_approved",
        )

        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{item_id}/candidate/{cid}/method/review"
        )
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "reject", "reason": unique_reason},
        )
        assert review_resp.status_code == 200, (
            f"POST review (reject) failed: {review_resp.status_code} {review_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L649"
        )
        review_body = review_resp.json()
        assert review_body.get("status") == "rejected", (
            f"candidate status after reject must be 'rejected'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649"
        )

        # METAGEN.CANDIDATE_REJECT event emitted;
        # bind by candidate_id and unique reason to avoid stale event matches.
        ev_resp = await api_client.get(
            f"{dataset_event_url}?limit=20",
            headers=admin_headers,
        )
        assert ev_resp.status_code == 200
        events = ev_resp.json().get("events", [])
        reject_event = next(
            (
                e for e in events
                if e["event_type"] == "METAGEN.CANDIDATE_REJECT"
                and e["detail"].get("candidate_id") == cid
            ),
            None,
        )
        assert reject_event is not None, (
            f"METAGEN.CANDIDATE_REJECT event for candidate_id={cid!r} must be emitted. "
            "spec: BACKEND.md §767 event catalogue"
        )
        ev_detail = reject_event["detail"]
        assert ev_detail["candidate_id"] == cid, (
            f"CANDIDATE_REJECT detail candidate_id must be {cid!r}; "
            f"got {ev_detail.get('candidate_id')!r}. spec: BACKEND.md §767"
        )
        assert ev_detail["item_id"] == item_id, (
            f"CANDIDATE_REJECT detail item_id must be {item_id!r}; "
            f"got {ev_detail.get('item_id')!r}. spec: BACKEND.md §767"
        )
        assert ev_detail["reason"] == unique_reason, (
            f"CANDIDATE_REJECT detail reason must be {unique_reason!r}; "
            f"got {ev_detail.get('reason')!r}. spec: BACKEND.md §767"
        )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_reject_approved_returns_409_METAGEN_CANNOT_REJECT_APPROVED(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Rejecting an already-approved candidate returns 409 METAGEN_CANNOT_REJECT_APPROVED.

    spec: USE_CASE_en.md §UC4 L655-656 — rejecting an approved candidate is refused
      with 409 METAGEN_CANNOT_REJECT_APPROVED
    spec: BACKEND.md L531-532 — reject is only valid for llm_approved candidates;
      approved returns 409 METAGEN_CANNOT_REJECT_APPROVED
    spec: BACKEND.md L949 — ConflictError error-code table: METAGEN_CANNOT_REJECT_APPROVED
    spec: src/backend/metagen/service.py L788-792 — ConflictError raised when
      cand.status == 'approved' and verdict == 'reject'
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        # Seed a candidate directly with status='approved' (bypasses approve flow)
        cid = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Already approved candidate.",
            status="approved",
        )

        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{item_id}/candidate/{cid}/method/review"
        )
        reject_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "reject", "reason": "attempt to reject approved"},
        )
        assert reject_resp.status_code == 409, (
            f"Reject on approved candidate must return 409; "
            f"got {reject_resp.status_code} {reject_resp.text}. "
            "spec: src/backend/metagen/service.py L788-792"
        )
        assert "METAGEN_CANNOT_REJECT_APPROVED" in str(reject_resp.json()), (
            f"409 response must carry METAGEN_CANNOT_REJECT_APPROVED code; "
            f"got {reject_resp.json()!r}. "
            "spec: BACKEND.md L949 — ConflictError table; "
            "spec: BACKEND.md L531-532 — reject of approved returns 409"
        )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_review_without_enabled_boundary_returns_422_METAGEN_DATASET_NOT_IN_BOUNDARY(  # noqa: E501
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Review attempt with no active boundary returns 422 METAGEN_DATASET_NOT_IN_BOUNDARY.

    spec: BACKEND.md L547-549 — boundary guard: candidate review against a dataset whose
      metagen_boundary is absent or is_enabled=false returns 422 METAGEN_DATASET_NOT_IN_BOUNDARY
    spec: BACKEND.md L953 — PreconditionFailedError maps to HTTP 422
    spec: src/backend/metagen/service.py L712-720 — PreconditionFailedError raised
      when boundary is None or boundary.is_enabled=false
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    try:
        # Ensure no boundary exists for _TEST_URN
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)

        cid = await _seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Candidate without boundary.",
            status="llm_approved",
        )

        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{item_id}/candidate/{cid}/method/review"
        )
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "approve", "reason": "should fail"},
        )
        assert review_resp.status_code == 422, (
            f"Review without boundary must return 422; "
            f"got {review_resp.status_code} {review_resp.text}. "
            "spec: src/backend/metagen/service.py L712-720"
        )
        assert "METAGEN_DATASET_NOT_IN_BOUNDARY" in str(review_resp.json()), (
            f"422 response must carry METAGEN_DATASET_NOT_IN_BOUNDARY code; "
            f"got {review_resp.json()!r}. "
            "spec: BACKEND.md L547-549 — boundary guard; "
            "spec: BACKEND.md L953 — PreconditionFailedError → 422"
        )

    finally:
        await _delete_metagen_state_for_urn(async_session, _TEST_URN)


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
        older_event_id = await _seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id="singleton",
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "dry_run": False, "counts": {}},
            occurred_at=older_time,
        )
        newer_event_id = await _seed_metagen_event(
            async_session,
            entity_type="metagen",
            entity_id="singleton",
            event_type="METAGEN.RUN_COMPLETE",
            detail={"run_id": str(uuid.uuid4()), "dry_run": False, "counts": {}},
            occurred_at=newer_time,
        )

        # GET all (no time filter) — verify envelope shape
        all_resp = await api_client.get(f"{event_url}?limit=200", headers=admin_headers)
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
            f"{event_url}?after={urllib.parse.quote(cutoff, safe='')}&limit=200",
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
        event_id_1 = await _seed_metagen_event(
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
        event_id_2 = await _seed_metagen_event(
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
