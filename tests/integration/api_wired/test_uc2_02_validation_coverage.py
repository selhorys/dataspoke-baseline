"""UC2 — Validation coverage filter: end-to-end through public REST API.

GET /spoke/validation takes a `coverage` query param selecting the row set:
  covered   (default) — datasets that hold a validation slot (current behavior);
  uncovered           — registered datasets (dataset_registry) with no conf;
  both                — the union.
Uncovered rows carry null description / variable_count / latest_data_time /
latest_score; in uncovered/both the ordering is tiebroken by dataset_urn so paging
stays deterministic.

Steps mirror the UC2 validation narrative — a steward registers a rule on one
dataset, then surveys coverage across the catalog.

Prerequisites (spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/api_wired/test_uc2_02_validation_coverage.py

spec: USE_CASE_en.md §UC2
spec: API.md §Validation — GET /spoke/validation coverage param
spec: feature/VALIDATION.md §API Surface — cross-dataset list view
"""
# spec: USE_CASE_en.md §UC2

import asyncio
import time
import urllib.parse
from contextlib import suppress

import httpx
import pytest
import pytest_asyncio

# Seed catalog so its datasets are registered (the uncovered set draws from the
# registry). spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# title_master holds the conf (covered); editions stays registered-no-conf
# (uncovered). Both are seeded catalog datasets.
_COVERED_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_UNCOVERED_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)
_COVERED_CONF_URL = (
    f"/api/v1/spoke/common/data/{urllib.parse.quote(_COVERED_URN, safe='')}"
    "/attr/validation/conf"
)
_LIST_URL = "/api/v1/spoke/validation"

# Consumed by the api-wired purge_urns autouse fixture (conftest.py).
URNS_TO_PURGE: list[str] = [_COVERED_URN, _UNCOVERED_URN]

_DESCRIPTION = "UC2 coverage-filter conf for title_master"
_VARIABLES = [
    {"name": "row_cnt", "description": "Daily row count"},
    {"name": "null_rate", "description": "Null rate"},
]


_CATALOG_URL = "/api/v1/spoke/common/data"


@pytest_asyncio.fixture(autouse=True)
async def _registry_synced(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """Populate dataset_registry from DataHub before the coverage assertions run.

    dataset_registry starts EMPTY after the reset; POST
    /internal/activities/ingestion/sync is its sole writer (reconciles from
    DataHub). The uncovered set is the registry minus confs, so it is empty until
    sync runs. DataHub ES indexing lags ~2-3 min after reset-seed, so re-trigger
    sync each iteration until the seeded catalog datasets are registered (180s
    budget / 5s interval). Registry membership is probed via GET /spoke/common/data
    (the dataset_registry view) so the check is independent of validation-conf
    state left over from a prior run.

    spec: feature/BACKEND.md §Ingestion Service — Sync sweep populates
      dataset_registry from DataHub.
    spec: project_es_indexing_lag_after_reset_seed — ES lag ~2-3 min after seed.
    spec: tests/integration/api_wired/test_uc1_03_passive_kafka.py step 0 — sync-poll pattern.
    """
    required = {_COVERED_URN, _UNCOVERED_URN}
    deadline = time.time() + 180.0
    registered: set[str] = set()
    while time.time() < deadline:
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
    return next((v for v in body["validations"] if v["dataset_urn"] == urn), None)


@pytest.mark.asyncio
async def test_uc2_validation_coverage_filter(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 narrative: 'As a steward, after registering a validation rule on one
    dataset I want to survey which registered datasets are covered and which are
    not, in one list.'

    Steps:
      1. PUT a validation conf on title_master (covered); editions stays uncovered.
      2. coverage=covered (default): title_master present; editions absent.
      3. coverage=uncovered: editions present with null conf/latest fields;
         title_master absent.
      4. coverage=both: union — title_master with conf fields, editions with nulls;
         deterministic ordering.
    """
    try:
        # ── Step 1: register the rule on title_master ─────────────────────────
        put_resp = await api_client.put(
            _COVERED_CONF_URL,
            headers=admin_headers,
            json={"description": _DESCRIPTION, "variables": _VARIABLES},
        )
        assert put_resp.status_code in (200, 201), (
            f"Seed PUT conf failed: {put_resp.status_code} {put_resp.text}. "
            "spec: API.md §Validation — PUT attr/validation/conf"
        )

        # ── Step 2: coverage=covered (the default) ────────────────────────────
        # Assert the default (no param) equals coverage=covered, and that the
        # covered set holds the conf'd dataset but not the registered-no-conf one.
        for params in ({"limit": 200}, {"limit": 200, "coverage": "covered"}):
            covered_resp = await api_client.get(
                _LIST_URL, headers=admin_headers, params=params
            )
            assert covered_resp.status_code == 200, (
                f"GET /spoke/validation {params} must return 200; "
                f"got {covered_resp.status_code}: {covered_resp.text}. "
                "spec: API.md §Validation — coverage default is 'covered'"
            )
            covered_body = covered_resp.json()
            covered_row = _row_for(covered_body, _COVERED_URN)
            assert covered_row is not None, (
                f"{_COVERED_URN!r} (has a conf) must appear under coverage=covered "
                f"({params}). spec: API.md §Validation — covered = datasets with a slot"
            )
            assert covered_row["description"] == _DESCRIPTION, (
                f"covered row.description must echo the conf; got "
                f"{covered_row['description']!r}. spec: API.md §Validation"
            )
            assert covered_row["variable_count"] == len(_VARIABLES), (
                f"covered row.variable_count must be {len(_VARIABLES)}; got "
                f"{covered_row['variable_count']!r}. spec: API.md §Validation"
            )
            assert _row_for(covered_body, _UNCOVERED_URN) is None, (
                f"{_UNCOVERED_URN!r} (no conf) must NOT appear under coverage=covered. "
                "spec: API.md §Validation — covered excludes registered-no-conf datasets"
            )

        # ── Step 3: coverage=uncovered ────────────────────────────────────────
        uncovered_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"limit": 200, "coverage": "uncovered"},
        )
        assert uncovered_resp.status_code == 200, (
            f"coverage=uncovered must return 200; got {uncovered_resp.status_code}: "
            f"{uncovered_resp.text}. spec: API.md §Validation — coverage=uncovered"
        )
        uncovered_body = uncovered_resp.json()
        uncovered_row = _row_for(uncovered_body, _UNCOVERED_URN)
        assert uncovered_row is not None, (
            f"{_UNCOVERED_URN!r} (registered, no conf) must appear under "
            "coverage=uncovered. spec: API.md §Validation — uncovered = registry minus confs"
        )
        # Uncovered rows carry null conf + latest fields.
        for field in ("description", "variable_count", "latest_data_time", "latest_score"):
            assert uncovered_row[field] is None, (
                f"uncovered row.{field} must be null; got {uncovered_row[field]!r}. "
                "spec: API.md §Validation — uncovered rows carry null conf/latest fields"
            )
        assert _row_for(uncovered_body, _COVERED_URN) is None, (
            f"{_COVERED_URN!r} (has a conf) must NOT appear under coverage=uncovered. "
            "spec: API.md §Validation — uncovered excludes datasets with a slot"
        )

        # ── Step 4: coverage=both (union) ─────────────────────────────────────
        both_resp = await api_client.get(
            _LIST_URL, headers=admin_headers, params={"limit": 200, "coverage": "both"}
        )
        assert both_resp.status_code == 200, (
            f"coverage=both must return 200; got {both_resp.status_code}: "
            f"{both_resp.text}. spec: API.md §Validation — coverage=both"
        )
        both_body = both_resp.json()
        both_covered = _row_for(both_body, _COVERED_URN)
        both_uncovered = _row_for(both_body, _UNCOVERED_URN)
        assert both_covered is not None and both_uncovered is not None, (
            "coverage=both must union covered and uncovered datasets — both "
            f"{_COVERED_URN!r} and {_UNCOVERED_URN!r} must appear. "
            "spec: API.md §Validation — both = union"
        )
        # The covered dataset keeps its conf fields; the uncovered one stays null.
        assert both_covered["description"] == _DESCRIPTION, (
            "in coverage=both the covered dataset keeps its conf description. "
            "spec: API.md §Validation — covered rows carry conf fields"
        )
        assert both_covered["variable_count"] == len(_VARIABLES)
        assert both_uncovered["description"] is None, (
            "in coverage=both the uncovered dataset carries null conf fields. "
            "spec: API.md §Validation — uncovered rows null in both"
        )
        assert both_uncovered["variable_count"] is None

        # Deterministic paging: the same query twice yields the same row order.
        both_again = await api_client.get(
            _LIST_URL, headers=admin_headers, params={"limit": 200, "coverage": "both"}
        )
        assert both_again.status_code == 200
        order_1 = [v["dataset_urn"] for v in both_body["validations"]]
        order_2 = [v["dataset_urn"] for v in both_again.json()["validations"]]
        assert order_1 == order_2, (
            "coverage=both ordering must be deterministic across identical requests "
            "(tiebroken by dataset_urn). spec: API.md §Validation — deterministic paging"
        )
    finally:
        with suppress(Exception):
            await api_client.delete(_COVERED_CONF_URL, headers=admin_headers)
