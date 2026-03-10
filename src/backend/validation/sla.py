"""Predictive SLA monitoring with threshold learning and breach prediction."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.shared.config import SLA_ALERT_BEFORE_MINUTES

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DayBaseline:
    """Learned baseline statistics for a single day-of-week."""

    expected_value: float
    std_dev: float
    upper_bound: float  # expected + σ
    lower_bound: float  # expected - σ


@dataclass(slots=True)
class LearnedBaseline:
    """Per-day-of-week baselines learned from history."""

    day_baselines: dict[int, DayBaseline]  # weekday (0=Mon) -> baseline
    lookback_days: int
    sample_count: int


@dataclass(slots=True)
class SLATarget:
    """Parsed SLA configuration from validation_configs.sla_target JSONB."""

    freshness_hours: int = 24
    min_quality_score: float = 70.0
    deadline_utc: str | None = None
    alert_before_minutes: int = SLA_ALERT_BEFORE_MINUTES
    auto_adjust_thresholds: bool = True


@dataclass(slots=True)
class SLACheckResult:
    """Result of an SLA check against a dataset."""

    is_breaching: bool
    is_pre_breach: bool
    current_freshness_hours: float
    current_quality_score: float
    predicted_breach_at: datetime | None
    minutes_until_breach: float | None
    root_cause_urns: list[str]
    impact_urns: list[str]
    learned_baseline: LearnedBaseline | None
    violations: list[str]


async def check_sla(
    datahub: Any,
    dataset_urn: str,
    sla_target: dict[str, Any],
    history: list,
    quality_score: float,
) -> SLACheckResult:
    """Check SLA compliance for a dataset.

    Args:
        datahub: DataHubClient instance for lineage queries.
        dataset_urn: URN of the dataset to check.
        sla_target: Raw JSONB dict from validation config.
        history: List of DatasetProfileClass instances (timeseries).
        quality_score: Current overall quality score (0-100).

    Returns:
        SLACheckResult with breach status, predictions, and lineage.
    """
    target = _parse_sla_target(sla_target)
    violations: list[str] = []
    is_breaching = False

    # Compute current freshness from most recent profile timestamp
    current_freshness_hours = _compute_freshness_hours(history)

    # Check freshness threshold
    if current_freshness_hours > target.freshness_hours:
        is_breaching = True
        violations.append(
            f"Freshness breach: {current_freshness_hours:.1f}h "
            f"exceeds {target.freshness_hours}h limit"
        )

    # Check quality score threshold
    if quality_score < target.min_quality_score:
        is_breaching = True
        violations.append(
            f"Quality breach: score {quality_score:.1f} below {target.min_quality_score} minimum"
        )

    # Learn thresholds if auto-adjust is enabled
    learned_baseline: LearnedBaseline | None = None
    if target.auto_adjust_thresholds:
        learned_baseline = await learn_thresholds(history)
        if learned_baseline:
            today_dow = datetime.now(tz=UTC).weekday()
            day_bl = learned_baseline.day_baselines.get(today_dow)
            if day_bl and history:
                latest_row_count = _get_latest_row_count(history)
                if latest_row_count is not None and day_bl.std_dev > 0:
                    if latest_row_count < day_bl.lower_bound:
                        violations.append(
                            f"Row count {latest_row_count:.0f} below learned baseline "
                            f"{day_bl.lower_bound:.0f} for {_day_name(today_dow)}"
                        )
                    elif latest_row_count > day_bl.upper_bound:
                        violations.append(
                            f"Row count {latest_row_count:.0f} above learned baseline "
                            f"{day_bl.upper_bound:.0f} for {_day_name(today_dow)}"
                        )

    # Predict breach time
    is_pre_breach = False
    predicted_breach_at: datetime | None = None
    minutes_until_breach: float | None = None

    if not is_breaching:
        predicted_breach_at = await _predict_breach_time(history, target)
        if predicted_breach_at is not None:
            now = datetime.now(tz=UTC)
            minutes_until_breach = (predicted_breach_at - now).total_seconds() / 60
            if minutes_until_breach <= target.alert_before_minutes:
                is_pre_breach = True
                violations.append(
                    f"Pre-breach: predicted breach at {predicted_breach_at.isoformat()} "
                    f"({minutes_until_breach:.0f} min)"
                )

    # Traverse lineage if breaching or pre-breach
    root_cause_urns: list[str] = []
    impact_urns: list[str] = []
    if is_breaching or is_pre_breach:
        try:
            root_cause_urns = await datahub.get_upstream_lineage(dataset_urn)
        except Exception:
            pass
        try:
            impact_urns = await datahub.get_downstream_lineage(dataset_urn)
        except Exception:
            pass

    return SLACheckResult(
        is_breaching=is_breaching,
        is_pre_breach=is_pre_breach,
        current_freshness_hours=round(current_freshness_hours, 2),
        current_quality_score=round(quality_score, 2),
        predicted_breach_at=predicted_breach_at,
        minutes_until_breach=round(minutes_until_breach, 1) if minutes_until_breach else None,
        root_cause_urns=root_cause_urns,
        impact_urns=impact_urns,
        learned_baseline=learned_baseline,
        violations=violations,
    )


async def learn_thresholds(history: list, lookback_days: int = 28) -> LearnedBaseline | None:
    """Learn per-day-of-week baselines from profile history.

    Args:
        history: List of DatasetProfileClass instances.
        lookback_days: Number of days of history to consider.

    Returns:
        LearnedBaseline with per-day statistics, or None if insufficient data.
    """
    if not history:
        return None

    cutoff = datetime.now(tz=UTC) - timedelta(days=lookback_days)
    by_day: dict[int, list[float]] = {}
    sample_count = 0

    for p in history:
        ts_ms = getattr(p, "timestampMillis", None)
        if ts_ms is None:
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        if dt < cutoff:
            continue
        row_count = getattr(p, "rowCount", None)
        if row_count is None:
            continue
        dow = dt.weekday()
        by_day.setdefault(dow, []).append(float(row_count))
        sample_count += 1

    if sample_count == 0:
        return None

    day_baselines: dict[int, DayBaseline] = {}
    for dow, values in by_day.items():
        mean = sum(values) / len(values)
        if len(values) >= 2:
            variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
            std_dev = variance**0.5
        else:
            std_dev = 0.0
        day_baselines[dow] = DayBaseline(
            expected_value=round(mean, 2),
            std_dev=round(std_dev, 2),
            upper_bound=round(mean + std_dev, 2),
            lower_bound=round(mean - std_dev, 2),
        )

    return LearnedBaseline(
        day_baselines=day_baselines,
        lookback_days=lookback_days,
        sample_count=sample_count,
    )


async def _predict_breach_time(history: list, target: SLATarget) -> datetime | None:
    """Predict when the SLA freshness threshold will be breached.

    Uses Prophet if available, falling back to linear extrapolation.
    """
    if not history:
        return None

    timestamps = []
    for p in history:
        ts_ms = getattr(p, "timestampMillis", None)
        if ts_ms is not None:
            timestamps.append(datetime.fromtimestamp(ts_ms / 1000, tz=UTC))

    if len(timestamps) < 2:
        return None

    timestamps.sort()

    # Try Prophet first
    try:
        predicted = await _predict_breach_prophet(timestamps, target)
        if predicted is not None:
            return predicted
    except Exception:
        pass

    # Linear extrapolation fallback
    return _predict_breach_linear(timestamps, target)


async def _predict_breach_prophet(timestamps: list[datetime], target: SLATarget) -> datetime | None:
    """Use Prophet to extrapolate freshness trend and find breach point."""
    try:
        from prophet import Prophet
    except ImportError:
        return None

    import pandas as pd

    now = datetime.now(tz=UTC)
    rows = []
    for ts in timestamps:
        hours_ago = (now - ts).total_seconds() / 3600
        rows.append({"ds": ts, "y": hours_ago})

    if len(rows) < 2:
        return None

    df = pd.DataFrame(rows)

    def _fit():
        m = Prophet(interval_width=0.95)
        m.fit(df)
        future = m.make_future_dataframe(periods=7, freq="D")
        return m.predict(future)

    forecast = await asyncio.to_thread(_fit)

    for _, row in forecast.iterrows():
        if row["yhat"] >= target.freshness_hours:
            breach_dt = row["ds"]
            if hasattr(breach_dt, "to_pydatetime"):
                breach_dt = breach_dt.to_pydatetime()
            if breach_dt.tzinfo is None:
                breach_dt = breach_dt.replace(tzinfo=UTC)
            if breach_dt > now:
                return breach_dt

    return None


def _predict_breach_linear(timestamps: list[datetime], target: SLATarget) -> datetime | None:
    """Simple linear extrapolation to predict when freshness exceeds SLA."""
    if len(timestamps) < 2:
        return None

    now = datetime.now(tz=UTC)
    last_update = timestamps[-1]
    current_freshness_hours = (now - last_update).total_seconds() / 3600

    if current_freshness_hours >= target.freshness_hours:
        return None  # Already breaching

    # Compute average interval between updates
    intervals = []
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds() / 3600
        if delta > 0:
            intervals.append(delta)

    if not intervals:
        return None

    avg_interval_hours = sum(intervals) / len(intervals)

    # Time until breach = freshness_hours - current_freshness
    hours_until_breach = target.freshness_hours - current_freshness_hours

    # If next expected update would arrive before breach, no pre-breach
    hours_since_last = current_freshness_hours
    next_expected_update = avg_interval_hours - hours_since_last
    if next_expected_update > 0 and next_expected_update < hours_until_breach:
        return None

    return now + timedelta(hours=hours_until_breach)


def _parse_sla_target(raw: dict[str, Any]) -> SLATarget:
    """Parse JSONB dict into SLATarget dataclass."""
    return SLATarget(
        freshness_hours=int(raw.get("freshness_hours", 24)),
        min_quality_score=float(raw.get("min_quality_score", 70.0)),
        deadline_utc=raw.get("deadline_utc"),
        alert_before_minutes=int(raw.get("alert_before_minutes", SLA_ALERT_BEFORE_MINUTES)),
        auto_adjust_thresholds=bool(raw.get("auto_adjust_thresholds", True)),
    )


def _compute_freshness_hours(profiles: list) -> float:
    """Compute hours since most recent profile timestamp."""
    if not profiles:
        return float("inf")

    latest_ts: int | None = None
    for p in profiles:
        ts_ms = getattr(p, "timestampMillis", None)
        if ts_ms is not None:
            if latest_ts is None or ts_ms > latest_ts:
                latest_ts = ts_ms

    if latest_ts is None:
        return float("inf")

    last_dt = datetime.fromtimestamp(latest_ts / 1000, tz=UTC)
    return (datetime.now(tz=UTC) - last_dt).total_seconds() / 3600


def _get_latest_row_count(profiles: list) -> float | None:
    """Get row count from the most recent profile."""
    latest_ts: int | None = None
    latest_row_count: float | None = None
    for p in profiles:
        ts_ms = getattr(p, "timestampMillis", None)
        rc = getattr(p, "rowCount", None)
        if ts_ms is not None and rc is not None:
            if latest_ts is None or ts_ms > latest_ts:
                latest_ts = ts_ms
                latest_row_count = float(rc)
    return latest_row_count


def _day_name(weekday: int) -> str:
    """Return human-readable day name from weekday number."""
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return names[weekday] if 0 <= weekday <= 6 else f"day-{weekday}"
