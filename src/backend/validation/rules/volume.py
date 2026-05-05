"""Volume rule evaluator."""

import logging
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation, evaluate_condition
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

_VALID_SOURCES = frozenset({"datahub_profile", "query"})


@register_rule("volume")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Evaluate volume against the configured source.

    Source discriminator (rule["source"]):
    - ``datahub_profile`` (default): read DatasetProfileClass.rowCount.
    - ``query``: execute SELECT COUNT(*) [WHERE filter] on the source platform.
    """
    rule_id = rule.get("rule_id", "")
    source = rule.get("source", "datahub_profile")

    if source not in _VALID_SOURCES:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"type": "invalid_rule", "msg": f"unknown source: {source}"}],
            partition=partition,
        )

    if source == "datahub_profile":
        return await _evaluate_datahub_profile(datahub, dataset_urn, rule_id, partition, rule)
    else:
        return await _evaluate_query(datahub, dataset_urn, rule, rule_id, partition, db)


async def _evaluate_datahub_profile(
    datahub: DataHubClient,
    dataset_urn: str,
    rule_id: str,
    partition: dict[str, Any],
    rule: dict[str, Any],
) -> RuleEvaluation:
    """Evaluate volume from DataHub DatasetProfileClass timeseries."""
    from datahub.metadata.schema_classes import DatasetProfileClass

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

    return _assess_row_count(rule_id, partition, row_count, condition)


async def _evaluate_query(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    rule_id: str,
    partition: dict[str, Any],
    db: Any,
) -> RuleEvaluation:
    """Evaluate volume via SELECT COUNT(*) [WHERE filter] on the source."""
    from src.backend.validation.timeseries import (
        execute_sql,
        quote_table_ref,
        resolve_source_config,
    )

    condition = rule.get("condition", {})
    platform, locator, identifier, auth = await resolve_source_config(db, dataset_urn, rule)

    try:
        table_ref = quote_table_ref(platform, identifier)
    except (ValueError, NotImplementedError) as exc:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"type": "invalid_rule", "msg": str(exc)}],
            partition=partition,
        )

    sql_filter = rule.get("filter")
    if sql_filter:
        sql = f"SELECT COUNT(*) AS row_count FROM {table_ref} WHERE {sql_filter}"
    else:
        sql = f"SELECT COUNT(*) AS row_count FROM {table_ref}"

    rows = await execute_sql(platform, locator, identifier, auth, sql)

    if not rows or rows[0].get("row_count") is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"row_count": None},
            validation=None,
            issues=[{"msg": "Query returned no data", "type": "no_data"}],
            partition=partition,
        )

    row_count = rows[0]["row_count"]
    return _assess_row_count(rule_id, partition, row_count, condition)


def _assess_row_count(
    rule_id: str,
    partition: dict[str, Any],
    row_count: Any,
    condition: dict[str, Any],
) -> RuleEvaluation:
    """Apply condition check to row_count and return a SUCCESS/FAILURE RuleEvaluation."""
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
