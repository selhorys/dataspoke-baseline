"""Spot tests for Metadata Generation — singleton conf and per-dataset boundary CRUD.

Concerns covered (9 test functions across 3 groups):

Singleton conf CRUD (Group 1):
  test_metagen_global_conf_roundtrip_put_get_patch_delete
  test_metagen_global_conf_get_when_unset_returns_null_body
  test_metagen_global_conf_put_invalid_result_limit_422
  test_metagen_global_conf_put_invalid_dataset_urn_422

Per-dataset boundary CRUD (Group 2):
  test_metagen_boundary_roundtrip_put_get_patch_delete
  test_metagen_boundary_get_unknown_urn_returns_null_body
  test_metagen_boundary_put_allowed_validation_422

Payload cap and schedule_tier boundary (Group 3):
  test_metagen_global_conf_put_dataset_filter_dimension_caps
    parametrized over (n, expected_status_set) x dimension:
      [at-cap-1000-accepted] x [tags, glossary_terms, dataset_urns] — 200/201 accepted
      [over-cap-1001-rejected] x [tags, glossary_terms, dataset_urns] — 422 rejected
    for both PUT and PATCH methods
  test_metagen_global_conf_put_invalid_schedule_tier_422

These tests are pure REST and do not require raw-SQL seeding.

spec: USE_CASE_en.md §UC4 (L552-776)
spec: API.md §UC4 Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns} ≤ 1,000
spec: BACKEND.md §UC4 Metadata Generation — singleton conf, boundary; schedule_tier Literal
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

    URN format is validated by check_dataset_urn_format in
    src/api/schemas/_dataset_filter.py, which raises InvalidDatasetUrnError.
    The service-layer wrapper validate_dataset_filter_service (same module as
    resolve_dataset_scope in src/backend/_dataset_filter.py) propagates
    InvalidDatasetUrnError unchanged so it maps to 422 INVALID_DATASET_URN.

    spec: USE_CASE_en.md §UC4 — dataset_filter.dataset_urns must be valid URNs
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


# ── Group 3: Payload cap and schedule_tier boundary tests ────────────────────


@pytest.mark.parametrize(
    ("n", "expected_status_set"),
    [(1000, {200, 201}), (1001, {422})],
    ids=["at-cap-1000-accepted", "over-cap-1001-rejected"],
)
@pytest.mark.parametrize(
    "dimension",
    ["tags", "glossary_terms", "dataset_urns"],
    ids=["tags", "glossary_terms", "dataset_urns"],
)
@pytest.mark.parametrize(
    "method",
    ["PUT", "PATCH"],
    ids=["PUT", "PATCH"],
)
@pytest.mark.asyncio
async def test_metagen_global_conf_put_dataset_filter_dimension_caps(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    method: str,
    dimension: str,
    n: int,
    expected_status_set: set[int],
) -> None:
    """PUT or PATCH at-cap (n=1000, accepted) and over-cap (n=1001, rejected) on a
    single dataset_filter dimension.

    The boundary test (n=1000) verifies the cap itself: a regression dropping the
    limit to 500 would still pass the n=1001 test but fail here.  Well-formed URNs
    are used so cap enforcement — not URN validation — triggers the result.

    spec: API.md §UC4 Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns}
      ≤ 1,000 entries per dimension; exactly 1,000 MUST be accepted; 1,001 MUST be rejected.
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"

    # Build n well-formed URN strings for the chosen dimension.
    if dimension == "tags":
        entries = [f"urn:li:tag:t-{i}" for i in range(n)]
    elif dimension == "glossary_terms":
        entries = [f"urn:li:glossaryTerm:gt-{i}" for i in range(n)]
    else:  # dataset_urns
        entries = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t_{i},DEV)"
            for i in range(n)
        ]

    try:
        if method == "PUT":
            resp = await api_client.put(
                conf_url,
                headers=admin_headers,
                json={
                    "is_enabled": False,
                    "schedule_tier": "daily",
                    "result_limit": 5,
                    "overwrite_pending": False,
                    "dataset_filter": {dimension: entries},
                },
            )
        else:  # PATCH — seed a minimal conf first so PATCH has a row to update
            seed_resp = await api_client.put(
                conf_url,
                headers=admin_headers,
                json={
                    "is_enabled": False,
                    "dataset_filter": {dimension: ["urn:li:tag:seed-0"] if dimension == "tags"
                                       else ["urn:li:glossaryTerm:seed-0"] if dimension == "glossary_terms"
                                       else ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.seed,DEV)"]},
                },
            )
            assert seed_resp.status_code in (200, 201), (
                f"Seed PUT for PATCH boundary test failed: {seed_resp.status_code} {seed_resp.text}"
            )
            resp = await api_client.patch(
                conf_url,
                headers=admin_headers,
                json={"dataset_filter": {dimension: entries}},
            )

        # spec: API.md §UC4 Payload caps — exactly 1,000 entries MUST be accepted
        # (200 or 201); 1,001 entries MUST be rejected (422) at the Pydantic boundary
        # before any service-layer or DB call.
        assert resp.status_code in expected_status_set, (
            f"{method} with n={n} {dimension} entries: expected status in {expected_status_set}, "
            f"got {resp.status_code}: {resp.text}. "
            "spec: API.md §UC4 Payload caps — dataset_filter cap is 1,000 per dimension"
        )

        if 422 in expected_status_set:
            # The 422 body must be non-empty JSON (we do not pin the error message wording).
            # spec: API.md §UC4 Payload caps — over-cap dimension rejected at schema boundary.
            body = resp.json()
            assert body, (
                f"422 response body must be non-empty JSON; got: {resp.text!r}. "
                "spec: API.md §Error Codes — validation errors return structured JSON body"
            )

    finally:
        # The at-cap PUT/PATCH may have written a row; clean up idempotently.
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_global_conf_put_origin_filter_round_trips(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT dataset_filter with origin+tags, then GET verifies the filter is persisted.

    Exercises the unified four-dimension dataset_filter shape for UC4.

    spec: spec/API.md §UC4 — dataset_filter unified four-dimension shape; origin dimension.
    spec: USE_CASE_en.md §UC4 §Conf — dataset_filter is optional scope filter.
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    expected_filter = {
        "origin": "DEV",
        "tags": ["urn:li:tag:area:fulfillment"],
    }

    try:
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": expected_filter,
                "result_limit": 3,
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT with origin+tags dataset_filter failed: {put_resp.status_code} {put_resp.text}. "
            "spec: API.md §UC4 — dataset_filter unified four-dimension shape"
        )
        put_body = put_resp.json()
        assert put_body["dataset_filter"] == expected_filter, (
            f"PUT response dataset_filter not preserved: {put_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 §Conf — dataset_filter round-trip"
        )

        get_resp = await api_client.get(conf_url, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["dataset_filter"] == expected_filter, (
            f"GET round-trip dataset_filter mismatch: {get_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 §Conf — dataset_filter must be persisted"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_global_conf_patch_adds_origin_to_existing_conf(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH adding origin='DEV' to an existing conf persists the updated filter.

    spec: spec/API.md §UC4 — dataset_filter unified four-dimension shape; PATCH is partial.
    spec: USE_CASE_en.md §UC4 §Conf — PATCH must update only the provided fields.
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"

    try:
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": {"tags": ["urn:li:tag:area:fulfillment"]},
                "result_limit": 3,
            },
        )

        patch_resp = await api_client.patch(
            conf_url,
            headers=admin_headers,
            json={"dataset_filter": {"origin": "DEV", "tags": ["urn:li:tag:area:fulfillment"]}},
        )
        assert patch_resp.status_code == 200, (
            f"PATCH with origin failed: {patch_resp.status_code} {patch_resp.text}. "
            "spec: API.md §UC4 — dataset_filter unified four-dimension shape"
        )
        patch_body = patch_resp.json()
        assert patch_body["dataset_filter"].get("origin") == "DEV", (
            f"PATCH did not persist origin='DEV': {patch_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 §Conf — PATCH updates dataset_filter"
        )

    finally:
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_global_conf_put_invalid_schedule_tier_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with an unlisted schedule_tier value returns 422 at the Pydantic boundary.

    schedule_tier is a Literal["hourly","daily","weekly"] | None field; "monthly"
    is not a member of that union so Pydantic rejects it with 422 before the
    service layer is reached.  This pins the Pydantic boundary so that a
    regression back to a service-layer string comparison would still surface via
    a different error shape (no longer 422 from the schema).

    spec: API.md §UC4 / BACKEND.md — schedule_tier ∈ {"hourly","daily","weekly"};
      Pydantic Literal auto-422
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"

    resp = await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "monthly",
        },
    )
    # spec: API.md §UC4 / BACKEND.md — schedule_tier ∈ {"hourly","daily","weekly"};
    # Pydantic Literal auto-422; "monthly" is not a valid member.
    assert resp.status_code == 422, (
        f"PUT with schedule_tier='monthly' must return 422; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: API.md §UC4 / BACKEND.md — schedule_tier Pydantic Literal auto-422"
    )
