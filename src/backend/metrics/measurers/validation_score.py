"""Measurer: validation-score — datasets categorised by rule pass rate.

A dataset is *rules_passing* if its most-recent validation result row for
each rule has ``assertion_result='SUCCESS'``.  If any rule's latest result is
not ``SUCCESS`` (or the dataset has no validation results at all) it is
*rules_failing*.

Spec: spec/feature/BACKEND.md §Metrics Service — baseline measurers
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import ValidationResult

_PASSING = "SUCCESS"


@register_measurer("validation-score")
async def measure(
    datasets: list[str],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
    **_kwargs: Any,
) -> tuple[float, dict[str, Any]]:
    """Return failing-dataset count and per-dataset rule-score breakdown.

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
        ``dataset_count``, ``rules_passing_count``, ``rules_failing_count``,
        and ``datasets`` (list of per-dataset dicts with ``urn``,
        ``category``, ``rules_passing``, ``rules_failing``).
    """
    if not datasets:
        return 0.0, {
            "dataset_count": 0,
            "rules_passing_count": 0,
            "rules_failing_count": 0,
            "datasets": [],
        }

    # For each (dataset_urn, rule_id), get the most recent assertion_result
    # using a window function.
    sub = (
        select(
            ValidationResult.dataset_urn,
            ValidationResult.rule_id,
            ValidationResult.assertion_result,
            func.row_number()
            .over(
                partition_by=(ValidationResult.dataset_urn, ValidationResult.rule_id),
                order_by=ValidationResult.measured_at.desc(),
            )
            .label("rn"),
        )
        .where(ValidationResult.dataset_urn.in_(datasets))
        .subquery()
    )
    latest_q = select(
        sub.c.dataset_urn,
        sub.c.rule_id,
        sub.c.assertion_result,
    ).where(sub.c.rn == 1)
    rows = (await db.execute(latest_q)).all()

    # Group per dataset
    from collections import defaultdict

    per_urn: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        per_urn[row.dataset_urn].append((row.rule_id, row.assertion_result))

    per_dataset: list[dict[str, Any]] = []
    failing_count = 0

    for urn in datasets:
        rule_results = per_urn.get(urn, [])
        rules_passing = sum(1 for _, r in rule_results if r == _PASSING)
        rules_failing = len(rule_results) - rules_passing

        if not rule_results or rules_failing > 0:
            category = "rules_failing"
            failing_count += 1
        else:
            category = "rules_passing"

        # Fix #8: unified breakdown shape per spec BACKEND.md §Metrics Service
        # Include detail for the first failing rule (or first rule if all pass).
        detail: dict[str, Any] = {
            "failed": rules_failing,
            "total": len(rule_results),
        }
        # Add rule_id of first failing rule if any
        for rule_id, result in rule_results:
            if result != _PASSING:
                detail["rule_id"] = rule_id
                break

        per_dataset.append(
            {
                "urn": urn,
                "category": category,
                "detail": detail,
            }
        )

    passing_count = len(datasets) - failing_count
    return float(failing_count), {
        "dataset_count": len(datasets),
        "rules_passing_count": passing_count,
        "rules_failing_count": failing_count,
        "datasets": per_dataset,
    }
