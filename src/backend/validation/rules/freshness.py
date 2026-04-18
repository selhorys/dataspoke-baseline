"""Freshness rule evaluator."""

import logging
from datetime import UTC, datetime
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation, parse_duration_seconds
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


@register_rule("freshness")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Evaluate freshness: hours since last OperationClass vs lookback_interval."""
    from datahub.metadata.schema_classes import OperationClass

    rule_id = rule.get("rule_id", "")
    lookback_interval = rule.get("lookback_interval", "24h")

    # Parse interval to seconds
    try:
        max_seconds = parse_duration_seconds(lookback_interval)
    except Exception:
        max_seconds = 86400.0  # default to 24h on parse failure

    operations: list[OperationClass] = await datahub.get_timeseries(
        dataset_urn, OperationClass, limit=1
    )

    if not operations:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"hours_since_last_update": None},
            validation=None,
            issues=[{"msg": "No operation records found for dataset", "type": "no_data"}],
            partition=partition,
        )

    last_op = operations[0]
    last_ts = getattr(last_op, "lastUpdatedTimestamp", None) or getattr(
        last_op, "timestampMillis", None
    )

    if last_ts is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"hours_since_last_update": None},
            validation=None,
            issues=[{"msg": "Last operation has no timestamp", "type": "missing_timestamp"}],
            partition=partition,
        )

    last_dt = datetime.fromtimestamp(last_ts / 1000, tz=UTC)
    seconds_since = (datetime.now(tz=UTC) - last_dt).total_seconds()
    hours_since = round(seconds_since / 3600, 3)

    values: dict[str, Any] = {"hours_since_last_update": hours_since}

    if seconds_since <= max_seconds:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="SUCCESS",
            values=values,
            validation=None,
            issues=[],
            partition=partition,
        )

    max_hours = round(max_seconds / 3600, 3)
    return RuleEvaluation(
        rule_id=rule_id,
        assertion_result="FAILURE",
        values=values,
        validation=None,
        issues=[
            {
                "msg": f"Dataset is stale: {hours_since}h since last update (max {max_hours}h)",
                "type": "freshness_violation",
                "hours_since": hours_since,
                "max_hours": max_hours,
            }
        ],
        partition=partition,
    )
