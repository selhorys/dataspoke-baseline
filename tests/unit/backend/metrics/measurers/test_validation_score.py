"""Unit tests for the validation-score measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types:
    - Registered under 'validation-score'.
    - Emits {'total': float, 'validation_score_sum': float}.
    - total = count of datasets matched by dataset_filter.
    - validation_score_sum = sum of each dataset's latest validation score
      within a per-dataset window; 0.0 when no in-window row.
    - Per-dataset window = 2 × mean(last N inter-arrival gaps), N from
      settings.validation_score_n_intervals (default 3). Fewer than N+1
      results → fallback metric_conf.time_window_sec.
  spec/feature/BACKEND.md §Metrics Service §Time windows:
    - detail carries time_window_sec (resolved int) and window_source
      ('intervals' or 'default').
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (score < 1.0 or no in-window row).
    - No 'category' field in per-dataset entries.
"""

from datetime import UTC, datetime, timedelta
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
    """Dataset whose only validation result is outside the fallback time window contributes 0.0.

    The measurer fetches the latest N+1 rows per dataset via SQL (row_number subquery),
    then applies the time window in Python.  When the only row's data_time is older than
    the resolved window the dataset contributes 0.0 and appears in breakdown.

    This test exercises the Python-side windowing gate with the fallback
    (default) window, since only 1 row is returned (< N+1 needed for intervals window).

    Spec: spec/USE_CASE_en.md §UC5 — validation_score_sum sums scores in
          [now - window_sec, now]; outside the window → 0.0.
    Spec: spec/feature/BACKEND.md §Metrics Service — datasets with no in-window row
          appear in breakdown.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.old,DEV)"
    time_window_sec = 86400  # 1-day fallback window

    # Single row older than the window — only 1 row so intervals window won't be used.
    old_row = MagicMock()
    old_row.dataset_urn = urn
    old_row.data_time = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec + 3600)
    old_row.score = 0.9
    old_row.rn = 1

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [old_row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )

    assert values["total"] == 1.0
    assert values["validation_score_sum"] == 0.0, (
        "A row whose data_time is older than the window must not be counted. "
        "Spec: spec/USE_CASE_en.md §UC5 — windowing must be enforced."
    )
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn


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


# ── Per-dataset window: intervals-derived (>= N+1 rows) ─────────────────────


def _make_validation_rows(
    urn: str,
    data_times: list,
    scores: list[float] | None = None,
) -> list[MagicMock]:
    """Build a list of mock ValidationResult rows ordered by data_time desc (rn=1 is newest).

    data_times should be a list of datetime objects, newest first (same order as the measurer
    receives after the row_number() subquery orders desc).
    """
    rows = []
    if scores is None:
        scores = [1.0] * len(data_times)
    for i, (dt, score) in enumerate(zip(data_times, scores), start=1):
        row = MagicMock()
        row.dataset_urn = urn
        row.data_time = dt
        row.score = score
        row.rn = i
        rows.append(row)
    return rows


def _make_validation_db(rows: list) -> AsyncMock:
    """Mock DB that returns the given rows for the validation-score subquery."""
    result = MagicMock()
    result.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_intervals_window_derived_from_n_plus_one_rows() -> None:
    """Dataset with >= N+1 rows uses window = mean(last N gaps) × 2.

    With N=3, rows at 0h, -24h, -48h, -72h (4 rows = N+1):
      gaps = [24h, 24h, 24h] → mean = 24h = 86400s → window = 172800s.
    Latest row is at now - 0s, which is within the 172800s window → score counted.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — validation-score
          per-dataset window = 2 × mean(last N inter-arrival gaps).
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — window_source='intervals'.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.intervals,DEV)"

    now = datetime.now(tz=UTC)
    # N=3 (default): need N+1=4 rows. Evenly spaced 24h apart.
    data_times = [
        now - timedelta(hours=0),   # rn=1 (latest)
        now - timedelta(hours=24),  # rn=2
        now - timedelta(hours=48),  # rn=3
        now - timedelta(hours=72),  # rn=4
    ]
    rows = _make_validation_rows(urn, data_times, scores=[1.0, 1.0, 1.0, 1.0])
    db = _make_validation_db(rows)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # fallback — must NOT be used (>= N+1 rows)
        datahub=MagicMock(),
        db=db,
    )

    # Latest row at 0h is within window=172800s → counted
    assert values["validation_score_sum"] == 1.0, (
        "Latest in-window row with score=1.0 must be counted. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert breakdown["datasets"] == [], (
        "Score=1.0 dataset must not appear in breakdown. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )


@pytest.mark.asyncio
async def test_intervals_window_source_is_intervals_in_detail() -> None:
    """Dataset with >= N+1 rows has window_source='intervals' in breakdown detail.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows —
          window_source='intervals' when window is derived from inter-arrival gaps.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.windetail,DEV)"

    now = datetime.now(tz=UTC)
    # 4 rows (N+1=4 for N=3), evenly spaced 48h → window = 2 × 48h = 96h = 345600s
    data_times = [
        now - timedelta(hours=200),  # rn=1 — latest (old, outside window)
        now - timedelta(hours=248),
        now - timedelta(hours=296),
        now - timedelta(hours=344),
    ]
    rows = _make_validation_rows(urn, data_times, scores=[0.8, 1.0, 1.0, 1.0])
    db = _make_validation_db(rows)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT be used
        datahub=MagicMock(),
        db=db,
    )

    # Latest row is 200h ago. window = 2 × mean([48h, 48h, 48h]) × 3600 = 345600s ≈ 96h.
    # 200h > 96h → latest row is outside window → 0.0 contribution → in breakdown.
    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert detail["window_source"] == "intervals", (
        "Dataset with >= N+1 rows must have window_source='intervals'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    # Spec literal: 4 rows evenly spaced 48h apart → gaps = [48h, 48h, 48h]
    # → mean = 48h = 172800s → window = 2 × 172800 = 345600s.
    # spec/USE_CASE_en.md §UC5 §Built-in active metric types — window = 2 × mean(last N gaps).
    assert detail["time_window_sec"] == 345600, (
        "Intervals-derived window must be 345600s (2 × mean of three 48h gaps). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )


@pytest.mark.asyncio
async def test_intervals_window_latest_in_window_score_counted() -> None:
    """Latest row inside the intervals-derived window contributes its score.

    N=3: 4 rows spaced 12h apart → window = 2 × 12h = 24h = 86400s.
    Latest row is 2h ago (< 24h window) with score=0.7 → counted.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          score counted is the latest result whose data_time is inside the window.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.inwindow,DEV)"

    now = datetime.now(tz=UTC)
    data_times = [
        now - timedelta(hours=2),   # rn=1, inside 24h window
        now - timedelta(hours=14),
        now - timedelta(hours=26),
        now - timedelta(hours=38),
    ]
    rows = _make_validation_rows(urn, data_times, scores=[0.7, 1.0, 1.0, 1.0])
    db = _make_validation_db(rows)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # fallback — must NOT be used
        datahub=MagicMock(),
        db=db,
    )

    # window = 2 × mean([12h, 12h, 12h]) = 24h. Latest at 2h < 24h → in-window, score=0.7.
    assert abs(values["validation_score_sum"] - 0.7) < 1e-6, (
        "In-window score=0.7 must be summed. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    # score=0.7 < 1.0 → appears in breakdown
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["detail"]["window_source"] == "intervals"


# ── Per-dataset window: sparse (< N+1 rows) → fallback ──────────────────────


@pytest.mark.asyncio
async def test_sparse_dataset_fewer_than_n_plus_one_rows_uses_fallback() -> None:
    """Dataset with fewer than N+1 rows falls back to metric_conf.time_window_sec.

    N=3: fewer than 4 rows → window = metric_conf.time_window_sec.
    With 2 rows, fallback 86400s, latest row 50000s ago → within window → counted.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — fewer than N
          intervals falls back to metric_conf.time_window_sec.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows —
          window_source='default' for fallback.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.sparse,DEV)"

    now = datetime.now(tz=UTC)
    # Only 2 rows (< N+1=4) — insufficient for intervals computation
    data_times = [
        now - timedelta(seconds=50000),   # rn=1, latest
        now - timedelta(seconds=100000),  # rn=2
    ]
    rows = _make_validation_rows(urn, data_times, scores=[1.0, 1.0])
    db = _make_validation_db(rows)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback applied
        datahub=MagicMock(),
        db=db,
    )

    # 50000s < 86400s → in-window using fallback → counted
    assert values["validation_score_sum"] == 1.0, (
        "Sparse dataset (< N+1 rows) fallback 86400s, latest 50000s ago → in-window. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_sparse_dataset_window_source_is_default() -> None:
    """Dataset with fewer than N+1 rows has window_source='default' in breakdown detail.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows —
          window_source='default' when fallback metric_conf.time_window_sec is used.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.sparsedetail,DEV)"

    now = datetime.now(tz=UTC)
    # Only 1 row (< N+1) → fallback 3600s. Latest at 5000s → stale.
    data_times = [now - timedelta(seconds=5000)]
    rows = _make_validation_rows(urn, data_times, scores=[0.9])
    db = _make_validation_db(rows)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # fallback
        datahub=MagicMock(),
        db=db,
    )

    # 5000s > 3600s → stale
    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert detail["window_source"] == "default", (
        "Sparse dataset must have window_source='default'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert detail["time_window_sec"] == 3600


@pytest.mark.asyncio
async def test_no_validation_result_uses_fallback_and_appears_in_breakdown() -> None:
    """Dataset with no validation results uses fallback window and contributes 0.0.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — fewer than N
          intervals falls back; contribution is 0.0 when no result inside the window.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — window_source='default'.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.norows,DEV)"

    db = _make_validation_db([])  # no rows at all

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values["validation_score_sum"] == 0.0
    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert detail["window_source"] == "default", (
        "Dataset with no rows must have window_source='default'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


# ── Breakdown detail shape: time_window_sec and window_source present ────────


@pytest.mark.asyncio
async def test_breakdown_detail_includes_time_window_sec_and_window_source() -> None:
    """Breakdown detail must include time_window_sec and window_source for failed datasets.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — breakdown stale-entry
          detail includes time_window_sec (resolved int) and window_source.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.detailshape,DEV)"

    db = _make_validation_db([])  # no rows → fallback → 0.0

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert "time_window_sec" in detail, (
        "Breakdown detail must include 'time_window_sec'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert "window_source" in detail, (
        "Breakdown detail must include 'window_source'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert isinstance(detail["time_window_sec"], int)
    assert isinstance(detail["window_source"], str)
