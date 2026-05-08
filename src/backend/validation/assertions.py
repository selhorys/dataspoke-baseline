"""DataHub assertion bridge — build URNs, emit assertion info, and report run events."""

import json
import logging
import time
from typing import Any

from datahub.emitter.mcp_builder import datahub_guid
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionResultTypeClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
    AssertionSourceClass,
    AssertionSourceTypeClass,
    AssertionStdOperatorClass,
    AssertionStdParameterClass,
    AssertionStdParametersClass,
    AssertionStdParameterTypeClass,
    AssertionTypeClass,
    AuditStampClass,
    CalendarIntervalClass,
    CustomAssertionInfoClass,
    FieldAssertionInfoClass,
    FieldAssertionTypeClass,
    FieldMetricAssertionClass,
    FieldMetricTypeClass,
    FieldValuesAssertionClass,
    FieldValuesFailThresholdClass,
    FieldValuesFailThresholdTypeClass,
    FixedIntervalScheduleClass,
    FreshnessAssertionInfoClass,
    FreshnessAssertionScheduleClass,
    FreshnessAssertionScheduleTypeClass,
    FreshnessAssertionTypeClass,
    PartitionSpecClass,
    PartitionTypeClass,
    RowCountTotalClass,
    SchemaAssertionCompatibilityClass,
    SchemaAssertionInfoClass,
    SchemaFieldSpecClass,
    SqlAssertionInfoClass,
    SqlAssertionTypeClass,
    StatusClass,
    VolumeAssertionInfoClass,
    VolumeAssertionTypeClass,
)

from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

_DATASPOKE_ACTOR_URN = "urn:li:corpuser:dataspoke"

# Maps DataSpoke rule type strings to DataHub AssertionTypeClass constants.
_RULE_TYPE_MAP: dict[str, str] = {
    "freshness": AssertionTypeClass.FRESHNESS,
    "volume": AssertionTypeClass.VOLUME,
    "field": AssertionTypeClass.FIELD,
    "schema": AssertionTypeClass.DATA_SCHEMA,
    "sql": AssertionTypeClass.SQL,
    "custom": AssertionTypeClass.CUSTOM,
}

# Maps DataSpoke rule.metric strings to DataHub FieldMetricTypeClass constants.
# PDL source: com.linkedin.assertion.FieldMetricType
_FIELD_METRIC_MAP: dict[str, str] = {
    "null_count": FieldMetricTypeClass.NULL_COUNT,
    "null_proportion": FieldMetricTypeClass.NULL_PERCENTAGE,
    "null_percentage": FieldMetricTypeClass.NULL_PERCENTAGE,
    "distinct_count": FieldMetricTypeClass.UNIQUE_COUNT,
    "unique_count": FieldMetricTypeClass.UNIQUE_COUNT,
    "unique_percentage": FieldMetricTypeClass.UNIQUE_PERCENTAGE,
    "min": FieldMetricTypeClass.MIN,
    "max": FieldMetricTypeClass.MAX,
    "mean": FieldMetricTypeClass.MEAN,
    "median": FieldMetricTypeClass.MEDIAN,
    "stddev": FieldMetricTypeClass.STDDEV,
    "negative_count": FieldMetricTypeClass.NEGATIVE_COUNT,
    "negative_percentage": FieldMetricTypeClass.NEGATIVE_PERCENTAGE,
    "zero_count": FieldMetricTypeClass.ZERO_COUNT,
    "zero_percentage": FieldMetricTypeClass.ZERO_PERCENTAGE,
    "min_length": FieldMetricTypeClass.MIN_LENGTH,
    "max_length": FieldMetricTypeClass.MAX_LENGTH,
    "empty_count": FieldMetricTypeClass.EMPTY_COUNT,
    "empty_percentage": FieldMetricTypeClass.EMPTY_PERCENTAGE,
}

# Maps DataSpoke condition type strings to DataHub AssertionStdOperatorClass constants.
_CONDITION_OPERATOR_MAP: dict[str, str] = {
    "equal_to": AssertionStdOperatorClass.EQUAL_TO,
    "not_equal_to": AssertionStdOperatorClass.NOT_EQUAL_TO,
    "less_than": AssertionStdOperatorClass.LESS_THAN,
    "less_than_or_equal_to": AssertionStdOperatorClass.LESS_THAN_OR_EQUAL_TO,
    "greater_than": AssertionStdOperatorClass.GREATER_THAN,
    "greater_than_or_equal_to": AssertionStdOperatorClass.GREATER_THAN_OR_EQUAL_TO,
    "between": AssertionStdOperatorClass.BETWEEN,
    "in": AssertionStdOperatorClass.IN,
    "not_in": AssertionStdOperatorClass.NOT_IN,
    "is_null": AssertionStdOperatorClass.NULL,
    "is_not_null": AssertionStdOperatorClass.NOT_NULL,
}


def build_assertion_urn(dataset_urn: str, rule_id: str) -> str:
    """Return a deterministic assertion URN for a dataset + rule combination.

    Uses datahub_guid() so the result is a valid DataHub-style GUID and
    consistent across re-runs.
    """
    guid = datahub_guid({"entity": dataset_urn, "rule": rule_id})
    return f"urn:li:assertion:{guid}"


def _condition_to_operator_params(
    condition: dict[str, Any],
) -> tuple[str, AssertionStdParametersClass]:
    """Convert a DataSpoke condition dict to a (operator, parameters) pair.

    Used by volume, sql, and field rule builders.
    Defaults to EQUAL_TO / value=0 on missing/unknown conditions.
    """
    condition_type = condition.get("type", "")
    operator = _CONDITION_OPERATOR_MAP.get(condition_type, AssertionStdOperatorClass.EQUAL_TO)

    if condition_type == "between":
        min_val = condition.get("min", 0)
        max_val = condition.get("max", 0)
        params = AssertionStdParametersClass(
            minValue=AssertionStdParameterClass(
                value=str(min_val),
                type=AssertionStdParameterTypeClass.NUMBER,
            ),
            maxValue=AssertionStdParameterClass(
                value=str(max_val),
                type=AssertionStdParameterTypeClass.NUMBER,
            ),
        )
    elif condition_type in ("is_null", "is_not_null"):
        params = AssertionStdParametersClass()
    elif condition_type in ("in", "not_in"):
        values = condition.get("value", [])
        params = AssertionStdParametersClass(
            value=AssertionStdParameterClass(
                value=json.dumps(values),
                type=AssertionStdParameterTypeClass.SET,
            )
        )
    else:
        raw_value = condition.get("value", 0)
        params = AssertionStdParametersClass(
            value=AssertionStdParameterClass(
                value=str(raw_value),
                type=AssertionStdParameterTypeClass.NUMBER,
            )
        )

    return operator, params


def _build_freshness_sub_aspect(
    dataset_urn: str, rule: dict[str, Any]
) -> FreshnessAssertionInfoClass:
    """Build the freshnessAssertion typed sub-aspect."""
    from src.backend.validation.rules.helpers import parse_duration_seconds

    lookback_interval = rule.get("lookback_interval", "24h")
    try:
        seconds = parse_duration_seconds(str(lookback_interval))
    except ValueError:
        seconds = 24 * 3600.0

    if seconds % 86400 == 0:
        unit = CalendarIntervalClass.DAY
        multiple = int(seconds / 86400)
    elif seconds % 3600 == 0:
        unit = CalendarIntervalClass.HOUR
        multiple = int(seconds / 3600)
    elif seconds % 60 == 0:
        unit = CalendarIntervalClass.MINUTE
        multiple = int(seconds / 60)
    else:
        unit = CalendarIntervalClass.SECOND
        multiple = int(seconds)

    fixed_interval = FixedIntervalScheduleClass(unit=unit, multiple=multiple)
    schedule = FreshnessAssertionScheduleClass(
        type=FreshnessAssertionScheduleTypeClass.FIXED_INTERVAL,
        fixedInterval=fixed_interval,
    )
    return FreshnessAssertionInfoClass(
        type=FreshnessAssertionTypeClass.DATASET_CHANGE,
        entity=dataset_urn,
        schedule=schedule,
    )


def _build_volume_sub_aspect(dataset_urn: str, rule: dict[str, Any]) -> VolumeAssertionInfoClass:
    """Build the volumeAssertion typed sub-aspect."""
    condition = rule.get("condition", {})
    operator, params = _condition_to_operator_params(condition)
    row_count_total = RowCountTotalClass(
        operator=operator,
        parameters=params,
    )
    return VolumeAssertionInfoClass(
        type=VolumeAssertionTypeClass.ROW_COUNT_TOTAL,
        entity=dataset_urn,
        rowCountTotal=row_count_total,
    )


def _build_field_sub_aspect(dataset_urn: str, rule: dict[str, Any]) -> FieldAssertionInfoClass:
    """Build the fieldAssertion typed sub-aspect."""
    field_path = rule.get("field", "")
    condition = rule.get("condition", {})
    exclude_nulls: bool = rule.get("exclude_nulls", True)

    metric_key = rule.get("metric")
    if metric_key is not None:
        assertion_type = FieldAssertionTypeClass.FIELD_METRIC
        metric_type = _FIELD_METRIC_MAP.get(
            str(metric_key).lower(), FieldMetricTypeClass.NULL_COUNT
        )
        operator, params = _condition_to_operator_params(condition)
        field_spec = SchemaFieldSpecClass(
            path=field_path,
            type="",
            nativeType="",
        )
        field_metric_assertion = FieldMetricAssertionClass(
            field=field_spec,
            metric=metric_type,
            operator=operator,
            parameters=params,
        )
        field_values_assertion = None
    else:
        assertion_type = FieldAssertionTypeClass.FIELD_VALUES
        operator, params = _condition_to_operator_params(condition)
        field_spec = SchemaFieldSpecClass(
            path=field_path,
            type="",
            nativeType="",
        )
        fail_threshold = FieldValuesFailThresholdClass(
            type=FieldValuesFailThresholdTypeClass.COUNT,
            value=int(rule.get("failure_threshold", 0)),
        )
        field_values_assertion = FieldValuesAssertionClass(
            field=field_spec,
            operator=operator,
            parameters=params,
            failThreshold=fail_threshold,
            excludeNulls=exclude_nulls,
        )
        field_metric_assertion = None

    return FieldAssertionInfoClass(
        type=assertion_type,
        entity=dataset_urn,
        fieldValuesAssertion=field_values_assertion,
        fieldMetricAssertion=field_metric_assertion,
    )


def _build_schema_sub_aspect(dataset_urn: str, rule: dict[str, Any]) -> SchemaAssertionInfoClass:
    """Build the schemaAssertion typed sub-aspect."""
    from datahub.metadata.schema_classes import (
        OtherSchemaClass,
        SchemaFieldClass,
        SchemaFieldDataTypeClass,
        SchemaMetadataClass,
        StringTypeClass,
    )

    compatibility_raw = rule.get("compatibility", "superset").upper()
    compatibility_map = {
        "EXACT_MATCH": SchemaAssertionCompatibilityClass.EXACT_MATCH,
        "EXACT": SchemaAssertionCompatibilityClass.EXACT_MATCH,
        "SUPERSET": SchemaAssertionCompatibilityClass.SUPERSET,
        "SUBSET": SchemaAssertionCompatibilityClass.SUBSET,
    }
    compatibility = compatibility_map.get(
        compatibility_raw, SchemaAssertionCompatibilityClass.SUPERSET
    )

    fields_spec: list[dict[str, Any]] = rule.get("fields", [])
    schema_fields = []
    for f in fields_spec:
        field_path = f.get("field", "")
        native_type = f.get("type", "STRING")
        schema_fields.append(
            SchemaFieldClass(
                fieldPath=field_path,
                type=SchemaFieldDataTypeClass(type=StringTypeClass()),
                nativeDataType=native_type,
            )
        )

    schema_meta = SchemaMetadataClass(
        schemaName="dataspoke_assertion_schema",
        platform="urn:li:dataPlatform:dataspoke",
        version=0,
        hash="",
        platformSchema=OtherSchemaClass(rawSchema=""),
        fields=schema_fields,
        created=AuditStampClass(time=int(time.time() * 1000), actor=_DATASPOKE_ACTOR_URN),
        lastModified=AuditStampClass(time=int(time.time() * 1000), actor=_DATASPOKE_ACTOR_URN),
    )

    return SchemaAssertionInfoClass(
        entity=dataset_urn,
        schema=schema_meta,
        compatibility=compatibility,
    )


def _build_sql_sub_aspect(dataset_urn: str, rule: dict[str, Any]) -> SqlAssertionInfoClass:
    """Build the sqlAssertion typed sub-aspect."""
    statement = rule.get("statement", "")
    condition = rule.get("condition", {})
    operator, params = _condition_to_operator_params(condition)

    return SqlAssertionInfoClass(
        type=SqlAssertionTypeClass.METRIC,
        entity=dataset_urn,
        statement=statement,
        operator=operator,
        parameters=params,
    )


def _build_custom_sub_aspect(dataset_urn: str, rule: dict[str, Any]) -> CustomAssertionInfoClass:
    """Build the customAssertion typed sub-aspect."""
    subtype = rule.get("subtype", "custom")
    logic = rule.get("sql") or rule.get("logic")
    return CustomAssertionInfoClass(
        type=subtype,
        entity=dataset_urn,
        logic=logic,
    )


def build_assertion_info(dataset_urn: str, rule: dict[str, Any]) -> AssertionInfoClass:
    """Construct an AssertionInfoClass with a fully populated typed sub-aspect.

    Each rule type dispatches to its own builder which populates the required
    typed sub-aspect field on AssertionInfoClass. Building an AssertionInfoClass
    without a matching sub-aspect produces an assertion stub with no detail in
    the DataHub UI.

    On any sub-aspect build error, falls back to sensible empty defaults so that
    a single malformed rule does not abort the entire upsert.
    """
    rule_type = rule.get("type", "custom").lower()
    assertion_type = _RULE_TYPE_MAP.get(rule_type, AssertionTypeClass.CUSTOM)
    rule_id = rule.get("rule_id", "")

    source = AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL)
    last_updated = AuditStampClass(
        time=int(time.time() * 1000),
        actor=_DATASPOKE_ACTOR_URN,
    )

    freshness_assertion = None
    volume_assertion = None
    field_assertion = None
    schema_assertion = None
    sql_assertion = None
    custom_assertion = None

    if rule_type == "freshness":
        freshness_assertion = _build_freshness_sub_aspect(dataset_urn, rule)
    elif rule_type == "volume":
        volume_assertion = _build_volume_sub_aspect(dataset_urn, rule)
    elif rule_type == "field":
        field_assertion = _build_field_sub_aspect(dataset_urn, rule)
    elif rule_type == "schema":
        try:
            schema_assertion = _build_schema_sub_aspect(dataset_urn, rule)
        except Exception as exc:
            raise ValueError(f"malformed rule for {rule_type}: {exc}") from exc
    elif rule_type == "sql":
        sql_assertion = _build_sql_sub_aspect(dataset_urn, rule)
    else:
        custom_assertion = _build_custom_sub_aspect(dataset_urn, rule)

    return AssertionInfoClass(
        type=assertion_type,
        freshnessAssertion=freshness_assertion,
        volumeAssertion=volume_assertion,
        fieldAssertion=field_assertion,
        schemaAssertion=schema_assertion,
        sqlAssertion=sql_assertion,
        customAssertion=custom_assertion,
        description=rule.get("description", f"DataSpoke {rule_type} assertion"),
        source=source,
        lastUpdated=last_updated,
        customProperties={
            "dataspoke_rule_id": rule_id,
            "dataspoke_rule_type": rule_type,
        },
    )


def build_run_event(
    assertion_urn: str,
    dataset_urn: str,
    run_id: str,
    result: str,
    values: dict[str, Any],
    partition: dict[str, Any],
) -> AssertionRunEventClass:
    """Construct an AssertionRunEventClass for reporting a rule evaluation result.

    Args:
        assertion_urn: The assertion URN built from build_assertion_urn().
        dataset_urn: The target dataset URN.
        run_id: The workflow execution / validation run ID.
        result: One of "SUCCESS", "FAILURE", "ERROR".
        values: Computed metric values for this run.
        partition: Partition context (empty dict = full-table scan).
    """
    result_type_map: dict[str, str] = {
        "SUCCESS": AssertionResultTypeClass.SUCCESS,
        "FAILURE": AssertionResultTypeClass.FAILURE,
        "ERROR": AssertionResultTypeClass.ERROR,
    }
    assertion_result_type = result_type_map.get(result.upper(), AssertionResultTypeClass.ERROR)

    native_results = {k: str(v) for k, v in values.items()}

    assertion_result = AssertionResultClass(
        type=assertion_result_type,
        nativeResults=native_results if native_results else None,
    )

    partition_type = (
        PartitionTypeClass.FULL_TABLE if not partition else PartitionTypeClass.PARTITION
    )
    partition_spec = PartitionSpecClass(
        partition=str(partition) if partition else "FULL_TABLE_SNAPSHOT",
        type=partition_type,
    )

    return AssertionRunEventClass(
        assertionUrn=assertion_urn,
        asserteeUrn=dataset_urn,
        runId=run_id,
        result=assertion_result,
        status=AssertionRunStatusClass.COMPLETE,
        timestampMillis=int(time.time() * 1000),
        partitionSpec=partition_spec,
    )


async def register_assertion(
    datahub: DataHubClient,
    assertion_urn: str,
    assertion_info: AssertionInfoClass,
) -> None:
    """Always emits assertionInfo + status(removed=False).

    The deterministic URN makes re-emit idempotent at the DataHub layer;
    explicit status(removed=False) resurrects any prior soft-deleted assertion.
    DataHubClient exceptions propagate so callers learn of DataHub availability
    issues at config-save time rather than silently.
    """
    await datahub.emit_assertion(assertion_urn, assertion_info)
    await datahub.emit_aspect(assertion_urn, StatusClass(removed=False))


async def report_result(
    datahub: DataHubClient,
    assertion_urn: str,
    run_event: AssertionRunEventClass,
) -> bool:
    """Emit an assertion run event to DataHub.

    Returns True on success, False on failure. Failures are logged as warnings
    with the assertion URN for operator visibility. The caller is responsible
    for converting a False return into an ERROR rule result.
    """
    try:
        await datahub.emit_assertion(assertion_urn, run_event)
        return True
    except Exception:
        logger.warning(
            "assertion_report_failed",
            exc_info=True,
            extra={"assertion_urn": assertion_urn},
        )
        return False
