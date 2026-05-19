"""Unit tests for the ingestion-freshness measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types:
    - Registered under 'ingestion-freshness'.
    - Emits {'total': float, 'ingested_in_time': float}.
    - total = count of datasets matched by dataset_filter.
    - ingested_in_time = count whose latest INGESTION.COMPLETE was less than
      metric_conf.time_window_sec ago.
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (stale datasets).
    - No 'category' field in per-dataset entries — only {urn, detail}.
    - detail for ingestion-freshness: {last_event_at: iso_string | None}.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.measurers import ingestion_freshness  # noqa: F401 — triggers registration


def _get_measurer():
    from src.backend.metrics.measurers.registry import get_measurer
    fn = get_measurer("ingestion-freshness")
    assert fn is not None, "ingestion-freshness measurer must be registered"
    return fn


# ── Registration ──────────────────────────────────────────────────────────────


def test_registered_under_correct_key() -> None:
    """Measurer is registered under 'ingestion-freshness'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metric_type value
          is 'ingestion-freshness'.
    """
    fn = _get_measurer()
    assert fn is not None


# ── Empty datasets list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_datasets_returns_zeros() -> None:
    """measure([]) returns total=0.0, ingested_in_time=0.0 with empty datasets list.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — total = count of
          datasets matched by dataset_filter.
    """
    measure = _get_measurer()
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values == {"total": 0.0, "ingested_in_time": 0.0}
    assert breakdown["dataset_count"] == 0
    assert breakdown["datasets"] == []


# ── Fresh dataset ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_dataset_not_in_breakdown() -> None:
    """Dataset with INGESTION.COMPLETE inside the time window is NOT in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] carries only failed entries (stale datasets).
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"
    recent = datetime.now(tz=UTC) - timedelta(hours=1)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = recent

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 1.0
    assert breakdown["datasets"] == [], (
        "Fresh dataset must NOT appear in breakdown. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert breakdown["dataset_count"] == 1


# ── Stale dataset — no event ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_no_event_in_breakdown_with_none_last_event() -> None:
    """Dataset with no INGESTION.COMPLETE event is in breakdown with last_event_at=None.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          detail for ingestion-freshness: {last_event_at: iso_string | None}.
    Spec: spec/feature/BACKEND.md §Metrics Service — absent event → stale.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.editions,DEV)"

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

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn
    assert entry["detail"]["last_event_at"] is None


# ── Stale dataset — old event ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_stale_event_in_breakdown() -> None:
    """Dataset with INGESTION.COMPLETE older than the cutoff appears in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service — ingestion-freshness: latest
          INGESTION.COMPLETE older than metric_conf.time_window_sec → stale.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders.fulfillment,DEV)"
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90000)  # older than 86400s

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = stale_ts

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn
    assert entry["detail"]["last_event_at"] is not None


# ── Boundary: time_window_sec is honored strictly ─────────────────────────────


@pytest.mark.asyncio
async def test_event_well_inside_window_is_ingested_in_time() -> None:
    """Event well inside the time window (half the window ago) is counted as ingested_in_time.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — ingested_in_time =
          count whose latest INGESTION.COMPLETE was less than time_window_sec ago.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.test,DEV)"
    time_window_sec = 3600
    # Event is well inside the window (half window ago)
    inside_window = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec // 2)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = inside_window

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 1.0
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_event_well_outside_window_is_stale() -> None:
    """Event well outside the window (2x window ago) is stale.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — 'less than
          time_window_sec ago'; events older than the window are stale.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary2.test,DEV)"
    time_window_sec = 3600
    # Event 2x the window ago — clearly outside
    outside_window = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec * 2)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = outside_window

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1


# ── No 'category' in breakdown entries ───────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_entries_have_no_category_field() -> None:
    """Breakdown entries must not carry a 'category' field.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] entries are {urn, detail?} only. No 'category' field.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.nocategory.test,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []  # no event → stale
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert "category" not in entry, (
        "Breakdown entry must not carry 'category'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert "urn" in entry
    assert "detail" in entry


# ── Mixed dataset set ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_fresh_and_stale_counts_correctly() -> None:
    """Three datasets: two fresh, one stale → total=3, ingested_in_time=2, breakdown=1 entry.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — total and
          ingested_in_time counts.
    """
    measure = _get_measurer()
    urn_fresh1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,DEV)"
    urn_fresh2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,DEV)"
    urn_stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,DEV)"

    now = datetime.now(tz=UTC)
    time_window_sec = 86400

    row1 = MagicMock()
    row1.entity_id = urn_fresh1
    row1.occurred_at = now - timedelta(hours=1)

    row2 = MagicMock()
    row2.entity_id = urn_fresh2
    row2.occurred_at = now - timedelta(hours=2)

    # urn_stale has an old event beyond the window
    row3 = MagicMock()
    row3.entity_id = urn_stale
    row3.occurred_at = now - timedelta(seconds=time_window_sec + 3600)

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row1, row2, row3]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn_fresh1, urn_fresh2, urn_stale],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )

    assert values["total"] == 3.0
    assert values["ingested_in_time"] == 2.0
    assert breakdown["dataset_count"] == 3
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn_stale


# ── Deterministic clock boundary (strict less-than) ──────────────────────────


@pytest.mark.asyncio
async def test_event_exactly_at_cutoff_is_stale(monkeypatch) -> None:
    """Event at exactly now - time_window_sec is STALE.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — ingested_in_time =
    count whose latest INGESTION.COMPLETE was *less than* time_window_sec ago.
    An event exactly time_window_sec old is NOT less than time_window_sec old,
    so it must be counted as stale (strict-`<` boundary).
    """
    import src.backend.metrics.measurers.ingestion_freshness as _mod

    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    class _FixedDatetime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(_mod, "datetime", _FixedDatetime)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.exact,DEV)"

    exact_cutoff = fixed_now - timedelta(seconds=time_window_sec)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = exact_cutoff

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn


@pytest.mark.asyncio
async def test_event_one_second_inside_window_is_fresh(monkeypatch) -> None:
    """Event at now - time_window_sec + 1s is FRESH.

    One second inside the window must be counted as ingested_in_time=1.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — ingested_in_time =
          count whose latest INGESTION.COMPLETE was *less than* time_window_sec ago.
    """
    import src.backend.metrics.measurers.ingestion_freshness as _mod

    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    class _FixedDatetime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(_mod, "datetime", _FixedDatetime)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.inside,DEV)"

    # One second inside the window
    one_sec_inside = fixed_now - timedelta(seconds=time_window_sec - 1)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = one_sec_inside

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 1.0, (
        "Event one second inside the window must be FRESH. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert breakdown["datasets"] == []
