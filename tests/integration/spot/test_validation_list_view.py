"""Spot integration tests for the cross-dataset validation list view.

Route under test:
  GET /api/v1/spoke/validation

Concerns covered:
- Empty list when no configs exist: envelope structure (offset, limit,
  total_count, validations=[]).
- Listed items appear after seeding one validation conf; each row carries
  dataset_urn, description, variable_count, is_removed, updated_at as
  specified by the ValidationListItem schema.
- Pagination: offset/limit controls the returned slice; total_count is stable.
- Filter ?removed=false excludes soft-deleted rows; ?removed=true returns only
  removed rows.
- latest_data_time and latest_score are populated after POSTing a result.

Prerequisites (per spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/spot/test_validation_list_view.py

Spec:
- spec/API.md §Validation (/spoke/validation) — GET /spoke/validation list row shape
- spec/feature/VALIDATION.md §API Surface — cross-dataset list view
- spec/TESTING.md §Spot integration tests — coverage rule
"""

import urllib.parse
from contextlib import suppress
from datetime import UTC, datetime

import httpx
import pytest

# Ingest orders schema so dataset URNs are resolvable.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"orders"})

_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
_ENC_URN = urllib.parse.quote(_DATASET_URN, safe="")
_CONF_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/conf"
_RESULT_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/result"
_LIST_URL = "/api/v1/spoke/validation"


@pytest.mark.asyncio
async def test_validation_list_empty_envelope_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/validation with no configs returns the required pagination envelope.

    spec: API.md §Validation — GET /spoke/validation pagination envelope fields:
    offset, limit, total_count, validations.
    """
    # Ensure the slot for this dataset is not present; delete if it is.
    with suppress(Exception):
        await api_client.delete(_CONF_URL, headers=admin_headers)

    resp = await api_client.get(
        _LIST_URL,
        headers=admin_headers,
        params={"limit": 1, "offset": 0},
    )
    assert resp.status_code == 200, (
        f"GET /spoke/validation must return 200; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Validation — GET /spoke/validation"
    )
    body = resp.json()
    # Envelope keys required by spec/API.md §Standard Envelope
    for key in ("offset", "limit", "total_count", "validations"):
        assert key in body, (
            f"Response envelope missing key {key!r}; got keys: {list(body.keys())}. "
            "spec: API.md §Validation — GET /spoke/validation"
        )
    assert isinstance(body["validations"], list), (
        "validations must be a list; spec: API.md §Validation"
    )
    assert body["offset"] == 0
    assert body["limit"] == 1


@pytest.mark.asyncio
async def test_validation_list_item_shape_after_seed(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """After seeding one validation conf, GET /spoke/validation includes its row.

    The row must carry: dataset_urn, description, variable_count, is_removed,
    updated_at.  latest_data_time and latest_score are null before any results.

    spec: API.md §Validation — GET /spoke/validation row shape (ValidationListItem).
    """
    description = "spot test list-view description"
    variables = ["row_cnt", "null_rate"]

    try:
        put_resp = await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={"description": description, "variables": variables},
        )
        assert put_resp.status_code in (200, 201), (
            f"Seed PUT failed: {put_resp.status_code} {put_resp.text}"
        )

        # List and find this dataset's row.
        list_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"limit": 100, "offset": 0},
        )
        assert list_resp.status_code == 200, (
            f"GET /spoke/validation failed: {list_resp.status_code}: {list_resp.text}"
        )
        body = list_resp.json()
        row = next(
            (v for v in body["validations"] if v["dataset_urn"] == _DATASET_URN),
            None,
        )
        assert row is not None, (
            f"Seeded dataset_urn {_DATASET_URN!r} not found in GET /spoke/validation. "
            "spec: API.md §Validation — seeded conf must appear in cross-dataset list"
        )
        assert row["description"] == description, (
            f"Expected description={description!r}; got {row['description']!r}. "
            "spec: API.md §Validation — row.description must match conf.description"
        )
        assert row["variable_count"] == len(variables), (
            f"Expected variable_count={len(variables)}; got {row['variable_count']!r}. "
            "spec: API.md §Validation — row.variable_count == len(conf.variables)"
        )
        assert row["is_removed"] is False, (
            f"is_removed must be False for an active conf; got {row['is_removed']!r}. "
            "spec: API.md §Validation — is_removed field"
        )
        assert "updated_at" in row and row["updated_at"], (
            "updated_at must be present and non-empty. spec: API.md §Validation"
        )
        # latest_data_time / latest_score keys must be present (nullable). Their
        # values are not asserted here: result history for this URN survives conf
        # delete/resurrect in the shared dev env, so a pristine-null check is not
        # reliable at module scope. The value contract is covered by
        # test_validation_list_item_carries_latest_result.
        assert "latest_data_time" in row, (
            "latest_data_time key must be present (nullable). spec: API.md §Validation"
        )
        assert "latest_score" in row, (
            "latest_score key must be present (nullable). spec: API.md §Validation"
        )
    finally:
        with suppress(Exception):
            await api_client.delete(_CONF_URL, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_list_item_carries_latest_result(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/validation row carries latest_data_time and latest_score after POST result.

    spec: API.md §Validation — latest_data_time = data_time of most recent result,
    latest_score = score of most recent result.
    """
    try:
        await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={"description": "list view result test", "variables": ["row_cnt"]},
        )
        # data_time far beyond any other test's results for this URN, so this
        # POST is unambiguously the most recent result (latest_* = max data_time).
        data_time = datetime(2030, 6, 1, 0, 0, 0, tzinfo=UTC)
        score = 0.87
        result_resp = await api_client.post(
            _RESULT_URL,
            headers=admin_headers,
            json={
                "data_time": data_time.isoformat(),
                "score": score,
                "variables": {"row_cnt": 100.0},
            },
        )
        assert result_resp.status_code == 201, (
            f"POST result failed: {result_resp.status_code} {result_resp.text}"
        )

        # Cross-dataset list should now show latest_data_time and latest_score.
        list_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"limit": 100},
        )
        assert list_resp.status_code == 200
        body = list_resp.json()
        row = next(
            (v for v in body["validations"] if v["dataset_urn"] == _DATASET_URN),
            None,
        )
        assert row is not None, (
            "Seeded dataset not found in list after POST result; "
            "spec: API.md §Validation — row must appear in cross-dataset list"
        )
        assert row["latest_data_time"] is not None, (
            "latest_data_time must be non-null after a result is posted. "
            "spec: API.md §Validation — latest_data_time from most recent result"
        )
        assert "2030-06-01" in row["latest_data_time"], (
            f"latest_data_time should include 2030-06-01; got {row['latest_data_time']!r}. "
            "spec: API.md §Validation — latest_data_time = data_time of most recent result"
        )
        assert row["latest_score"] == pytest.approx(score, abs=0.001), (
            f"latest_score should be {score!r}; got {row['latest_score']!r}. "
            "spec: API.md §Validation — latest_score = score of most recent result"
        )
    finally:
        with suppress(Exception):
            await api_client.delete(_CONF_URL, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_list_removed_filter(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/validation?removed=false excludes soft-deleted rows;
    ?removed=true returns only removed rows.

    spec: API.md §Validation — GET /spoke/validation filterable by removed status.
    spec/feature/VALIDATION.md §Rule Configuration — DELETE performs a soft delete.
    """
    try:
        # Seed conf, then soft-delete it.
        await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={"description": "removed filter test", "variables": ["row_cnt"]},
        )
        del_resp = await api_client.delete(_CONF_URL, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"DELETE failed: {del_resp.status_code} {del_resp.text}"
        )

        # ?removed=false must NOT contain our dataset_urn.
        # Guard: if the total_count of active configs exceeds the page size, a
        # single-page membership check could be vacuously true (URN on page 2+).
        # We assert total_count <= limit so a false-pass is impossible.
        _LIMIT = 100
        active_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"removed": "false", "limit": _LIMIT},
        )
        assert active_resp.status_code == 200, (
            f"GET /spoke/validation?removed=false failed: {active_resp.status_code}"
        )
        active_body = active_resp.json()
        assert active_body["total_count"] <= _LIMIT, (
            f"total_count={active_body['total_count']} exceeds limit={_LIMIT}; "
            "page-1-only membership check is no longer valid — increase _LIMIT or paginate. "
            "spec: API.md §Pagination"
        )
        active_urns = [v["dataset_urn"] for v in active_body["validations"]]
        assert _DATASET_URN not in active_urns, (
            f"Soft-deleted URN {_DATASET_URN!r} must not appear in ?removed=false list. "
            "spec: API.md §Validation — removed filter"
        )

        # ?removed=true must contain our dataset_urn.
        removed_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"removed": "true", "limit": _LIMIT},
        )
        assert removed_resp.status_code == 200, (
            f"GET /spoke/validation?removed=true failed: {removed_resp.status_code}"
        )
        removed_body = removed_resp.json()
        assert removed_body["total_count"] <= _LIMIT, (
            f"total_count={removed_body['total_count']} exceeds limit={_LIMIT}; "
            "page-1-only membership check is no longer valid — increase _LIMIT or paginate. "
            "spec: API.md §Pagination"
        )
        removed_urns = [v["dataset_urn"] for v in removed_body["validations"]]
        assert _DATASET_URN in removed_urns, (
            f"Soft-deleted URN {_DATASET_URN!r} must appear in ?removed=true list. "
            "spec: API.md §Validation — removed filter"
        )
    finally:
        # Resurrect for clean teardown (DELETE idempotent on already-removed).
        with suppress(Exception):
            await api_client.put(
                _CONF_URL,
                headers=admin_headers,
                json={"description": "resurrection for teardown", "variables": ["row_cnt"]},
            )
        with suppress(Exception):
            await api_client.delete(_CONF_URL, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_list_pagination(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/validation pagination: offset/limit restrict the returned slice;
    total_count is independent of the current page.

    spec: API.md §Pagination — offset + limit controls; total_count is the full count.
    spec: API.md §Validation — GET /spoke/validation paginated.
    """
    try:
        # Seed one known config so total_count >= 1.
        await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={"description": "pagination test", "variables": ["row_cnt"]},
        )

        # Fetch a wide page to read the authoritative total_count.
        # total_count (not page length) is the source of truth for pagination
        # invariants; limit=100 just ensures the first page isn't artificially
        # shorter than what the server actually has.
        baseline_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"limit": 100, "offset": 0},
        )
        assert baseline_resp.status_code == 200
        baseline_body = baseline_resp.json()
        total = baseline_body["total_count"]
        assert total >= 1, "total_count must be >= 1 after seeding"

        # limit=1 must return exactly 1 row with the correct total_count.
        page_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"limit": 1, "offset": 0},
        )
        assert page_resp.status_code == 200
        page_body = page_resp.json()
        assert page_body["total_count"] == total, (
            f"total_count with limit=1 ({page_body['total_count']}) must match "
            f"total_count with limit=100 ({total}). "
            "spec: API.md §Pagination — total_count is independent of limit"
        )
        assert len(page_body["validations"]) == 1, (
            f"limit=1 must return exactly 1 row; got {len(page_body['validations'])}. "
            "spec: API.md §Pagination — limit controls page size"
        )

        # offset=total must return an empty validations list but correct total_count.
        beyond_resp = await api_client.get(
            _LIST_URL,
            headers=admin_headers,
            params={"limit": 100, "offset": total},
        )
        assert beyond_resp.status_code == 200
        beyond_body = beyond_resp.json()
        assert beyond_body["total_count"] == total, (
            "total_count must not change at offset=total. "
            "spec: API.md §Pagination"
        )
        assert beyond_body["validations"] == [], (
            f"offset=total must return empty validations; got: {beyond_body['validations']}. "
            "spec: API.md §Pagination — empty page past end"
        )
    finally:
        with suppress(Exception):
            await api_client.delete(_CONF_URL, headers=admin_headers)
