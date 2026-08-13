"""Measurer: validation-score — sums per-dataset validation scores within the metric's window.

The window is ``metric_conf["time_window_sec"]``, applied uniformly to every dataset in
the run: it is the recency SLO the governance lead declares, not a quantity read off a
dataset's own validation cadence.

The score counted is the latest ``ValidationResult`` row's ``score`` IFF its
``data_time`` falls within that window. A dataset with no qualifying row contributes
0.0 and appears in the breakdown.

Spec: spec/feature/BACKEND.md §Metrics Service — Measurement window
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
        Must contain ``time_window_sec`` (positive int) — the measurement window
        applied to every dataset.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``validation_results``.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``validation_score_sum``; breakdown lists datasets with score < 1.0,
        no validation result at all, or a latest result outside the window.
    """
    if not datasets:
        return (
            {"total": 0.0, "validation_score_sum": 0.0},
            {"dataset_count": 0, "datasets": []},
        )

    window_sec = int(metric_conf["time_window_sec"])
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(seconds=window_sec)

    # ── 1. Fetch the latest ValidationResult row per dataset ─────────────────
    # A single query: row_number() partitioned by dataset_urn ordered by data_time
    # desc, filtered to rn == 1 — one round trip for the whole dataset list.
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
    ).where(sub.c.rn == 1)
    rows = (await db.execute(rows_q)).all()

    latest: dict[str, tuple[datetime, float]] = {
        row.dataset_urn: (row.data_time, row.score) for row in rows
    }

    # ── 2. Evaluate per dataset ───────────────────────────────────────────────
    total = len(datasets)
    score_sum = 0.0
    failed_datasets: list[dict[str, Any]] = []

    for urn in datasets:
        latest_row = latest.get(urn)

        if latest_row is None:
            # No validation result at all
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "latest_data_time": None,
                        "score": None,
                        "time_window_sec": window_sec,
                    },
                }
            )
            continue

        latest_data_time, latest_score = latest_row

        if latest_data_time < cutoff:
            # Latest row is outside the window
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "latest_data_time": latest_data_time.isoformat(),
                        "score": latest_score,
                        "time_window_sec": window_sec,
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
