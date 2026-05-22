"""Measurer: validation-score — sums per-dataset validation scores within a per-dataset window.

For each dataset the window is derived from its own validation cadence:
if it has >= N+1 ``ValidationResult`` rows the mean inter-arrival gap over the
last N gaps is doubled (``WINDOW_FACTOR = 2``) to form the window.  Datasets
with fewer rows fall back to ``metric_conf["time_window_sec"]``.

The score counted is the latest row's ``score`` IFF its ``data_time`` falls
within the resolved window.  A dataset with no qualifying row contributes 0.0
and appears in the breakdown.

Spec: spec/feature/BACKEND.md §Metrics Service — Time windows
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.admin.config_service import get_runtime_config
from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import ValidationResult

# Multiplier applied to the mean inter-arrival gap to form the freshness window.
WINDOW_FACTOR = 2


@register_measurer("validation-score")
async def measure(
    datasets: list[str],
    metric_conf: dict[str, Any],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return validation-score values and a breakdown of datasets scoring below 1.0.

    Parameters
    ----------
    datasets:
        Dataset URNs to measure.
    metric_conf:
        Must contain ``time_window_sec`` (positive int) used as the fallback
        window when a dataset has fewer than N+1 validation results.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``validation_results``.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``validation_score_sum``; breakdown lists datasets with score < 1.0,
        no in-window row, or row outside the window.
    """
    if not datasets:
        return (
            {"total": 0.0, "validation_score_sum": 0.0},
            {"dataset_count": 0, "datasets": []},
        )

    rc = await get_runtime_config(db)
    N: int = rc.validation_score_n_intervals
    default_window_sec = int(metric_conf["time_window_sec"])
    now = datetime.now(tz=UTC)

    # ── 1. Fetch the latest N+1 ValidationResult rows per dataset ────────────
    # A single query: row_number() partitioned by dataset_urn ordered by data_time desc,
    # filtered to rn <= N+1.  This gives enough rows to compute N inter-arrival gaps.
    sub = (
        select(
            ValidationResult.dataset_urn,
            ValidationResult.data_time,
            ValidationResult.score,
            func.row_number()
            .over(
                partition_by=ValidationResult.dataset_urn,
                order_by=ValidationResult.data_time.desc(),
            )
            .label("rn"),
        )
        .where(ValidationResult.dataset_urn.in_(datasets))
        .subquery()
    )
    rows_q = select(
        sub.c.dataset_urn,
        sub.c.data_time,
        sub.c.score,
        sub.c.rn,
    ).where(sub.c.rn <= N + 1)
    rows = (await db.execute(rows_q)).all()

    # Group rows per dataset, already ordered desc by rn (rn==1 is most recent).
    grouped: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in rows:
        grouped[row.dataset_urn].append((row.data_time, row.score))
    # Ensure ascending rn order within each group (rn==1 is index 0).
    for urn in grouped:
        grouped[urn].sort(key=lambda t: t[0], reverse=True)  # desc by data_time

    # ── 2. Evaluate per dataset ───────────────────────────────────────────────
    total = len(datasets)
    score_sum = 0.0
    failed_datasets: list[dict[str, Any]] = []

    for urn in datasets:
        urn_rows = grouped.get(urn, [])

        # Resolve window
        if len(urn_rows) >= N + 1:
            # Compute N consecutive inter-arrival gaps (desc order, so row[0] is newest).
            gaps = [
                (urn_rows[i][0] - urn_rows[i + 1][0]).total_seconds()
                for i in range(N)
            ]
            mean_gap = sum(gaps) / N
            window_sec = int(mean_gap * WINDOW_FACTOR)
            window_source = "intervals"
        else:
            window_sec = default_window_sec
            window_source = "default"

        cutoff = now - timedelta(seconds=window_sec)

        if not urn_rows:
            # No validation result at all
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "latest_data_time": None,
                        "score": None,
                        "time_window_sec": window_sec,
                        "window_source": window_source,
                    },
                }
            )
            continue

        latest_data_time, latest_score = urn_rows[0]

        if latest_data_time < cutoff:
            # Latest row is outside the window
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "latest_data_time": latest_data_time.isoformat(),
                        "score": latest_score,
                        "time_window_sec": window_sec,
                        "window_source": window_source,
                    },
                }
            )
            continue

        # In-window row found: accumulate score
        score_sum += latest_score
        if latest_score < 1.0:
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "latest_data_time": latest_data_time.isoformat(),
                        "score": latest_score,
                        "time_window_sec": window_sec,
                        "window_source": window_source,
                    },
                }
            )

    values: dict[str, float] = {
        "total": float(total),
        "validation_score_sum": float(score_sum),
    }
    breakdown: dict[str, Any] = {
        "dataset_count": total,
        "datasets": failed_datasets,
    }
    return values, breakdown
