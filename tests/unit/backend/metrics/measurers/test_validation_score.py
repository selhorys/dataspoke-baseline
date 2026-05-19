"""Unit tests for the validation-score measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types:
    - Registered under 'validation-score'.
    - Emits {'total': float, 'validation_score_sum': float}.
    - total = count of datasets matched by dataset_filter.
    - validation_score_sum = sum of each dataset's latest validation score in
      [now - time_window_sec, now]; 0.0 when no in-window row.
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (score < 1.0 or no in-window row).
    - No 'category' field in per-dataset entries.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.measurers import validation_score  # noqa: F401 — triggers registration


def _get_measurer():
    from src.backend.metrics.measurers.registry import get_measurer
    fn = get_measurer("validation-score")
    assert fn is not None, "validation-score measurer must be registered"
    return fn


# ── Registration ──────────────────────────────────────────────────────────────


def test_registered_under_correct_key() -> None:
    """Measurer is registered under 'validation-score'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metric_type
          value is 'validation-score'.
    """
    fn = _get_measurer()
    assert fn is not None


# ── Empty datasets list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_datasets_returns_zeros() -> None:
    """measure([]) returns total=0.0, validation_score_sum=0.0.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types.
    """
    measure = _get_measurer()

    values, breakdown = await measure(
        datasets=[],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=AsyncMock(),
    )

    assert values == {"total": 0.0, "validation_score_sum": 0.0}
    assert breakdown["dataset_count"] == 0
    assert breakdown["datasets"] == []


# ── All passing (score=1.0) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_datasets_score_one_not_in_breakdown() -> None:
    """Datasets with in-window score=1.0 are NOT in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          validation-score: latest score in window < 1.0 → failed.
    """
    measure = _get_measurer()
    urn1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,DEV)"
    urn2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,DEV)"

    now = datetime.now(tz=UTC)

    row1 = MagicMock()
    row1.dataset_urn = urn1
    row1.data_time = now - timedelta(hours=1)
    row1.score = 1.0

    row2 = MagicMock()
    row2.dataset_urn = urn2
    row2.data_time = now - timedelta(hours=2)
    row2.score = 1.0

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row1, row2]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn1, urn2],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values["total"] == 2.0
    assert values["validation_score_sum"] == 2.0
    assert breakdown["datasets"] == [], (
        "Datasets with score=1.0 must NOT appear in breakdown. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )


# ── Three datasets: two passing, one partial ──────────────────────────────────


@pytest.mark.asyncio
async def test_three_datasets_two_full_one_partial() -> None:
    """Three datasets with scores [1.0, 1.0, 0.7]: total=3, sum=2.7, breakdown=1 entry.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — validation_score_sum
          is the sum across all datasets in the window.
    """
    measure = _get_measurer()
    urn1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.full1,DEV)"
    urn2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.full2,DEV)"
    urn3 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.partial,DEV)"

    now = datetime.now(tz=UTC)

    row1 = MagicMock()
    row1.dataset_urn = urn1
    row1.data_time = now - timedelta(hours=1)
    row1.score = 1.0

    row2 = MagicMock()
    row2.dataset_urn = urn2
    row2.data_time = now - timedelta(hours=2)
    row2.score = 1.0

    row3 = MagicMock()
    row3.dataset_urn = urn3
    row3.data_time = now - timedelta(hours=3)
    row3.score = 0.7

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row1, row2, row3]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn1, urn2, urn3],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values["total"] == 3.0
    assert abs(values["validation_score_sum"] - 2.7) < 1e-6
    assert breakdown["dataset_count"] == 3
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn3


# ── Dataset with out-of-window result ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_out_of_window_result_contributes_zero() -> None:
    """Dataset whose only validation result is outside the time window contributes 0.0.

    The measurer's SQL WHERE clause filters data_time >= window_start. This test
    verifies that the SQL statement issued to db.execute contains a data_time bound
    parameter whose value is approximately now - time_window_sec, proving the filter
    is present and uses the correct cutoff.

    Spec: spec/USE_CASE_en.md §UC5 — validation_score_sum sums scores in
          [now - time_window_sec, now]; outside the window → 0.0.
    Spec: spec/feature/BACKEND.md §Metrics Service — datasets with no in-window row
          appear in breakdown.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.old,DEV)"
    time_window_sec = 86400

    captured_statements: list[Any] = []

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []  # no in-window result (filtered out by SQL)

    async def _capture_execute(stmt, *args, **kwargs):
        captured_statements.append(stmt)
        return execute_result

    db.execute = _capture_execute

    before_call = datetime.now(tz=UTC)
    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )
    after_call = datetime.now(tz=UTC)

    assert values["total"] == 1.0
    assert values["validation_score_sum"] == 0.0
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn

    # Verify the SQL statement carries a window_start bound parameter close to
    # [now - time_window_sec], proving the measurer filters by data_time.
    assert captured_statements, "db.execute must be called at least once."
    stmt = captured_statements[0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": False})
    params = compiled.params

    # The measurer binds window_start as a datetime parameter. Find the parameter
    # whose value is a datetime close to (now - time_window_sec).
    expected_cutoff_low = before_call - timedelta(seconds=time_window_sec)
    expected_cutoff_high = after_call - timedelta(seconds=time_window_sec)

    window_param_found = any(
        isinstance(v, datetime)
        and expected_cutoff_low - timedelta(seconds=5) <= v <= expected_cutoff_high + timedelta(seconds=5)
        for v in params.values()
    )
    assert window_param_found, (
        f"Expected a data_time bound parameter near {expected_cutoff_low.isoformat()} "
        f"to {expected_cutoff_high.isoformat()} in the SQL statement params, "
        f"but params were: {params!r}. "
        "Spec: spec/USE_CASE_en.md §UC5 — windowing must be enforced in the SQL query."
    )


# ── Dataset with no validation result ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_no_result_contributes_zero_and_is_in_breakdown() -> None:
    """Dataset with no validation result at all contributes 0.0 and appears in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets with no in-window row contribute 0.0 AND appear in breakdown.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.novalidation,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values["validation_score_sum"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn


# ── No 'category' in breakdown entries ───────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_entries_have_no_category_field() -> None:
    """Breakdown entries must not carry a 'category' field.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] entries are {urn, detail?} only.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.nocat,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert len(breakdown["datasets"]) >= 1
    for entry in breakdown["datasets"]:
        assert "category" not in entry, (
            "Breakdown entries must not have 'category'. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert "urn" in entry
