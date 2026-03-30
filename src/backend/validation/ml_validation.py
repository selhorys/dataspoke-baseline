"""ML-based validation for sql_timeseries rules.

Validates extracted timeseries values against historical ValidationResult
records using simple statistical models (range, day-of-week).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_MIN_HISTORY_ROWS = 3


async def validate_values(
    dataset_urn: str,
    rule_id: str,
    values: dict[str, Any],
    ml_config: dict[str, Any],
    db: AsyncSession,
) -> dict[str, bool] | None:
    """Validate extracted values against historical ValidationResult records.

    Returns a ``{target: bool}`` dict where ``True`` means the current value
    is within the expected range for that target.  Returns ``None`` when there
    is insufficient history to build a model (< 3 historical rows).

    ``ml_config`` fields:
    - ``targets`` (list[str]): value column names to validate.
    - ``model`` (str, default "range"): "range" | "day_of_week".
    - ``validation_range`` (str, optional): if "day_of_week", forces the
      day-of-week model regardless of the ``model`` field.
    - ``lookback_partitions`` (int, default 30): how many historical rows to use.
    """
    from src.shared.db.models import ValidationResult

    targets: list[str] = ml_config.get("targets", [])
    if not targets:
        return None

    lookback: int = ml_config.get("lookback_partitions", 30)
    model: str = ml_config.get("model", "range")
    validation_range: str = ml_config.get("validation_range", "")
    if validation_range == "day_of_week":
        model = "day_of_week"

    # Query historical results for the same dataset + rule
    result = await db.execute(
        select(ValidationResult)
        .where(
            ValidationResult.dataset_urn == dataset_urn,
            ValidationResult.rule_id == rule_id,
        )
        .order_by(ValidationResult.measured_at.desc())
        .limit(lookback)
    )
    history_rows = result.scalars().all()

    if len(history_rows) < _MIN_HISTORY_ROWS:
        logger.debug(
            "ml_validation_insufficient_history",
            extra={
                "dataset_urn": dataset_urn,
                "rule_id": rule_id,
                "history_count": len(history_rows),
                "required": _MIN_HISTORY_ROWS,
            },
        )
        return None

    verdicts: dict[str, bool] = {}
    for target in targets:
        current_val = values.get(target)
        if current_val is None:
            verdicts[target] = False
            continue

        try:
            current_float = float(current_val)
        except (TypeError, ValueError):
            verdicts[target] = False
            continue

        history: list[dict[str, Any]] = [
            {"values": row.values, "measured_at": row.measured_at}
            for row in history_rows
        ]

        if model == "day_of_week":
            verdicts[target] = _validate_day_of_week(
                history, target, current_float
            )
        else:
            verdicts[target] = _validate_range(history, target, current_float)

    return verdicts


def _validate_range(
    history: list[dict[str, Any]],
    target: str,
    current_value: float,
) -> bool:
    """Range model: current value must be within [min, max] of the lookback window."""
    historical_values: list[float] = []
    for entry in history:
        raw = entry["values"].get(target)
        if raw is not None:
            try:
                historical_values.append(float(raw))
            except (TypeError, ValueError):
                pass

    if not historical_values:
        return False

    hist_min = min(historical_values)
    hist_max = max(historical_values)
    return hist_min <= current_value <= hist_max


def _validate_day_of_week(
    history: list[dict[str, Any]],
    target: str,
    current_value: float,
) -> bool:
    """Day-of-week model: current value within mean +/- 2*std for the same weekday.

    Groups historical values by weekday of ``measured_at``, computes per-day
    mean and standard deviation, then checks whether ``current_value`` falls
    within mean +/- 2*std for the current weekday.

    Falls back to the global range model if there are no historical values for
    the current weekday.
    """
    from datetime import datetime, timezone

    # Determine current weekday (0=Monday … 6=Sunday) using UTC now
    current_weekday = datetime.now(tz=timezone.utc).weekday()

    # Group historical values by weekday
    by_weekday: dict[int, list[float]] = {}
    for entry in history:
        raw = entry["values"].get(target)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        measured_at = entry["measured_at"]
        if measured_at is None:
            continue
        # Ensure timezone-aware
        if hasattr(measured_at, "weekday"):
            wd = measured_at.weekday()
        else:
            continue
        by_weekday.setdefault(wd, []).append(val)

    weekday_vals = by_weekday.get(current_weekday, [])
    if len(weekday_vals) < 2:
        # Fall back to global range model when insufficient same-weekday data
        all_vals: list[float] = [v for vals in by_weekday.values() for v in vals]
        if not all_vals:
            return False
        return min(all_vals) <= current_value <= max(all_vals)

    # Compute mean and std for this weekday
    mean = sum(weekday_vals) / len(weekday_vals)
    variance = sum((v - mean) ** 2 for v in weekday_vals) / (len(weekday_vals) - 1)
    std = math.sqrt(variance)

    lower = mean - 2 * std
    upper = mean + 2 * std
    return lower <= current_value <= upper
