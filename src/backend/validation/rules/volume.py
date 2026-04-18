"""Volume rule evaluator."""

import logging
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation, evaluate_condition
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


@register_rule("volume")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Evaluate volume: rowCount from latest DatasetProfileClass vs condition."""
    from datahub.metadata.schema_classes import DatasetProfileClass

    rule_id = rule.get("rule_id", "")
    condition = rule.get("condition", {})

    profiles: list[DatasetProfileClass] = await datahub.get_timeseries(
        dataset_urn, DatasetProfileClass, limit=1
    )

    if not profiles:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"row_count": None},
            validation=None,
            issues=[{"msg": "No profile data found for dataset", "type": "no_data"}],
            partition=partition,
        )

    latest = profiles[0]
    row_count = getattr(latest, "rowCount", None)

    if row_count is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"row_count": None},
            validation=None,
            issues=[{"msg": "Profile has no rowCount", "type": "missing_metric"}],
            partition=partition,
        )

    values: dict[str, Any] = {"row_count": row_count}
    passed, issue_msg = evaluate_condition(row_count, condition)

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
        issues=[{"msg": issue_msg, "type": "volume_violation", "row_count": row_count}],
        partition=partition,
    )
