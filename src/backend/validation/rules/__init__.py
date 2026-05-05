"""Validation rule evaluation — registry-dispatched.

Re-exports:
- RuleEvaluation   — dataclass holding evaluation result
- evaluate_rule    — async dispatcher (public API, identical signature to old rules.py)
- evaluate_condition — condition helper (public name, was _evaluate_condition)
- parse_duration_seconds — duration parser (public name, was _parse_duration_seconds)
"""

import logging
from typing import Any

# Import each evaluator module so its @register_rule decorator runs and
# populates the registry before evaluate_rule is called.
from src.backend.validation.rules import (
    custom,  # noqa: F401
    field,  # noqa: F401
    freshness,  # noqa: F401
    schema,  # noqa: F401
    sql,  # noqa: F401
    volume,  # noqa: F401
)
from src.backend.validation.rules.helpers import (
    RuleEvaluation,
    evaluate_condition,
    parse_duration_seconds,
)
from src.backend.validation.rules.registry import get_evaluator
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

__all__ = [
    "RuleEvaluation",
    "evaluate_rule",
    "evaluate_condition",
    "parse_duration_seconds",
]


async def evaluate_rule(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Dispatch rule evaluation to the appropriate registered handler.

    Unknown rule types fall back to the "custom" evaluator, preserving the
    behaviour of the original if/elif ladder that ended with an ``else`` branch
    calling ``_evaluate_custom``.

    Exceptions from the evaluator are caught here and returned as an ERROR
    RuleEvaluation — same wrapping as the original rules.py:59-72.
    """
    rule_id = rule.get("rule_id", "")
    rule_type = rule.get("type", "custom").lower()

    evaluator = get_evaluator(rule_type) or get_evaluator("custom")

    try:
        return await evaluator(datahub, dataset_urn, rule, partition, db=db)
    except Exception as exc:
        logger.warning(
            "rule_evaluation_failed",
            exc_info=True,
            extra={"dataset_urn": dataset_urn, "rule_id": rule_id, "rule_type": rule_type},
        )
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[
                {
                    "msg": f"evaluator failed: {type(exc).__name__}",
                    "type": "evaluation_error",
                }
            ],
            partition=partition,
        )
