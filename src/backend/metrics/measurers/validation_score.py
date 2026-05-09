"""Measurer: pct_rules_passing — datasets categorised by validation score.

A dataset is *passing* if its most-recent result row (last-write-wins per
``data_time``) has ``score == 1.0``.  A dataset with no validation results
is counted as failing.

Spec: spec/feature/BACKEND.md §Metrics Service — baseline measurers
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import ValidationResult


@register_measurer("pct_rules_passing")
async def measure(
    datasets: list[str],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
    **_kwargs: Any,
) -> tuple[float, dict[str, Any]]:
    """Return failing-dataset count and per-dataset score breakdown.

    Parameters
    ----------
    datasets:
        Dataset URNs to measure.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``validation_results``.

    Returns
    -------
    tuple[float, dict]
        ``(failing_count, breakdown)`` where ``breakdown`` has keys
        ``dataset_count``, ``passing_count``, ``failing_count``,
        and ``datasets`` (list of per-dataset dicts with ``urn``,
        ``category``, ``latest_score``).
    """
    if not datasets:
        return 0.0, {
            "dataset_count": 0,
            "passing_count": 0,
            "failing_count": 0,
            "datasets": [],
        }

    # For each dataset_urn, get the most recent score using a window function
    # (last-write-wins per data_time, then latest data_time overall).
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
    latest_q = select(
        sub.c.dataset_urn,
        sub.c.score,
    ).where(sub.c.rn == 1)
    rows = (await db.execute(latest_q)).all()

    score_by_urn: dict[str, float] = {row.dataset_urn: row.score for row in rows}

    per_dataset: list[dict[str, Any]] = []
    failing_count = 0

    for urn in datasets:
        latest_score = score_by_urn.get(urn)
        if latest_score is None or latest_score < 1.0:
            category = "failing"
            failing_count += 1
        else:
            category = "passing"

        per_dataset.append(
            {
                "urn": urn,
                "category": category,
                "latest_score": latest_score,
            }
        )

    passing_count = len(datasets) - failing_count
    return float(failing_count), {
        "dataset_count": len(datasets),
        "passing_count": passing_count,
        "failing_count": failing_count,
        "datasets": per_dataset,
    }
