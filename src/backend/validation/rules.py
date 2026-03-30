"""Rule evaluators for the DataHub assertion layer.

Each rule type maps to one or more DataHub aspects.  The evaluator
fetches the relevant aspect(s), extracts the target metric(s), applies
the specified condition, and returns a RuleEvaluation with the result.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


@dataclass
class RuleEvaluation:
    """Result of evaluating a single rule against a dataset partition."""

    rule_id: str
    assertion_result: str  # "SUCCESS" | "FAILURE" | "ERROR"
    values: dict[str, Any]
    validation: dict[str, bool] | None
    issues: list[dict[str, Any]]
    partition: dict[str, Any]


async def evaluate_rule(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Dispatch rule evaluation to the appropriate handler.

    The ``rule`` dict must contain at minimum:
    - ``rule_id``: unique identifier within the config
    - ``type``: one of freshness | volume | field | schema | sql | custom
    """
    rule_id = rule.get("rule_id", "")
    rule_type = rule.get("type", "custom").lower()

    try:
        if rule_type == "freshness":
            return await _evaluate_freshness(datahub, dataset_urn, rule, partition)
        elif rule_type == "volume":
            return await _evaluate_volume(datahub, dataset_urn, rule, partition)
        elif rule_type == "field":
            return await _evaluate_field(datahub, dataset_urn, rule, partition)
        elif rule_type == "schema":
            return await _evaluate_schema(datahub, dataset_urn, rule, partition)
        elif rule_type == "sql":
            return await _evaluate_sql(datahub, dataset_urn, rule, partition, db=db)
        else:
            return await _evaluate_custom(datahub, dataset_urn, rule, partition, db=db)
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
            issues=[{"msg": f"Unexpected error during evaluation: {exc}", "type": "evaluation_error"}],
            partition=partition,
        )


def _parse_duration_seconds(value: str) -> float:
    """Parse a human-readable duration string into seconds.

    Supported formats: "6h", "6 hours", "30m", "30 minutes", "1 hour",
    "2d", "2 days", "1 day", "90s", "90 seconds".
    Raises ValueError for unrecognised formats.
    """
    import re

    value = value.strip().lower()
    # Pattern: optional space between number and unit
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", value)
    if not m:
        raise ValueError(f"Cannot parse duration: {value!r}")
    amount = float(m.group(1))
    unit = m.group(2)
    _unit_seconds: dict[str, float] = {
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "m": 60.0,
        "min": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "d": 86400.0,
        "day": 86400.0,
        "days": 86400.0,
    }
    if unit not in _unit_seconds:
        raise ValueError(f"Unknown duration unit: {unit!r}")
    return amount * _unit_seconds[unit]


async def _evaluate_freshness(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
) -> RuleEvaluation:
    """Evaluate freshness: hours since last OperationClass vs lookback_interval."""
    from datahub.metadata.schema_classes import OperationClass

    rule_id = rule.get("rule_id", "")
    lookback_interval = rule.get("lookback_interval", "24h")

    # Parse interval to seconds
    try:
        max_seconds = _parse_duration_seconds(lookback_interval)
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


async def _evaluate_volume(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
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
    passed, issue_msg = _evaluate_condition(row_count, condition)

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


async def _evaluate_field(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
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

    # Map metric name → DataHub field profile attribute
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
    passed, issue_msg = _evaluate_condition(numeric_value, condition)

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


async def _evaluate_schema(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
) -> RuleEvaluation:
    """Evaluate schema compatibility against expected fields/types."""
    from datahub.metadata.schema_classes import SchemaMetadataClass

    rule_id = rule.get("rule_id", "")
    expected_fields: list[dict[str, Any]] = rule.get("expected_fields", [])
    compatibility = rule.get("compatibility", "superset")  # exact_match | superset | subset

    schema: SchemaMetadataClass | None = await datahub.get_aspect(dataset_urn, SchemaMetadataClass)

    if schema is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"field_count": 0},
            validation=None,
            issues=[{"msg": "No schema metadata found for dataset", "type": "no_schema"}],
            partition=partition,
        )

    actual_fields = schema.fields or []
    actual_field_map: dict[str, str] = {
        f.fieldPath: getattr(f, "nativeDataType", "") for f in actual_fields
    }

    expected_field_map: dict[str, str] = {
        f["name"]: f.get("type", "") for f in expected_fields
    }

    missing_fields = [name for name in expected_field_map if name not in actual_field_map]
    extra_fields = [name for name in actual_field_map if name not in expected_field_map]

    # Type mismatches for fields present in both
    type_mismatches = [
        {
            "field": name,
            "expected": expected_field_map[name],
            "actual": actual_field_map[name],
        }
        for name in expected_field_map
        if name in actual_field_map
        and expected_field_map[name]
        and actual_field_map[name] != expected_field_map[name]
    ]

    values: dict[str, Any] = {
        "actual_field_count": len(actual_field_map),
        "expected_field_count": len(expected_field_map),
        "missing_field_count": len(missing_fields),
        "extra_field_count": len(extra_fields),
        "type_mismatch_count": len(type_mismatches),
    }

    issues: list[dict[str, Any]] = []

    if compatibility == "exact_match":
        if missing_fields or extra_fields or type_mismatches:
            if missing_fields:
                issues.append(
                    {"msg": f"Missing fields: {missing_fields}", "type": "missing_fields"}
                )
            if extra_fields:
                issues.append(
                    {"msg": f"Extra fields not in expected schema: {extra_fields}", "type": "extra_fields"}
                )
    elif compatibility == "superset":
        # Actual schema must contain all expected fields
        if missing_fields:
            issues.append(
                {"msg": f"Missing required fields: {missing_fields}", "type": "missing_fields"}
            )
    elif compatibility == "subset":
        # All actual fields must be in expected schema
        if extra_fields:
            issues.append(
                {"msg": f"Unexpected fields in schema: {extra_fields}", "type": "extra_fields"}
            )

    if type_mismatches:
        for tm in type_mismatches:
            issues.append(
                {
                    "msg": f"Type mismatch for field '{tm['field']}': expected '{tm['expected']}', got '{tm['actual']}'",
                    "type": "type_mismatch",
                    **tm,
                }
            )

    assertion_result = "SUCCESS" if not issues else "FAILURE"
    return RuleEvaluation(
        rule_id=rule_id,
        assertion_result=assertion_result,
        values=values,
        validation=None,
        issues=issues,
        partition=partition,
    )


async def _evaluate_sql(
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
    from src.backend.validation.timeseries import resolve_source_config, execute_sql

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
        source_type, locator, identifier, auth = await resolve_source_config(
            db, dataset_urn, rule
        )
        rows = await execute_sql(source_type, locator, identifier, auth, statement)
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

    passed, issue_msg = _evaluate_condition(scalar_value, condition)
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


async def _evaluate_custom(
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


def _evaluate_condition(actual_value: Any, condition: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate a condition dict against an actual numeric value.

    Supported condition types:
    - between: {"type": "between", "min": X, "max": Y}
    - less_than: {"type": "less_than", "value": X}
    - less_than_or_equal: {"type": "less_than_or_equal", "value": X}
    - greater_than: {"type": "greater_than", "value": X}
    - greater_than_or_equal: {"type": "greater_than_or_equal", "value": X}
    - equal: {"type": "equal", "value": X}
    - not_equal: {"type": "not_equal", "value": X}

    Returns (passed, issue_message).
    """
    if not condition:
        # No condition specified → trivially pass
        return True, ""

    condition_type = condition.get("type", "")

    try:
        actual = float(actual_value)
    except (TypeError, ValueError):
        return False, f"Cannot compare non-numeric value '{actual_value}' with condition"

    if condition_type == "between":
        min_val = float(condition.get("min", 0))
        max_val = float(condition.get("max", float("inf")))
        if min_val <= actual <= max_val:
            return True, ""
        return False, f"Value {actual} is not between {min_val} and {max_val}"

    elif condition_type in ("less_than", "lt"):
        threshold = float(condition["value"])
        if actual < threshold:
            return True, ""
        return False, f"Value {actual} is not less than {threshold}"

    elif condition_type in ("less_than_or_equal", "lte"):
        threshold = float(condition["value"])
        if actual <= threshold:
            return True, ""
        return False, f"Value {actual} is not less than or equal to {threshold}"

    elif condition_type in ("greater_than", "gt"):
        threshold = float(condition["value"])
        if actual > threshold:
            return True, ""
        return False, f"Value {actual} is not greater than {threshold}"

    elif condition_type in ("greater_than_or_equal", "gte"):
        threshold = float(condition["value"])
        if actual >= threshold:
            return True, ""
        return False, f"Value {actual} is not greater than or equal to {threshold}"

    elif condition_type in ("equal", "eq"):
        threshold = float(condition["value"])
        if actual == threshold:
            return True, ""
        return False, f"Value {actual} does not equal {threshold}"

    elif condition_type in ("not_equal", "neq"):
        threshold = float(condition["value"])
        if actual != threshold:
            return True, ""
        return False, f"Value {actual} equals {threshold} (expected not equal)"

    else:
        return False, f"Unknown condition type: '{condition_type}'"
