"""SQL-based rule evaluator."""

import logging
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation, evaluate_condition
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


@register_rule("sql")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """SQL-based evaluation — execute ``rule["statement"]`` and check the scalar result.

    Requires ``db`` (AsyncSession) to resolve source connection details.
    Falls back to ERROR "not yet implemented" when the rule has no ``statement``
    field or when ``db`` is not provided (stub-compatible behaviour).
    """
    from src.backend.validation.timeseries import execute_sql, resolve_source_config

    rule_id = rule.get("rule_id", "")
    statement = rule.get("statement")

    if not statement or db is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"msg": "SQL execution engine not yet implemented", "type": "not_implemented"}],
            partition=partition,
        )

    try:
        platform, locator, identifier, auth = await resolve_source_config(
            db, dataset_urn, rule
        )
        rows = await execute_sql(platform, locator, identifier, auth, statement)
    except Exception as exc:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"msg": f"SQL source error: {exc}", "type": "source_error"}],
            partition=partition,
        )

    if not rows:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"msg": "SQL statement returned no rows", "type": "no_data"}],
            partition=partition,
        )

    # Extract scalar: first column of the first row
    first_row = rows[0]
    scalar_value = next(iter(first_row.values()), None)

    condition = rule.get("condition", {})
    values: dict[str, Any] = {"result": scalar_value}

    passed, issue_msg = evaluate_condition(scalar_value, condition)
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
        issues=[{"msg": issue_msg, "type": "sql_condition_violation", "result": scalar_value}],
        partition=partition,
    )
