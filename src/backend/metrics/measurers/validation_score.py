"""Measurer: validation-score — sums per-dataset validation scores within a time window.

For each dataset, the latest validation result whose ``data_time`` falls within
``[now - time_window_sec, now]`` is used. Datasets with no in-window row contribute
0.0 to the sum and appear in the breakdown.

Spec: spec/feature/BACKEND.md §Metrics Service — built-in active metric types
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import ValidationResult


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
        Must contain ``time_window_sec`` (positive int).
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``validation_results``.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``validation_score_sum``; breakdown lists only datasets with score < 1.0
        or no in-window row.
    """
    if not datasets:
        return (
            {"total": 0.0, "validation_score_sum": 0.0},
            {"dataset_count": 0, "datasets": []},
        )

    time_window_sec = int(metric_conf["time_window_sec"])
    now = datetime.now(tz=UTC)
    window_start = now - timedelta(seconds=time_window_sec)

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
        .where(
            ValidationResult.dataset_urn.in_(datasets),
            ValidationResult.data_time >= window_start,
            ValidationResult.data_time <= now,
        )
        .subquery()
    )
    latest_q = select(
        sub.c.dataset_urn,
        sub.c.data_time,
        sub.c.score,
    ).where(sub.c.rn == 1)
    rows = (await db.execute(latest_q)).all()

    score_by_urn: dict[str, tuple[datetime, float]] = {
        row.dataset_urn: (row.data_time, row.score) for row in rows
    }

    total = len(datasets)
    score_sum = 0.0
    failed_datasets: list[dict[str, Any]] = []

    for urn in datasets:
        entry = score_by_urn.get(urn)
        if entry is not None:
            data_time, score = entry
            score_sum += score
            if score < 1.0:
                failed_datasets.append(
                    {
                        "urn": urn,
                        "detail": {
                            "latest_data_time": data_time.isoformat(),
                            "score": score,
                        },
                    }
                )
        else:
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "latest_data_time": None,
                        "score": None,
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
