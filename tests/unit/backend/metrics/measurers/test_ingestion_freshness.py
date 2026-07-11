"""Unit tests for the ingestion-freshness measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types:
    - Registered under 'ingestion-freshness'.
    - Emits {'total': float, 'ingested_in_time': float}.
    - total = count of datasets matched by dataset_filter.
    - ingested_in_time = count whose latest INGESTION.COMPLETE was less than
      the per-dataset freshness window ago.
    - Per-dataset window: active-custom → SCHEDULE_TIER_SECONDS[tier]*2
      (hourly→7200, daily→172800, weekly→1209600); passive → 7200;
      no config or active-custom with null tier → metric_conf.time_window_sec.
  spec/feature/BACKEND.md §Metrics Service §Time windows:
    - Window derived from ingestion_configs: mode+schedule_tier.
    - detail carries time_window_sec (resolved int) and window_source.
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (stale datasets).
    - No 'category' field in per-dataset entries — only {urn, detail}.
    - detail for ingestion-freshness: {last_event_at, time_window_sec, window_source}.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.measurers import ingestion_freshness  # noqa: F401 — triggers registration
from tests.unit.conftest import route_db_execute


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

    Note: The spec mandates "less than time_window_sec ago" but does not explicitly
    address the exact-equality instant. The strict-`<` tie-break applied here is a
    chosen implementation detail, not a spec requirement. The companion test
    `test_event_one_second_inside_window_is_fresh` covers the spec-mandated boundary
    on the safe (inside-window) side.
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


# ── Per-dataset window: active-custom with schedule_tier ─────────────────────


def _make_two_query_db(config_rows: list, event_rows: list) -> AsyncMock:
    """Build a mock DB that returns config_rows on the first execute call
    (ingestion_source_dataset JOIN ingestion_source query) and event_rows on
    the second (events query).

    The ingestion-freshness measurer calls db.execute twice per invocation:
    1. SELECT from ingestion_source_dataset JOIN ingestion_source (returns mapping rows)
    2. SELECT from events with row_number() (returns event rows)

    Each config_row mock must have fields: dataset_urn, origin, last_seen_at, mode, schedule_tier.
    Mode values use the new per-source enum: ACTIVE_CUSTOM_MANAGED, DATAHUB_MANAGED, PASSIVE.
    """
    config_result = MagicMock()
    config_result.all.return_value = config_rows
    event_result = MagicMock()
    event_result.all.return_value = event_rows

    db = AsyncMock()
    # Route by SQL: the mapping query hits ingestion_source_dataset; the freshness
    # query is the row_number() select over events (the default).
    route_db_execute(
        db, [("ingestion_source_dataset", config_result)], default=event_result
    )
    return db


@pytest.mark.asyncio
async def test_active_custom_daily_window_is_twice_daily_period() -> None:
    """active-custom with schedule_tier='daily' uses 172800s window (2 × 86400).

    A INGESTION.COMPLETE event 130000s ago (< 172800) is ingested_in_time=1.
    An event at 200000s ago (> 172800) is stale.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          active-custom: window = SCHEDULE_TIER_SECONDS[tier] × 2.
          daily → 86400 × 2 = 172800s.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.daily,DEV)"

    # Config row: active-custom daily
    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "ACTIVE_CUSTOM_MANAGED"
    config_row.schedule_tier = "daily"

    # Event within the window: 130000s ago < 172800s (spec literal: daily=86400×2)
    recent_event = MagicMock()
    recent_event.entity_id = urn
    recent_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=130000)

    db = _make_two_query_db([config_row], [recent_event])

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — should NOT be used
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 1.0, (
        "active-custom daily: event 130000s ago must be in-time (window=172800s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert breakdown["datasets"] == [], (
        "In-time dataset must not appear in breakdown. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )


@pytest.mark.asyncio
async def test_active_custom_daily_stale_outside_window() -> None:
    """active-custom daily: event 200000s ago (> 172800) is stale; breakdown has window_source.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          active-custom daily window = 172800s; event outside → stale.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — breakdown detail
          carries time_window_sec and window_source='managed:daily'.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.daily-stale,DEV)"

    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "ACTIVE_CUSTOM_MANAGED"
    config_row.schedule_tier = "daily"

    stale_event = MagicMock()
    stale_event.entity_id = urn
    stale_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=200000)

    db = _make_two_query_db([config_row], [stale_event])

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT override active-custom
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn

    detail = entry["detail"]
    # Spec literal: active-custom daily → 86400 × 2 = 172800s
    # spec/USE_CASE_en.md §UC5 §Built-in active metric types — active-custom:
    # window = SCHEDULE_TIER_SECONDS[tier] × 2.
    assert detail["time_window_sec"] == 172800, (
        "detail.time_window_sec must be 172800 (active-custom:daily = 86400 × 2). "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert detail["window_source"] == "managed:daily", (
        "detail.window_source must be 'managed:daily' for ACTIVE_CUSTOM_MANAGED daily tier. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


@pytest.mark.asyncio
async def test_active_custom_hourly_window_is_7200s() -> None:
    """active-custom with schedule_tier='hourly' uses 7200s window (2 × 3600).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          active-custom hourly → 3600 × 2 = 7200s window.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.hourly,DEV)"

    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "ACTIVE_CUSTOM_MANAGED"
    config_row.schedule_tier = "hourly"

    stale_event = MagicMock()
    stale_event.entity_id = urn
    # 8000s ago — past the 7200s hourly window
    stale_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=8000)

    db = _make_two_query_db([config_row], [stale_event])

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT be used
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 0.0, (
        "active-custom hourly: event 8000s ago must be stale (window=7200s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    # Spec literal: active-custom hourly → 3600 × 2 = 7200s
    # spec/USE_CASE_en.md §UC5 §Built-in active metric types — hourly → 7200s.
    assert breakdown["datasets"][0]["detail"]["time_window_sec"] == 7200
    assert breakdown["datasets"][0]["detail"]["window_source"] == "managed:hourly"


@pytest.mark.asyncio
async def test_active_custom_weekly_window_is_1209600s() -> None:
    """active-custom with schedule_tier='weekly' uses 1209600s window (2 × 604800).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          active-custom weekly → 604800 × 2 = 1209600s window.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.weekly,DEV)"

    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "ACTIVE_CUSTOM_MANAGED"
    config_row.schedule_tier = "weekly"

    fresh_event = MagicMock()
    fresh_event.entity_id = urn
    # 600000s ago — within the 1209600s weekly window
    fresh_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=600000)

    db = _make_two_query_db([config_row], [fresh_event])

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # fallback — must NOT be used
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 1.0, (
        "active-custom weekly: event 600000s ago must be in-time (window=1209600s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )


# ── Per-dataset window: passive ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passive_window_is_7200s() -> None:
    """Passive ingestion config uses 7200s window (2 × hourly sync cadence).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          passive → twice the DataHub-sync cadence (hourly → 7200s).
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — passive: 7200s.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passive,DEV)"

    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "PASSIVE"
    config_row.schedule_tier = None

    stale_event = MagicMock()
    stale_event.entity_id = urn
    # 8000s ago — past the 7200s passive window
    stale_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=8000)

    db = _make_two_query_db([config_row], [stale_event])

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT be used for passive
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 0.0
    detail = breakdown["datasets"][0]["detail"]
    # Spec literal: passive → twice the hourly sync cadence = 3600 × 2 = 7200s
    # spec/USE_CASE_en.md §UC5 §Built-in active metric types — passive → 7200s.
    assert detail["time_window_sec"] == 7200, (
        "Passive dataset must use window 7200s (2 × hourly sync cadence). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert detail["window_source"] == "passive", (
        "Passive dataset detail.window_source must be 'passive'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


@pytest.mark.asyncio
async def test_passive_in_window_fresh() -> None:
    """Passive dataset with event 3600s ago (< 7200s) is ingested_in_time=1.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — passive window = 7200s.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passive-fresh,DEV)"

    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "PASSIVE"
    config_row.schedule_tier = None

    fresh_event = MagicMock()
    fresh_event.entity_id = urn
    fresh_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=3600)

    db = _make_two_query_db([config_row], [fresh_event])

    values, _ = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 600},  # fallback — must NOT override passive
        datahub=MagicMock(),
        db=db,
    )

    assert values["ingested_in_time"] == 1.0, (
        "Passive dataset with event 3600s ago must be in-time (passive window=7200s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )


# ── Per-dataset window: no config → fallback ─────────────────────────────────


@pytest.mark.asyncio
async def test_no_config_row_falls_back_to_metric_conf_time_window() -> None:
    """Dataset with no ingestion_configs row falls back to metric_conf.time_window_sec.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          no config (or active-custom with null schedule_tier) → metric_conf.time_window_sec.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — fallback window.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.noconfig,DEV)"

    # No config rows returned for this dataset
    stale_event = MagicMock()
    stale_event.entity_id = urn
    # 5000s ago — stale relative to fallback 3600s window, fresh relative to 86400s window
    stale_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=5000)

    db = _make_two_query_db([], [stale_event])  # empty config result

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # fallback: 3600s
        datahub=MagicMock(),
        db=db,
    )

    # 5000s > 3600s → stale with fallback window
    assert values["ingested_in_time"] == 0.0, (
        "Dataset with no config, event at 5000s ago, fallback 3600s → stale. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    detail = breakdown["datasets"][0]["detail"]
    assert detail["time_window_sec"] == 3600
    assert detail["window_source"] == "default", (
        "No config row must produce window_source='default'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


@pytest.mark.asyncio
async def test_active_custom_null_schedule_tier_falls_back_to_metric_conf() -> None:
    """active-custom with null schedule_tier falls back to metric_conf.time_window_sec.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          active-custom with no schedule_tier → metric_conf.time_window_sec.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — fallback window.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.activenull,DEV)"

    config_row = MagicMock()
    config_row.dataset_urn = urn
    config_row.mode = "ACTIVE_CUSTOM_MANAGED"
    config_row.schedule_tier = None  # null tier → fallback

    fresh_event = MagicMock()
    fresh_event.entity_id = urn
    fresh_event.occurred_at = datetime.now(tz=UTC) - timedelta(seconds=50000)

    db = _make_two_query_db([config_row], [fresh_event])

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback applied
        datahub=MagicMock(),
        db=db,
    )

    # 50000s < 86400s → in-time using fallback
    assert values["ingested_in_time"] == 1.0, (
        "active-custom null tier with fallback 86400s, event 50000s ago → in-time. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert breakdown["datasets"] == []


# ── Breakdown detail shape: time_window_sec and window_source present ────────


@pytest.mark.asyncio
async def test_stale_breakdown_detail_includes_time_window_sec_and_window_source() -> None:
    """Stale dataset breakdown detail includes time_window_sec and window_source.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — breakdown stale-entry
          detail includes time_window_sec (resolved int) and window_source.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.detail-check,DEV)"

    # No config row → fallback
    db = _make_two_query_db([], [])  # no events → stale

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=MagicMock(),
        db=db,
    )

    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert "time_window_sec" in detail, (
        "Stale breakdown detail must include 'time_window_sec'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert "window_source" in detail, (
        "Stale breakdown detail must include 'window_source'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert isinstance(detail["time_window_sec"], int)
    assert isinstance(detail["window_source"], str)
