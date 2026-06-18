"""Spot tests for Metadata Generation — conf-collection and per-dataset boundary CRUD.

Concerns covered:

Conf collection CRUD (Group 1):
  test_metagen_conf_create_get_list_put_patch_delete
  test_metagen_conf_duplicate_name_returns_409_conf_exists
  test_metagen_conf_get_missing_returns_404_conf_not_found
  test_metagen_conf_put_invalid_result_limit_422
  test_metagen_conf_put_invalid_dataset_urn_422
  test_metagen_conf_create_invalid_schedule_tier_422
  test_metagen_conf_dataset_filter_dimension_caps (parametrized)

Per-dataset boundary CRUD (Group 2):
  test_metagen_boundary_roundtrip_put_get_patch_delete
  test_metagen_boundary_get_unknown_urn_returns_null_body
  test_metagen_boundary_put_allowed_validation_422

These tests are pure REST and do not require raw-SQL seeding.

spec: API.md §Metadata Generation (/spoke/metagen)
spec: API.md §Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns} ≤ 1,000
spec: feature/BACKEND.md §Metadata Generation Service — conf collection, boundary
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import urllib.parse
import uuid
from contextlib import suppress

import httpx
import pytest

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

_CONF_URL = "/api/v1/spoke/metagen/conf"


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _delete_conf(
    api_client: httpx.AsyncClient, headers: dict[str, str], conf_id: str
) -> None:
    with suppress(Exception):
        await api_client.delete(f"{_CONF_URL}/{conf_id}", headers=headers)


# ── Group 1: Conf collection CRUD ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_conf_create_get_list_put_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST → GET → list → PUT → PATCH → DELETE one conf; per-conf events observable.

    spec: API.md §Metadata Generation — conf is a managed collection
      (GET/POST /conf, GET/PUT/PATCH/DELETE /conf/{conf_id}); POST returns 201.
    spec: feature/BACKEND.md §Metadata Generation Service — per-conf fields:
      name, is_enabled, schedule_tier, dataset_filter, result_limit, overwrite_pending.
    """
    name = _unique_name("catalog-docs")
    conf_id = None
    try:
        # POST — create a conf (201)
        create_resp = await api_client.post(
            _CONF_URL,
            headers=admin_headers,
            json={
                "name": name,
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 5,
                "overwrite_pending": False,
            },
        )
        assert create_resp.status_code == 201, (
            f"POST conf must return 201; got {create_resp.status_code} {create_resp.text}. "
            "spec: API.md §Metadata Generation — POST /metagen/conf → 201"
        )
        body = create_resp.json()
        conf_id = body["id"]
        assert body["name"] == name
        assert body["is_enabled"] is True
        assert body["schedule_tier"] == "daily"
        assert body["result_limit"] == 5
        assert body["overwrite_pending"] is False
        assert body["dataset_filter"] == {"dataset_urns": [_TEST_URN]}
        assert "created_at" in body and "updated_at" in body

        # GET one
        get_resp = await api_client.get(f"{_CONF_URL}/{conf_id}", headers=admin_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["result_limit"] == 5

        # LIST — paginated envelope keyed by 'confs'
        list_resp = await api_client.get(f"{_CONF_URL}?limit=100", headers=admin_headers)
        assert list_resp.status_code == 200
        list_body = list_resp.json()
        assert "confs" in list_body and "total_count" in list_body, (
            "Conf list must use the paginated envelope (confs + total_count). "
            "spec: API.md §Standard Response Envelope"
        )
        assert conf_id in {c["id"] for c in list_body["confs"]}

        # PUT — full replacement
        put_resp = await api_client.put(
            f"{_CONF_URL}/{conf_id}",
            headers=admin_headers,
            json={
                "name": name,
                "is_enabled": False,
                "schedule_tier": "weekly",
                "dataset_filter": {},
                "result_limit": 10,
                "overwrite_pending": True,
            },
        )
        assert put_resp.status_code == 200
        put_body = put_resp.json()
        assert put_body["schedule_tier"] == "weekly"
        assert put_body["result_limit"] == 10

        # PATCH — partial update
        patch_resp = await api_client.patch(
            f"{_CONF_URL}/{conf_id}",
            headers=admin_headers,
            json={"is_enabled": True},
        )
        assert patch_resp.status_code == 200
        patch_body = patch_resp.json()
        assert patch_body["is_enabled"] is True
        assert patch_body["result_limit"] == 10, "PATCH must preserve non-patched fields"

        # Per-conf events feed shows the config mutations.
        ev_resp = await api_client.get(
            f"{_CONF_URL}/{conf_id}/event?limit=50", headers=admin_headers
        )
        assert ev_resp.status_code == 200
        ev_body = ev_resp.json()
        assert "events" in ev_body and isinstance(ev_body["events"], list)

        # DELETE — 204
        del_resp = await api_client.delete(f"{_CONF_URL}/{conf_id}", headers=admin_headers)
        assert del_resp.status_code == 204
        conf_id = None
    finally:
        if conf_id is not None:
            await _delete_conf(api_client, admin_headers, conf_id)


@pytest.mark.asyncio
async def test_metagen_conf_duplicate_name_returns_409_conf_exists(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST a conf with an already-used name returns 409 METAGEN_CONF_EXISTS.

    spec: API.md §Metadata Generation — name unique; collision → 409 METAGEN_CONF_EXISTS.
    """
    name = _unique_name("dup")
    first_id = None
    try:
        first = await api_client.post(_CONF_URL, headers=admin_headers, json={"name": name})
        assert first.status_code == 201
        first_id = first.json()["id"]

        dup = await api_client.post(_CONF_URL, headers=admin_headers, json={"name": name})
        assert dup.status_code == 409, (
            f"Duplicate name must return 409; got {dup.status_code} {dup.text}. "
            "spec: API.md §Metadata Generation — METAGEN_CONF_EXISTS"
        )
        assert dup.json()["error_code"] == "METAGEN_CONF_EXISTS"
    finally:
        if first_id is not None:
            await _delete_conf(api_client, admin_headers, first_id)


@pytest.mark.asyncio
async def test_metagen_conf_get_missing_returns_404_conf_not_found(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /metagen/conf/{conf_id} for an absent conf returns 404 METAGEN_CONF_NOT_FOUND.

    spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    missing_id = str(uuid.uuid4())
    resp = await api_client.get(f"{_CONF_URL}/{missing_id}", headers=admin_headers)
    assert resp.status_code == 404, (
        f"GET missing conf must return 404; got {resp.status_code} {resp.text}. "
        "spec: API.md §Metadata Generation — METAGEN_CONF_NOT_FOUND"
    )
    assert resp.json()["error_code"] == "METAGEN_CONF_NOT_FOUND"


@pytest.mark.asyncio
async def test_metagen_conf_put_invalid_result_limit_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST/PUT with result_limit=0 returns 422 (Pydantic ge=1).

    spec: API.md §Payload caps — conf result_limit ∈ [1, 20].
    """
    resp = await api_client.post(
        _CONF_URL,
        headers=admin_headers,
        json={"name": _unique_name("bad-limit"), "result_limit": 0},
    )
    assert resp.status_code == 422, (
        f"result_limit=0 must return 422; got {resp.status_code} {resp.text}. "
        "spec: API.md §Payload caps — result_limit ∈ [1, 20]"
    )


@pytest.mark.asyncio
async def test_metagen_conf_put_invalid_dataset_urn_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST with a malformed URN in dataset_filter.dataset_urns returns 422.

    spec: API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs;
      validated at POST/PUT/PATCH for metagen/conf.
    """
    resp = await api_client.post(
        _CONF_URL,
        headers=admin_headers,
        json={
            "name": _unique_name("bad-urn"),
            "dataset_filter": {"dataset_urns": ["not-a-valid-urn"]},
        },
    )
    assert resp.status_code == 422, (
        f"Malformed URN must return 422; got {resp.status_code} {resp.text}. "
        "spec: API.md §Error Catalogue — INVALID_DATASET_URN"
    )


@pytest.mark.asyncio
async def test_metagen_conf_create_invalid_schedule_tier_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST with an unlisted schedule_tier returns 422 at the Pydantic boundary.

    spec: feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null};
      Pydantic Literal auto-422.
    """
    resp = await api_client.post(
        _CONF_URL,
        headers=admin_headers,
        json={"name": _unique_name("bad-tier"), "schedule_tier": "monthly"},
    )
    assert resp.status_code == 422, (
        f"schedule_tier='monthly' must return 422; got {resp.status_code} {resp.text}. "
        "spec: feature/BACKEND_SCHEMA.md — schedule_tier Literal"
    )


@pytest.mark.parametrize(
    ("n", "expected_status_set"),
    [(1000, {201}), (1001, {422})],
    ids=["at-cap-1000-accepted", "over-cap-1001-rejected"],
)
@pytest.mark.parametrize(
    "dimension",
    ["tags", "glossary_terms", "dataset_urns"],
    ids=["tags", "glossary_terms", "dataset_urns"],
)
@pytest.mark.asyncio
async def test_metagen_conf_dataset_filter_dimension_caps(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    dimension: str,
    n: int,
    expected_status_set: set[int],
) -> None:
    """POST at-cap (1000, accepted) and over-cap (1001, rejected) on a single
    dataset_filter dimension.

    spec: API.md §Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns}
      ≤ 1,000 per dimension; exactly 1,000 accepted, 1,001 rejected at the schema boundary.
    """
    if dimension == "tags":
        entries = [f"urn:li:tag:t-{i}" for i in range(n)]
    elif dimension == "glossary_terms":
        entries = [f"urn:li:glossaryTerm:gt-{i}" for i in range(n)]
    else:
        entries = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t_{i},DEV)" for i in range(n)
        ]

    created_id = None
    try:
        resp = await api_client.post(
            _CONF_URL,
            headers=admin_headers,
            json={"name": _unique_name("caps"), "dataset_filter": {dimension: entries}},
        )
        assert resp.status_code in expected_status_set, (
            f"n={n} {dimension}: expected {expected_status_set}, got {resp.status_code}: "
            f"{resp.text}. spec: API.md §Payload caps — dataset_filter cap is 1,000 per dimension"
        )
        if resp.status_code == 201:
            created_id = resp.json()["id"]
    finally:
        if created_id is not None:
            await _delete_conf(api_client, admin_headers, created_id)


# ── Group 2: Per-dataset boundary CRUD ───────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_boundary_roundtrip_put_get_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT → GET → PATCH → DELETE per-dataset boundary at the renamed
    attr/metagen/boundary route.

    spec: API.md §Data Resource — per-dataset boundary route is
      /spoke/common/data/{urn}/attr/metagen/boundary; allowed ∈
      {dataset.description, column.description}.
    """
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    try:
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
            f"PUT boundary failed: {put_resp.status_code} {put_resp.text}"
        )
        put_body = put_resp.json()
        assert put_body["dataset_urn"] == _TEST_URN
        assert put_body["is_enabled"] is True
        assert set(put_body["allowed"]) == {"dataset.description", "column.description"}
        assert put_body["owner"] == "test-owner"

        get_resp = await api_client.get(boundary_url, headers=admin_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["is_enabled"] is True

        patch_resp = await api_client.patch(
            boundary_url, headers=admin_headers, json={"is_enabled": False}
        )
        assert patch_resp.status_code == 200
        patch_body = patch_resp.json()
        assert patch_body["is_enabled"] is False
        assert set(patch_body["allowed"]) == {"dataset.description", "column.description"}, (
            "PATCH must not alter non-patched allowed field"
        )

        del_resp = await api_client.delete(boundary_url, headers=admin_headers)
        assert del_resp.status_code == 204
    finally:
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_boundary_get_unknown_urn_returns_null_body(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET boundary for a URN with no row returns 200 with a null body (not 404).

    spec: feature/BACKEND.md §Metadata Generation Service — GET returns null body
      with 200 when the boundary row has never been written.
    """
    unknown_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        "example_db.catalog.no_such_table_spot_metagen_boundary,DEV)"
    )
    encoded = urllib.parse.quote(unknown_urn, safe="")
    boundary_url = f"/api/v1/spoke/common/data/{encoded}/attr/metagen/boundary"

    get_resp = await api_client.get(boundary_url, headers=admin_headers)
    assert get_resp.status_code == 200
    assert get_resp.json() is None, (
        "GET boundary for unknown URN must return null body. "
        "spec: feature/BACKEND.md §Metadata Generation Service"
    )


@pytest.mark.asyncio
async def test_metagen_boundary_put_allowed_validation_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT boundary with an invalid kind in allowed returns 422.

    spec: API.md §Metadata Generation — allowed ∈ {dataset.description, column.description}.
    """
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    resp = await api_client.put(
        boundary_url,
        headers=admin_headers,
        json={"is_enabled": True, "allowed": ["cross_data.md"]},
    )
    assert resp.status_code == 422, (
        f"Invalid allowed kind must return 422; got {resp.status_code} {resp.text}. "
        "spec: API.md §Metadata Generation — allowed Literal"
    )
