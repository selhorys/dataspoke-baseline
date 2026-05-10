"""Unit tests for the pct_rules_passing measurer (validation_score.py).

Tests the spec-mandated input → output transformation:
- Empty datasets list returns (0.0, breakdown with dataset_count=0, empty datasets list).
- All datasets with score=1.0 → aggregate value=0.0 (no failing datasets).
- A dataset with score<1.0 is categorised as failing (category="failing").
- A dataset with no validation result is categorised as failing.
- Returned breakdown shape matches spec: dataset_count, datasets[]{urn, category, detail}.

spec: feature/BACKEND.md §Metrics Service — baseline measurers:
      pct_rules_passing: a dataset is passing iff its most recent result has score==1.0;
      datasets with no result are counted as failing; returns failing_count as the metric value.
      Breakdown format: {"dataset_count": <int>, "datasets": [{"urn": ..., "category": ...,
      "detail": {...}}]} (BACKEND.md lines 610-622).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

# Import side-effect: ensures the measurer registers itself.
from src.backend.metrics.measurers.validation_score import measure


# ── Empty dataset list ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_datasets_returns_zero_failing() -> None:
    """measure([]) must return (0.0, breakdown with all zeros) without querying DB.

    spec: feature/BACKEND.md §Metrics Service — empty dataset list is a no-op.
    """
    value, breakdown = await measure(
        datasets=[],
        datahub=MagicMock(),
        db=AsyncMock(),
    )
    assert value == 0.0
    assert breakdown["dataset_count"] == 0
    assert breakdown["datasets"] == []


# ── All passing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_datasets_passing_returns_zero_failing() -> None:
    """All datasets with latest score==1.0 → aggregate value=0.0 (no failing datasets).

    spec: feature/BACKEND.md §Metrics Service — dataset is passing iff score==1.0.
    """
    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.reviews.user_ratings,DEV)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders.order_items,DEV)",
    ]

    # Mock the DB to return score=1.0 for each URN
    row1 = MagicMock()
    row1.dataset_urn = urns[0]
    row1.score = 1.0
    row2 = MagicMock()
    row2.dataset_urn = urns[1]
    row2.score = 1.0

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row1, row2]
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(datasets=urns, datahub=MagicMock(), db=db)

    assert value == 0.0, f"Expected 0 failing, got {value}"
    assert breakdown["dataset_count"] == 2


# ── One failing (score < 1.0) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_score_below_one_is_failing() -> None:
    """Dataset with latest score=0.7 must be categorised as failing.

    spec: feature/BACKEND.md §Metrics Service — dataset is failing if score < 1.0.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.reviews.user_ratings_legacy,DEV)"

    row = MagicMock()
    row.dataset_urn = urn
    row.score = 0.7

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(datasets=[urn], datahub=MagicMock(), db=db)

    assert value == 1.0, f"Expected failing_count=1, got {value}"
    per_ds = breakdown["datasets"]
    assert per_ds[0]["category"] == "failing"
    assert per_ds[0]["urn"] == urn


# ── Dataset with no result is failing ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_no_result_is_failing() -> None:
    """Dataset with no validation result must be counted as failing.

    spec: feature/BACKEND.md §Metrics Service — datasets with no result → failing.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []  # no rows — no result for this dataset
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(datasets=[urn], datahub=MagicMock(), db=db)

    assert value == 1.0, "Dataset with no result must be counted as failing"
    ds = breakdown["datasets"][0]
    assert ds["category"] == "failing"


# ── Breakdown shape ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_contains_required_keys() -> None:
    """Returned breakdown must contain dataset_count and datasets with urn/category/detail.

    spec: feature/BACKEND.md §Metrics Service lines 610-622 — breakdown shape:
          {"dataset_count": <int>, "datasets": [{"urn": ..., "category": ..., "detail": {...}}]}.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.test,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    _, breakdown = await measure(datasets=[urn], datahub=MagicMock(), db=db)

    for key in ("dataset_count", "datasets"):
        assert key in breakdown, (
            f"breakdown must contain '{key}'; spec: feature/BACKEND.md §Metrics Service."
        )

    assert isinstance(breakdown["datasets"], list)
    ds = breakdown["datasets"][0]
    for field in ("urn", "category"):
        assert field in ds, f"per-dataset entry must have '{field}'"
