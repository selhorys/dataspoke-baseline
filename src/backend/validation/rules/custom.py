"""Custom rule evaluator — handles subtype dispatch including sql_timeseries."""

import logging
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


@register_rule("custom")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Custom rule evaluation.

    Handles ``subtype: "sql_timeseries"`` with optional ML validation.
    All other subtypes return ERROR "not yet implemented".
    """
    from src.backend.validation.ml_validation import validate_values
    from src.backend.validation.timeseries import execute_timeseries_sql

    rule_id = rule.get("rule_id", "")
    subtype = rule.get("subtype", "")

    if subtype != "sql_timeseries" or db is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"msg": "Custom rule engine not yet implemented", "type": "not_implemented"}],
            partition=partition,
        )

    # Execute the SQL timeseries query
    try:
        ts_result = await execute_timeseries_sql(db, dataset_urn, rule, partition)
    except Exception as exc:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"msg": f"Timeseries SQL error: {exc}", "type": "source_error"}],
            partition=partition,
        )

    extracted_values: dict[str, Any] = ts_result.get("values", {})
    resolved_partitions: dict[str, Any] = ts_result.get("partitions", {})

    # ML validation (optional, best-effort)
    ml_config = rule.get("ml_validation")
    validation_verdicts: dict[str, bool] | None = None
    if ml_config and extracted_values:
        try:
            validation_verdicts = await validate_values(
                dataset_urn=dataset_urn,
                rule_id=rule_id,
                values=extracted_values,
                ml_config=ml_config,
                db=db,
            )
        except Exception:
            logger.warning(
                "ml_validation_failed",
                exc_info=True,
                extra={"dataset_urn": dataset_urn, "rule_id": rule_id},
            )

    # Determine assertion result from ML verdicts
    assertion_result = "SUCCESS"
    issues: list[dict[str, Any]] = []

    if validation_verdicts is not None:
        failed_targets = [t for t, passed in validation_verdicts.items() if not passed]
        if failed_targets:
            assertion_result = "FAILURE"
            issues = [
                {
                    "msg": f"ML validation failed for target(s): {failed_targets}",
                    "type": "ml_validation_failure",
                    "failed_targets": failed_targets,
                }
            ]

    # Merge resolved partition into the effective partition
    effective_partition = {**partition, **resolved_partitions}

    return RuleEvaluation(
        rule_id=rule_id,
        assertion_result=assertion_result,
        values=extracted_values,
        validation=validation_verdicts,
        issues=issues,
        partition=effective_partition,
    )
