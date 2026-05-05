"""Freshness rule evaluator."""

import logging
from datetime import UTC, datetime
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation, parse_duration_seconds
from src.backend.validation.rules.registry import register_rule
from src.backend.validation.timeseries import _IDENTIFIER_RE
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

_VALID_SOURCES = frozenset({"datahub_operation", "datahub_profile", "query"})


def _coerce_to_utc_datetime(value: Any) -> datetime | None:
    """Convert a raw timestamp value to a UTC-aware datetime.

    Handles: datetime (with or without tz), epoch ms (int/float > 1e10),
    epoch seconds (int/float), and ISO-format strings.
    Returns None if conversion fails.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    if isinstance(value, (int, float)):
        # Heuristic: values > 1e11 are likely epoch milliseconds.
        if value > 1e11:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        return datetime.fromtimestamp(value, tz=UTC)

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            pass
        # Try interpreting as a numeric string.
        try:
            numeric = float(value)
            return _coerce_to_utc_datetime(numeric)
        except ValueError:
            pass

    return None


@register_rule("freshness")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Evaluate freshness against the configured source.

    Source discriminator (rule["source"]):
    - ``datahub_operation`` (default): read OperationClass.lastUpdatedTimestamp.
    - ``datahub_profile``: read DatasetProfileClass.timestampMillis.
    - ``query``: execute SELECT MAX(<last_modified_field>) on the source platform.
    """
    rule_id = rule.get("rule_id", "")
    lookback_interval = rule.get("lookback_interval", "24h")
    source = rule.get("source", "datahub_operation")

    if source not in _VALID_SOURCES:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[{"type": "invalid_rule", "msg": f"unknown source: {source}"}],
            partition=partition,
        )

    try:
        max_seconds = parse_duration_seconds(lookback_interval)
    except Exception:
        max_seconds = 86400.0

    if source == "datahub_operation":
        return await _evaluate_datahub_operation(
            datahub, dataset_urn, rule_id, partition, max_seconds
        )
    elif source == "datahub_profile":
        return await _evaluate_datahub_profile(
            datahub, dataset_urn, rule_id, partition, max_seconds
        )
    else:
        return await _evaluate_query(
            datahub, dataset_urn, rule, rule_id, partition, max_seconds, db
        )


async def _evaluate_datahub_operation(
    datahub: DataHubClient,
    dataset_urn: str,
    rule_id: str,
    partition: dict[str, Any],
    max_seconds: float,
) -> RuleEvaluation:
    """Evaluate freshness from DataHub OperationClass timeseries."""
    from datahub.metadata.schema_classes import OperationClass

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

    return _assess_timestamp(rule_id, partition, last_ts / 1000, max_seconds)


async def _evaluate_datahub_profile(
    datahub: DataHubClient,
    dataset_urn: str,
    rule_id: str,
    partition: dict[str, Any],
    max_seconds: float,
) -> RuleEvaluation:
    """Evaluate freshness from DataHub DatasetProfileClass timeseries."""
    from datahub.metadata.schema_classes import DatasetProfileClass

    profiles: list[DatasetProfileClass] = await datahub.get_timeseries(
        dataset_urn, DatasetProfileClass, limit=1
    )

    if not profiles:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"hours_since_last_update": None},
            validation=None,
            issues=[{"msg": "No profile data found for dataset", "type": "no_data"}],
            partition=partition,
        )

    latest = profiles[0]
    ts_ms = getattr(latest, "timestampMillis", None)

    if ts_ms is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"hours_since_last_update": None},
            validation=None,
            issues=[{"msg": "Profile has no timestampMillis", "type": "missing_timestamp"}],
            partition=partition,
        )

    return _assess_timestamp(rule_id, partition, ts_ms / 1000, max_seconds)


async def _evaluate_query(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    rule_id: str,
    partition: dict[str, Any],
    max_seconds: float,
    db: Any,
) -> RuleEvaluation:
    """Evaluate freshness via SELECT MAX(<last_modified_field>) on the source."""
    from src.backend.validation.timeseries import (
        execute_sql,
        quote_table_ref,
        resolve_source_config,
    )

    last_modified_field = rule.get("last_modified_field")
    if not last_modified_field or not _IDENTIFIER_RE.match(last_modified_field):
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="ERROR",
            values={},
            validation=None,
            issues=[
                {
                    "type": "invalid_rule",
                    "msg": (
                        "last_modified_field is required for source=query and must be a valid "
                        r"SQL identifier (\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z)"
                    ),
                }
            ],
            partition=partition,
        )

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
        sql = f'SELECT MAX("{last_modified_field}") AS last_ts FROM {table_ref} WHERE {sql_filter}'
    else:
        sql = f'SELECT MAX("{last_modified_field}") AS last_ts FROM {table_ref}'

    rows = await execute_sql(platform, locator, identifier, auth, sql)

    if not rows or rows[0].get("last_ts") is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"hours_since_last_update": None},
            validation=None,
            issues=[{"msg": "Query returned no data or NULL timestamp", "type": "no_data"}],
            partition=partition,
        )

    last_dt = _coerce_to_utc_datetime(rows[0]["last_ts"])
    if last_dt is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"hours_since_last_update": None},
            validation=None,
            issues=[{
                "msg": "Could not interpret query result as a timestamp",
                "type": "missing_timestamp",
            }],
            partition=partition,
        )

    seconds_since = (datetime.now(tz=UTC) - last_dt).total_seconds()
    return _assess_freshness_seconds(rule_id, partition, seconds_since, max_seconds)


def _assess_timestamp(
    rule_id: str,
    partition: dict[str, Any],
    epoch_seconds: float,
    max_seconds: float,
) -> RuleEvaluation:
    """Shared pass/fail logic for epoch-second timestamps."""
    last_dt = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    seconds_since = (datetime.now(tz=UTC) - last_dt).total_seconds()
    return _assess_freshness_seconds(rule_id, partition, seconds_since, max_seconds)


def _assess_freshness_seconds(
    rule_id: str,
    partition: dict[str, Any],
    seconds_since: float,
    max_seconds: float,
) -> RuleEvaluation:
    """Convert seconds-since-update to a SUCCESS/FAILURE RuleEvaluation."""
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
