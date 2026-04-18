"""Field-level metric rule evaluator."""

import logging
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation, evaluate_condition
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

# Map metric name → DataHub field profile attribute (private to this module)
_METRIC_ATTR_MAP: dict[str, str] = {
    "null_count": "nullCount",
    "null_proportion": "nullProportion",
    "unique_count": "uniqueCount",
    "unique_proportion": "uniqueProportion",
    "min": "min",
    "max": "max",
    "mean": "mean",
    "median": "median",
    "stdev": "stdev",
}


@register_rule("field")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Evaluate a field-level metric from the latest DatasetProfileClass."""
    from datahub.metadata.schema_classes import DatasetProfileClass

    rule_id = rule.get("rule_id", "")
    field_path = rule.get("field", "")
    metric_name = rule.get("metric", "null_count")
    condition = rule.get("condition", {})

    profiles: list[DatasetProfileClass] = await datahub.get_timeseries(
        dataset_urn, DatasetProfileClass, limit=1
    )

    if not profiles:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={metric_name: None},
            validation=None,
            issues=[{"msg": "No profile data found for dataset", "type": "no_data"}],
            partition=partition,
        )

    latest = profiles[0]
    field_profiles = getattr(latest, "fieldProfiles", None) or []

    target_fp = None
    for fp in field_profiles:
        if getattr(fp, "fieldPath", None) == field_path:
            target_fp = fp
            break

    if target_fp is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={metric_name: None},
            validation=None,
            issues=[
                {
                    "msg": f"Field '{field_path}' not found in profile",
                    "type": "field_not_found",
                    "field": field_path,
                }
            ],
            partition=partition,
        )

    attr_name = _METRIC_ATTR_MAP.get(metric_name, metric_name)
    actual_value = getattr(target_fp, attr_name, None)

    if actual_value is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={metric_name: None},
            validation=None,
            issues=[
                {
                    "msg": f"Metric '{metric_name}' not available for field '{field_path}'",
                    "type": "missing_metric",
                    "field": field_path,
                    "metric": metric_name,
                }
            ],
            partition=partition,
        )

    # Convert to float if possible for numeric comparison
    try:
        numeric_value = float(actual_value)
    except (TypeError, ValueError):
        numeric_value = actual_value

    values: dict[str, Any] = {metric_name: numeric_value, "field": field_path}
    passed, issue_msg = evaluate_condition(numeric_value, condition)

    if passed:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="SUCCESS",
            values=values,
            validation=None,
            issues=[],
            partition=partition,
        )

    return RuleEvaluation(
        rule_id=rule_id,
        assertion_result="FAILURE",
        values=values,
        validation=None,
        issues=[
            {
                "msg": issue_msg,
                "type": "field_violation",
                "field": field_path,
                "metric": metric_name,
                "actual_value": numeric_value,
            }
        ],
        partition=partition,
    )
