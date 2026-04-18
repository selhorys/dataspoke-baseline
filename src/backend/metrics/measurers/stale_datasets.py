"""Measurer: stale_datasets — datasets without a freshness rule or with a freshness failure."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import ValidationConfig, ValidationResult


@register_measurer("stale_datasets")
async def measure(
    datasets: list[str],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
) -> tuple[float, dict[str, Any]]:
    """Aggregate stale-dataset count over validation_results.

    A dataset is counted as stale if:
    - It has no active ValidationConfig with a freshness rule
      (category: ``no_freshness_rule``), OR
    - Its latest ValidationResult for the freshness rule is a FAILURE
      (category: ``freshness_failure``).
    """
    affected: list[dict[str, Any]] = []

    for urn in datasets:
        # Query active validation config for this dataset
        config_q = select(ValidationConfig).where(
            ValidationConfig.dataset_urn == urn,
            ValidationConfig.is_active == True,  # noqa: E712
        )
        config_result = await db.execute(config_q)
        config_row = config_result.scalar_one_or_none()

        if config_row is None:
            affected.append({"urn": urn, "category": "no_freshness_rule"})
            continue

        # Find a freshness rule in the config
        freshness_rule_id: str | None = None
        for rule in config_row.rules or []:
            if isinstance(rule, dict) and rule.get("type") == "freshness":
                freshness_rule_id = rule.get("rule_id")
                break

        if freshness_rule_id is None:
            affected.append({"urn": urn, "category": "no_freshness_rule"})
            continue

        # Query the latest validation result for that freshness rule
        result_q = (
            select(ValidationResult)
            .where(
                ValidationResult.dataset_urn == urn,
                ValidationResult.rule_id == freshness_rule_id,
            )
            .order_by(ValidationResult.measured_at.desc())
            .limit(1)
        )
        val_result = await db.execute(result_q)
        val_row = val_result.scalar_one_or_none()

        if val_row is None or val_row.assertion_result == "FAILURE":
            affected.append(
                {
                    "urn": urn,
                    "category": "freshness_failure",
                    "detail": {"rule_id": freshness_rule_id},
                }
            )

    return float(len(affected)), {
        "dataset_count": len(datasets),
        "datasets": affected,
    }
