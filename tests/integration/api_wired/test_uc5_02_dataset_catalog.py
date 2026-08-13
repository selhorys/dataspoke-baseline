"""UC5 — Governance dataset catalog: end-to-end through public REST API.

The Governance "Datasets" surface lets a steward survey every registered dataset
and its cross-feature coverage (ingestion + metagen) in one list. It is backed by
the collection root GET /spoke/common/data (the same base set as
/ingestion/unmanaged and /metagen/uncovered — the dataset_registry).

This file exercises the catalog endpoint REST-only:
  - envelope shape (offset/limit/total_count/datasets) + total_count == registry;
  - row shape (dataset_urn; ingestion list of {source_id,name,mode,platform};
    validation {covered}; metagen list);
  - metagen coverage: an enabled conf whose dataset_filter matches a dataset makes
    that dataset's row list the conf; an unmatched dataset's metagen stays empty;
  - validation coverage: a dataset with a validation conf has validation.covered
    true; a dataset with none has it false;
  - ingestion empty list ([]) for a dataset no source covers (fresh-seed baseline);
  - pagination envelope + sort=dataset_urn[_desc].

The ingestion-COVERED case ({source_id,name,mode,platform} populated for a single
source) is exercised by the UC1 active-custom run in
test_uc1_02_active_custom_postgres.py step 8 (the catalog read-back after a real run),
where the api-wired pipeline naturally produces a covered dataset. The MULTIPLE-source
case (a dataset covered by several sources) is spot-owned in
tests/integration/spot/test_common_data_catalog.py — neither is duplicated here.

Prerequisites (spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/api_wired/test_uc5_02_dataset_catalog.py

spec: USE_CASE_en.md §UC5 (governance survey)
spec: API.md §Data Resource — GET /spoke/common/data (collection root)
spec: feature/FRONTEND_GOVERNANCE.md §Datasets
"""
# spec: USE_CASE_en.md §UC5

import asyncio
import time
import urllib.parse
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import dataspoke_db

# Seed catalog into DataHub so its datasets are registered in dataset_registry
# (the reset-seed datahub-sync reconciles the registry to the ingested URN set).
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_CATALOG_TITLE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_CATALOG_EDITIONS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)

_CATALOG_URL = "/api/v1/spoke/common/data"
_METAGEN_CONF_URL = "/api/v1/spoke/metagen/conf"

# Consumed by the api-wired purge_urns autouse fixture (conftest.py).
URNS_TO_PURGE: list[str] = [_CATALOG_TITLE_URN, _CATALOG_EDITIONS_URN]


@pytest_asyncio.fixture(autouse=True)
async def _clean_ingestion_sources() -> AsyncGenerator[None]:
    """Reset ingestion_source rows before/after each test so the catalog's
    ingestion coverage starts from a no-source (all-null) baseline.

    DB-touching setup/teardown lives in the fixture; the test body stays REST-only.
    spec: TESTING.md §Api-Wired Integration Tests — setup/teardown may use util.
    """
    await dataspoke_db.reset_ingestion_sources()
    yield
    await dataspoke_db.reset_ingestion_sources()


@pytest_asyncio.fixture(autouse=True)
async def _registry_synced(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
    _clean_ingestion_sources: None,  # ordering: sync after the source reset
) -> None:
    """Populate dataset_registry from DataHub before the catalog assertions run.

    dataset_registry starts EMPTY after the reset; POST
    /internal/activities/ingestion/sync is its sole writer (reconciles from
    DataHub). DataHub ES indexing lags ~2-3 min after reset-seed, so re-trigger
    sync on every iteration until the seeded catalog datasets are registered
    (180s budget / 5s interval). The system sources reconciled by sync declare
    no postgres patterns, so they map no catalog datasets — ingestion stays null.

    spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep populates
      dataset_registry from DataHub.
    spec: project_es_indexing_lag_after_reset_seed — ES lag ~2-3 min after seed.
    spec: tests/integration/api_wired/test_uc1_03_passive_kafka.py step 0 — sync-poll pattern.
    """
    required = {_CATALOG_TITLE_URN, _CATALOG_EDITIONS_URN}
    deadline = time.time() + 180.0
    registered: set[str] = set()
    while time.time() < deadline:
        # Re-sync each iteration: newly ES-indexed URNs surface on each call.
        sync_resp = await api_client.post(
            "/internal/activities/ingestion/sync", headers=internal_headers
        )
        if sync_resp.status_code == 200:
            cat_resp = await api_client.get(
                f"{_CATALOG_URL}?limit=500", headers=admin_headers
            )
            if cat_resp.status_code == 200:
                registered = {d["dataset_urn"] for d in cat_resp.json()["datasets"]}
                if required <= registered:
                    return
        await asyncio.sleep(5.0)
    raise AssertionError(
        "catalog datasets not registered within 180s; sync populates "
        "dataset_registry from DataHub and ES indexing may lag ~2-3 min. Missing: "
        f"{sorted(required - registered)}. "
        "spec: feature/BACKEND.md §Ingestion Service — Sync sweep; "
        "spec: project_es_indexing_lag_after_reset_seed"
    )


def _row_for(body: dict, urn: str) -> dict | None:
    return next((d for d in body["datasets"] if d["dataset_urn"] == urn), None)


@pytest.mark.asyncio
async def test_catalog_envelope_and_row_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/data returns the paginated envelope and the documented
    per-row shape for every registered dataset.

    spec: API.md §Data Resource — GET /spoke/common/data: envelope
    (offset/limit/total_count/datasets); each row carries dataset_urn,
    ingestion (list of {source_id,name,mode,platform}), validation ({covered}),
    metagen (list of {conf_id,name}).
    """
    resp = await api_client.get(
        _CATALOG_URL, headers=admin_headers, params={"limit": 200, "offset": 0}
    )
    assert resp.status_code == 200, (
        f"GET /spoke/common/data must return 200; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Data Resource — GET /spoke/common/data"
    )
    body = resp.json()
    for key in ("offset", "limit", "total_count", "datasets"):
        assert key in body, (
            f"catalog envelope missing key {key!r}; got {list(body)}. "
            "spec: API.md §Data Resource — GET /spoke/common/data envelope"
        )
    assert isinstance(body["datasets"], list)
    assert body["offset"] == 0
    assert body["limit"] == 200

    # The seeded catalog datasets are registered and must appear in the catalog.
    title_row = _row_for(body, _CATALOG_TITLE_URN)
    assert title_row is not None, (
        f"{_CATALOG_TITLE_URN!r} must appear in the dataset catalog "
        "(registered catalog dataset). "
        "spec: API.md §Data Resource — lists every registered dataset (dataset_registry)"
    )

    # Per-row shape contract holds for every row on the page.
    for row in body["datasets"]:
        assert isinstance(row["dataset_urn"], str) and row["dataset_urn"], (
            f"row.dataset_urn must be a non-empty string; got {row.get('dataset_urn')!r}."
        )
        ingestion = row["ingestion"]
        assert isinstance(ingestion, list), (
            f"row.ingestion must be a list (empty when no source covers it); "
            f"got {type(ingestion).__name__}. "
            "spec: API.md §Data Resource — row.ingestion is a list of covering sources"
        )
        for src in ingestion:
            assert {"source_id", "name", "mode", "platform"} <= set(src), (
                f"each ingestion entry must carry source_id/name/mode/platform; got {src!r}. "
                "spec: API.md §Data Resource — row.ingestion entry shape"
            )
        # validation coverage is a {covered: bool} object on every row.
        assert isinstance(row["validation"], dict) and isinstance(
            row["validation"].get("covered"), bool
        ), (
            f"row.validation must carry a bool 'covered'; got {row.get('validation')!r}. "
            "spec: API.md §Data Resource — row.validation ({covered})"
        )
        assert isinstance(row["metagen"], list), (
            f"row.metagen must be a list; got {type(row['metagen']).__name__}. "
            "spec: API.md §Data Resource — row.metagen is a list of {conf_id,name}"
        )
        for conf in row["metagen"]:
            assert {"conf_id", "name"} <= set(conf), (
                f"each metagen entry must carry conf_id+name; got {conf!r}. "
                "spec: API.md §Data Resource — row.metagen entry shape"
            )

    # With ingestion_source reset, no source covers any catalog dataset → empty list.
    assert title_row["ingestion"] == [], (
        "title_master ingestion must be an empty list with no source covering it. "
        "spec: API.md §Data Resource — row.ingestion is [] when unmanaged"
    )

    # total_count equals the number of rows when a single wide page holds them all.
    assert body["total_count"] == len(body["datasets"]), (
        f"total_count ({body['total_count']}) must equal the row count "
        f"({len(body['datasets'])}) on a page wide enough to hold the whole registry. "
        "spec: API.md §Standard Envelope — total_count is the full registry count"
    )


@pytest.mark.asyncio
async def test_catalog_metagen_coverage(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """An enabled metagen conf whose dataset_filter matches a dataset makes that
    dataset's catalog row list the conf; an unmatched dataset's metagen stays [].

    spec: API.md §Data Resource — row.metagen lists enabled metagen confs whose
    dataset_filter matches the dataset (possibly empty).
    """
    conf_id: str | None = None
    conf_name = f"uc5-catalog-{uuid.uuid4().hex[:8]}"
    try:
        # Create an enabled conf scoped to title_master only.
        post_conf = await api_client.post(
            _METAGEN_CONF_URL,
            headers=admin_headers,
            json={
                "name": conf_name,
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": f"dataset_urn = '{_CATALOG_TITLE_URN}'",
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert post_conf.status_code == 201, (
            f"POST metagen conf failed: {post_conf.status_code} {post_conf.text}. "
            "spec: API.md §Metadata Generation — POST /metagen/conf → 201"
        )
        conf_id = post_conf.json()["id"]

        resp = await api_client.get(
            _CATALOG_URL, headers=admin_headers, params={"limit": 200}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        title_row = _row_for(body, _CATALOG_TITLE_URN)
        editions_row = _row_for(body, _CATALOG_EDITIONS_URN)
        assert title_row is not None and editions_row is not None, (
            "both catalog datasets must be registered and present in the catalog."
        )

        # title_master is matched → its metagen lists the conf we created.
        matched = [c for c in title_row["metagen"] if c["conf_id"] == conf_id]
        assert len(matched) == 1, (
            f"title_master metagen must list the matching conf {conf_id!r}; "
            f"got {title_row['metagen']}. "
            "spec: API.md §Data Resource — row.metagen lists matching enabled confs"
        )
        assert matched[0]["name"] == conf_name, (
            f"matched conf name must be {conf_name!r}; got {matched[0]['name']!r}. "
            "spec: API.md §Data Resource — row.metagen entry carries the conf name"
        )

        # editions is NOT in the conf's filter → its metagen excludes this conf.
        assert all(c["conf_id"] != conf_id for c in editions_row["metagen"]), (
            f"editions (outside the conf filter) must NOT list conf {conf_id!r}; "
            f"got {editions_row['metagen']}. "
            "spec: API.md §Data Resource — only matching confs appear in row.metagen"
        )
    finally:
        if conf_id is not None:
            await api_client.delete(
                f"{_METAGEN_CONF_URL}/{conf_id}", headers=admin_headers
            )


@pytest.mark.asyncio
async def test_catalog_validation_coverage(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A dataset with a validation conf has validation.covered true; a dataset with
    none has it false.

    spec: API.md §Data Resource — row.validation.covered is true when a validation
    conf exists for the dataset, false otherwise.
    """
    enc_title = urllib.parse.quote(_CATALOG_TITLE_URN, safe="")
    val_conf_url = f"/api/v1/spoke/common/data/{enc_title}/attr/validation/conf"
    try:
        # title_master gets a validation conf → covered; editions keeps none.
        put = await api_client.put(
            val_conf_url,
            headers=admin_headers,
            json={
                "description": "UC5 catalog coverage seed",
                "variables": [{"name": "row_cnt", "description": "row count"}],
            },
        )
        assert put.status_code in (200, 201), (
            f"PUT validation conf failed: {put.status_code} {put.text}. "
            "spec: API.md §Validation — PUT attr/validation/conf"
        )

        resp = await api_client.get(
            _CATALOG_URL, headers=admin_headers, params={"limit": 200}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        title_row = _row_for(body, _CATALOG_TITLE_URN)
        editions_row = _row_for(body, _CATALOG_EDITIONS_URN)
        assert title_row is not None and editions_row is not None, (
            "both catalog datasets must be registered and present in the catalog."
        )

        assert title_row["validation"]["covered"] is True, (
            "title_master must have validation.covered=true after a conf is created. "
            "spec: API.md §Data Resource — covered when a validation conf exists"
        )
        assert editions_row["validation"]["covered"] is False, (
            "editions (no validation conf) must have validation.covered=false. "
            "spec: API.md §Data Resource — uncovered when no conf exists"
        )
    finally:
        await api_client.delete(val_conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_catalog_pagination(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Pagination: limit controls page size; total_count is independent of the
    page; offset=total returns an empty page with the same total_count.

    spec: API.md §Pagination — offset/limit controls; total_count is the full count.
    """
    wide = await api_client.get(_CATALOG_URL, headers=admin_headers, params={"limit": 200})
    assert wide.status_code == 200, wide.text
    total = wide.json()["total_count"]
    assert total >= 1, "registry must hold at least the seeded catalog datasets"

    page = await api_client.get(
        _CATALOG_URL, headers=admin_headers, params={"limit": 1, "offset": 0}
    )
    assert page.status_code == 200
    page_body = page.json()
    assert page_body["total_count"] == total, (
        f"total_count with limit=1 ({page_body['total_count']}) must equal the wide "
        f"page total_count ({total}). spec: API.md §Pagination — total_count is fixed"
    )
    assert len(page_body["datasets"]) == 1, (
        f"limit=1 must return exactly one row; got {len(page_body['datasets'])}. "
        "spec: API.md §Pagination — limit controls page size"
    )

    beyond = await api_client.get(
        _CATALOG_URL, headers=admin_headers, params={"limit": 200, "offset": total}
    )
    assert beyond.status_code == 200
    beyond_body = beyond.json()
    assert beyond_body["total_count"] == total, (
        "total_count must not change at offset=total. spec: API.md §Pagination"
    )
    assert beyond_body["datasets"] == [], (
        f"offset=total must return an empty page; got {beyond_body['datasets']}. "
        "spec: API.md §Pagination — empty page past the end"
    )


@pytest.mark.asyncio
async def test_catalog_sort_by_dataset_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """The default (no `sort`) order is ascending dataset_urn; sort=dataset_urn
    matches it; sort=dataset_urn_desc returns the exact reverse over the same page.

    spec: API.md §Data Resource — GET /spoke/common/data sortable by dataset_urn
    (dataset_urn / dataset_urn_desc, default dataset_urn_asc).
    """
    default = await api_client.get(
        _CATALOG_URL, headers=admin_headers, params={"limit": 200}
    )
    asc = await api_client.get(
        _CATALOG_URL,
        headers=admin_headers,
        params={"limit": 200, "sort": "dataset_urn"},
    )
    desc = await api_client.get(
        _CATALOG_URL,
        headers=admin_headers,
        params={"limit": 200, "sort": "dataset_urn_desc"},
    )
    assert default.status_code == 200, default.text
    assert asc.status_code == 200 and desc.status_code == 200, (asc.text, desc.text)
    default_urns = [d["dataset_urn"] for d in default.json()["datasets"]]
    asc_urns = [d["dataset_urn"] for d in asc.json()["datasets"]]
    desc_urns = [d["dataset_urn"] for d in desc.json()["datasets"]]

    # Default sort (no `sort` param) is dataset_urn ascending, and equals the
    # explicit sort=dataset_urn page.
    # spec: API.md §Data Resource — default dataset_urn_asc.
    assert default_urns == sorted(default_urns), (
        "the default (no `sort`) order must be ascending dataset_urn. "
        "spec: API.md §Data Resource — default dataset_urn_asc"
    )
    assert default_urns == asc_urns, (
        "the default order must equal sort=dataset_urn (the documented default). "
        "spec: API.md §Data Resource — default dataset_urn_asc"
    )
    assert asc_urns == sorted(asc_urns), (
        "sort=dataset_urn must return ascending dataset_urn order. "
        "spec: API.md §Data Resource — sortable by dataset_urn"
    )
    assert desc_urns == list(reversed(asc_urns)), (
        "sort=dataset_urn_desc must be the exact reverse of the ascending order. "
        "spec: API.md §Data Resource — dataset_urn_desc"
    )
