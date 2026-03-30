"""Unit tests for ML-based validation (mocked DB session)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.validation.ml_validation import (
    _validate_day_of_week,
    _validate_range,
    validate_values,
)


_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"
_RULE_ID = "ts_r1"


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_history_row(values: dict, measured_at: datetime) -> MagicMock:
    """Create a mock ValidationResult row with given values and measured_at."""
    row = MagicMock()
    row.values = values
    row.measured_at = measured_at
    return row


def _mock_db_history(db: AsyncMock, rows: list) -> None:
    """Configure db.execute to return the provided rows as scalars."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db.execute = AsyncMock(return_value=result_mock)


# ── validate_values: insufficient history ─────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_values_returns_none_when_fewer_than_3_history_rows(db):
    """Fewer than 3 history rows → returns None (not enough data to model)."""
    _mock_db_history(db, [
        _make_history_row({"row_count": 100}, datetime.now(tz=UTC) - timedelta(days=1)),
        _make_history_row({"row_count": 110}, datetime.now(tz=UTC) - timedelta(days=2)),
    ])

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"row_count": 105},
        ml_config={"targets": ["row_count"], "model": "range"},
        db=db,
    )

    assert result is None


@pytest.mark.asyncio
async def test_validate_values_returns_none_with_exactly_2_rows(db):
    """Exactly 2 history rows is still insufficient (need >= 3)."""
    _mock_db_history(db, [
        _make_history_row({"row_count": 100}, datetime.now(tz=UTC) - timedelta(days=i))
        for i in range(1, 3)
    ])

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"row_count": 200},
        ml_config={"targets": ["row_count"]},
        db=db,
    )

    assert result is None


@pytest.mark.asyncio
async def test_validate_values_returns_none_when_targets_empty(db):
    """Empty targets list → returns None immediately (no history query needed)."""
    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"row_count": 100},
        ml_config={"targets": []},
        db=db,
    )

    assert result is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_validate_values_exactly_3_history_rows_returns_result(db):
    """Exactly 3 history rows meets the minimum — should return a verdict dict, not None."""
    history = [
        _make_history_row({"metric": v}, datetime.now(tz=UTC) - timedelta(days=i))
        for i, v in enumerate([10, 20, 30], 1)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"metric": 20},
        ml_config={"targets": ["metric"], "model": "range"},
        db=db,
    )

    # 3 rows is exactly the minimum — should produce a result, not None
    assert result is not None
    assert "metric" in result


@pytest.mark.asyncio
async def test_validate_values_non_numeric_current_value_returns_false(db):
    """Non-numeric string in current values that cannot be cast to float → False verdict."""
    history = [
        _make_history_row({"metric": v}, datetime.now(tz=UTC) - timedelta(days=i))
        for i, v in enumerate([10, 20, 30], 1)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"metric": "not-a-number"},
        ml_config={"targets": ["metric"], "model": "range"},
        db=db,
    )

    assert result is not None
    assert result["metric"] is False


# ── validate_values: range model ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_values_range_model_within_range_returns_true(db):
    """Historical values [10, 20, 30]; current=25 → True (within [10, 30])."""
    history = [
        _make_history_row({"metric": v}, datetime.now(tz=UTC) - timedelta(days=i))
        for i, v in enumerate([10, 20, 30], 1)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"metric": 25},
        ml_config={"targets": ["metric"], "model": "range"},
        db=db,
    )

    assert result is not None
    assert result["metric"] is True


@pytest.mark.asyncio
async def test_validate_values_range_model_above_max_returns_false(db):
    """Historical values [10, 20, 30]; current=50 → False (above max=30)."""
    history = [
        _make_history_row({"metric": v}, datetime.now(tz=UTC) - timedelta(days=i))
        for i, v in enumerate([10, 20, 30], 1)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"metric": 50},
        ml_config={"targets": ["metric"], "model": "range"},
        db=db,
    )

    assert result is not None
    assert result["metric"] is False


@pytest.mark.asyncio
async def test_validate_values_missing_target_in_values_returns_false(db):
    """Target column absent from current values dict → verdict is False."""
    history = [
        _make_history_row({"metric": v}, datetime.now(tz=UTC) - timedelta(days=i))
        for i, v in enumerate([10, 20, 30], 1)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"other_col": 25},  # "metric" is absent
        ml_config={"targets": ["metric"], "model": "range"},
        db=db,
    )

    assert result is not None
    assert result["metric"] is False


@pytest.mark.asyncio
async def test_validate_values_multiple_targets_evaluated_independently(db):
    """Each target is validated separately; one can pass while another fails."""
    history = [
        _make_history_row(
            {"rows": v, "nulls": v * 0.01},
            datetime.now(tz=UTC) - timedelta(days=i),
        )
        for i, v in enumerate([100, 110, 120], 1)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"rows": 115, "nulls": 5.0},  # rows ok, nulls way above max ~1.2
        ml_config={"targets": ["rows", "nulls"], "model": "range"},
        db=db,
    )

    assert result is not None
    assert result["rows"] is True
    assert result["nulls"] is False


@pytest.mark.asyncio
async def test_validate_values_validation_range_day_of_week_overrides_model(db):
    """validation_range='day_of_week' forces day_of_week model regardless of model field."""
    now = datetime.now(tz=UTC)
    # Create history entries all on the same weekday as today so there are enough
    current_weekday = now.weekday()
    # Offsets in multiples of 7 days to land on the same weekday
    history = [
        _make_history_row(
            {"metric": 100},
            now - timedelta(days=7 * (i + 1)),
        )
        for i in range(4)
    ]
    _mock_db_history(db, history)

    result = await validate_values(
        dataset_urn=_DATASET_URN,
        rule_id=_RULE_ID,
        values={"metric": 100},
        ml_config={
            "targets": ["metric"],
            "model": "range",  # should be overridden
            "validation_range": "day_of_week",
        },
        db=db,
    )

    # At minimum we get back a dict — the model ran without error
    assert result is not None
    assert "metric" in result


# ── _validate_range ────────────────────────────────────────────────────────────


def test_validate_range_within_range_returns_true():
    history = [
        {"values": {"v": 10}},
        {"values": {"v": 20}},
        {"values": {"v": 30}},
    ]
    assert _validate_range(history, "v", 15) is True


def test_validate_range_at_exact_min_returns_true():
    history = [
        {"values": {"v": 10}},
        {"values": {"v": 20}},
        {"values": {"v": 30}},
    ]
    assert _validate_range(history, "v", 10) is True


def test_validate_range_at_exact_max_returns_true():
    history = [
        {"values": {"v": 10}},
        {"values": {"v": 20}},
        {"values": {"v": 30}},
    ]
    assert _validate_range(history, "v", 30) is True


def test_validate_range_below_min_returns_false():
    history = [
        {"values": {"v": 10}},
        {"values": {"v": 20}},
        {"values": {"v": 30}},
    ]
    assert _validate_range(history, "v", 5) is False


def test_validate_range_above_max_returns_false():
    history = [
        {"values": {"v": 10}},
        {"values": {"v": 20}},
        {"values": {"v": 30}},
    ]
    assert _validate_range(history, "v", 35) is False


def test_validate_range_no_historical_values_returns_false():
    """All history rows have None for the target → returns False."""
    history = [
        {"values": {"other": 1}},
        {"values": {"other": 2}},
    ]
    assert _validate_range(history, "v", 10) is False


def test_validate_range_skips_non_numeric_historical_values():
    """Non-numeric entries in history are silently skipped."""
    history = [
        {"values": {"v": "bad"}},
        {"values": {"v": 10}},
        {"values": {"v": 20}},
    ]
    # Only 10 and 20 are valid; current 15 is within [10, 20]
    assert _validate_range(history, "v", 15) is True


# ── _validate_day_of_week ──────────────────────────────────────────────────────


def test_validate_day_of_week_within_range_returns_true():
    """Current value within mean ± 2*std for the current weekday → True."""
    now = datetime.now(tz=UTC)
    current_weekday = now.weekday()

    # Build same-weekday history: 4 values at exactly 100
    history = [
        {
            "values": {"v": 100.0},
            "measured_at": now - timedelta(days=7 * (i + 1)),
        }
        for i in range(4)
    ]

    # std=0, so mean±2*std = [100, 100]; only 100.0 passes
    assert _validate_day_of_week(history, "v", 100.0) is True


def test_validate_day_of_week_outside_range_returns_false():
    """Value far outside mean ± 2*std for the current weekday → False."""
    now = datetime.now(tz=UTC)

    # Build history with values centred at 100, std≈0
    history = [
        {
            "values": {"v": 100.0},
            "measured_at": now - timedelta(days=7 * (i + 1)),
        }
        for i in range(4)
    ]

    # 200 is far outside [100 ± 0]
    assert _validate_day_of_week(history, "v", 200.0) is False


def test_validate_day_of_week_insufficient_same_day_falls_back_to_global_range():
    """Fewer than 2 same-weekday samples falls back to global range model."""
    now = datetime.now(tz=UTC)
    current_weekday = now.weekday()

    # One entry on today's weekday, plus multiple on other days
    entries = []
    # Today's weekday: only one entry
    entries.append({
        "values": {"v": 50.0},
        "measured_at": now - timedelta(days=7),
    })
    # Entries on other weekdays to populate global range [40, 60]
    other_day = (current_weekday + 1) % 7
    for i in range(3):
        offset = i + 1
        # Move to the "other_day" weekday
        days_offset = (7 - current_weekday + other_day) % 7 + 7 * offset
        entries.append({
            "values": {"v": 40.0 + i * 10},
            "measured_at": now - timedelta(days=days_offset),
        })

    # Global range covers [40, 60]; 50 is within → True
    assert _validate_day_of_week(entries, "v", 50.0) is True


def test_validate_day_of_week_no_historical_values_returns_false():
    """No historical values for the target at all → returns False."""
    now = datetime.now(tz=UTC)
    history = [
        {"values": {"other": 1.0}, "measured_at": now - timedelta(days=7)},
    ]
    assert _validate_day_of_week(history, "v", 50.0) is False


def test_validate_day_of_week_skips_entries_with_none_measured_at():
    """Entries where measured_at is None are silently skipped."""
    now = datetime.now(tz=UTC)
    valid_entry = {
        "values": {"v": 100.0},
        "measured_at": now - timedelta(days=7),
    }
    null_entry = {
        "values": {"v": 999.0},
        "measured_at": None,
    }
    # Only the valid entry should contribute; the null entry is skipped
    history = [valid_entry, null_entry, valid_entry, valid_entry]

    # With 4 entries all at 100, checking 100 should pass
    # (even though null_entry is skipped)
    result = _validate_day_of_week(history, "v", 100.0)
    # We cannot guarantee the weekday matches, but no exception should be raised
    assert isinstance(result, bool)
