"""Spot tests for Metadata Generation — singleton conf and per-dataset boundary CRUD.

Concerns covered (7 test functions across 2 groups):

Singleton conf CRUD (Group 1):
  test_metagen_global_conf_roundtrip_put_get_patch_delete
  test_metagen_global_conf_get_when_unset_returns_null_body
  test_metagen_global_conf_put_invalid_result_limit_422
  test_metagen_global_conf_put_invalid_dataset_urn_422

Per-dataset boundary CRUD (Group 2):
  test_metagen_boundary_roundtrip_put_get_patch_delete
  test_metagen_boundary_get_unknown_urn_returns_null_body
  test_metagen_boundary_put_allowed_validation_422

These tests are pure REST and do not require raw-SQL seeding.

spec: USE_CASE_en.md §UC4 (L552-776)
spec: BACKEND.md §UC4 Metadata Generation — singleton conf, boundary
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import urllib.parse
from contextlib import suppress

import httpx
import pytest

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master
# into DataHub + PG before any tests run.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Primary test dataset — catalog.title_master (Imazon UC4 table).
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")


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
