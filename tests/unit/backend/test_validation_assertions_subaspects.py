"""Unit tests — typed sub-aspects and AssertionInfo conventions (Group A1, A2).

Spec sources:
- spec/DATAHUB_INTEGRATION.md §Assertion Aspects (7 mandatory conventions)
- .claude/skills/datahub-api/reference.md Pattern D (typed sub-aspect table + anti-patterns)
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from datahub.metadata.schema_classes import (
    AssertionSourceTypeClass,
    AssertionTypeClass,
    CalendarIntervalClass,
    FieldAssertionTypeClass,
    FieldMetricTypeClass,
)

from src.backend.validation.assertions import (
    build_assertion_info,
    build_assertion_urn,
    build_run_event,
    register_assertion,
    report_result,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"

# ── Group A1: typed sub-aspects populated ──────────────────────────────────────


def test_freshness_sub_aspect_is_not_none():
    """DATAHUB_INTEGRATION.md convention 1: freshnessAssertion must be populated for freshness rules."""
    rule = {"rule_id": "r_fresh", "type": "freshness", "lookback_interval": "24h"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.freshnessAssertion is not None, (
        "Anti-pattern: freshnessAssertion=None with type=FRESHNESS renders blank assertion in DataHub UI"
    )
    schedule = info.freshnessAssertion.schedule
    assert schedule is not None
    assert schedule.fixedInterval is not None, (
        "fixedInterval must be populated when schedule type is FIXED_INTERVAL"
    )
    assert schedule.fixedInterval.unit == CalendarIntervalClass.DAY
    assert schedule.fixedInterval.multiple == 1


def test_freshness_fixed_interval_24h_maps_to_day_1():
    """24h = 86400s → cleanest unit is DAY/1."""
    rule = {"rule_id": "r", "type": "freshness", "lookback_interval": "24h"}
    info = build_assertion_info(_DATASET_URN, rule)
    fi = info.freshnessAssertion.schedule.fixedInterval
    assert fi.unit == CalendarIntervalClass.DAY
    assert fi.multiple == 1


def test_freshness_fixed_interval_6h_maps_to_hour_6():
    """6h = 21600s → not divisible by 86400, divisible by 3600 → HOUR/6."""
    rule = {"rule_id": "r", "type": "freshness", "lookback_interval": "6h"}
    info = build_assertion_info(_DATASET_URN, rule)
    fi = info.freshnessAssertion.schedule.fixedInterval
    assert fi.unit == CalendarIntervalClass.HOUR
    assert fi.multiple == 6


def test_freshness_fixed_interval_30m_maps_to_minute_30():
    """30m = 1800s → not divisible by 3600, divisible by 60 → MINUTE/30."""
    rule = {"rule_id": "r", "type": "freshness", "lookback_interval": "30m"}
    info = build_assertion_info(_DATASET_URN, rule)
    fi = info.freshnessAssertion.schedule.fixedInterval
    assert fi.unit == CalendarIntervalClass.MINUTE
    assert fi.multiple == 30


def test_freshness_fixed_interval_2d_maps_to_day_2():
    """2d = 172800s → divisible by 86400 → DAY/2."""
    rule = {"rule_id": "r", "type": "freshness", "lookback_interval": "2d"}
    info = build_assertion_info(_DATASET_URN, rule)
    fi = info.freshnessAssertion.schedule.fixedInterval
    assert fi.unit == CalendarIntervalClass.DAY
    assert fi.multiple == 2


def test_freshness_fixed_interval_invalid_lookback_defaults_to_hour_24():
    """Unparseable lookback_interval defaults to HOUR/24."""
    rule = {"rule_id": "r", "type": "freshness", "lookback_interval": "bad-value"}
    info = build_assertion_info(_DATASET_URN, rule)
    fi = info.freshnessAssertion.schedule.fixedInterval
    assert fi.unit == CalendarIntervalClass.DAY
    assert fi.multiple == 1


def test_volume_sub_aspect_is_not_none():
    """DATAHUB_INTEGRATION.md convention 1: volumeAssertion must be populated for volume rules."""
    rule = {"rule_id": "r_vol", "type": "volume", "condition": {"type": "greater_than", "value": 0}}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.volumeAssertion is not None, (
        "Anti-pattern: volumeAssertion=None with type=VOLUME renders blank assertion in DataHub UI"
    )


def test_field_sub_aspect_is_not_none():
    """DATAHUB_INTEGRATION.md convention 1: fieldAssertion must be populated for field rules."""
    rule = {
        "rule_id": "r_field",
        "type": "field",
        "field": "rating_score",
        "condition": {"type": "less_than_or_equal_to", "value": 0},
    }
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion is not None, (
        "Anti-pattern: fieldAssertion=None with type=FIELD renders blank assertion in DataHub UI"
    )


def test_schema_sub_aspect_is_not_none():
    """DATAHUB_INTEGRATION.md convention 1: schemaAssertion must be populated for schema rules."""
    rule = {"rule_id": "r_schema", "type": "schema", "fields": [], "compatibility": "superset"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.schemaAssertion is not None, (
        "Anti-pattern: schemaAssertion=None with type=DATA_SCHEMA renders blank assertion in DataHub UI"
    )


def test_sql_sub_aspect_is_not_none():
    """DATAHUB_INTEGRATION.md convention 1: sqlAssertion must be populated for sql rules."""
    rule = {
        "rule_id": "r_sql",
        "type": "sql",
        "statement": "SELECT COUNT(*) FROM orders WHERE price <= 0",
        "condition": {"type": "equal_to", "value": 0},
    }
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.sqlAssertion is not None, (
        "Anti-pattern: sqlAssertion=None with type=SQL renders blank assertion in DataHub UI"
    )


def test_custom_sub_aspect_is_not_none():
    """DATAHUB_INTEGRATION.md convention 1: customAssertion must be populated for custom rules."""
    rule = {"rule_id": "r_custom", "type": "custom", "subtype": "sql_timeseries"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.customAssertion is not None, (
        "Anti-pattern: customAssertion=None with type=CUSTOM renders blank assertion in DataHub UI"
    )


# ── Group A1 cont.: assertionInfo.type matches rule type ─────────────────────


def test_freshness_type_constant():
    """DATAHUB_INTEGRATION.md table: freshness → AssertionTypeClass.FRESHNESS."""
    rule = {"rule_id": "r", "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.FRESHNESS


def test_volume_type_constant():
    """DATAHUB_INTEGRATION.md table: volume → AssertionTypeClass.VOLUME."""
    rule = {"rule_id": "r", "type": "volume", "condition": {}}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.VOLUME


def test_field_type_constant():
    """DATAHUB_INTEGRATION.md table: field → AssertionTypeClass.FIELD."""
    rule = {"rule_id": "r", "type": "field", "field": "col"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.FIELD


def test_schema_type_is_data_schema_not_schema():
    """DATAHUB_INTEGRATION.md note: PDL constant is DATA_SCHEMA (not SCHEMA — reserved-word workaround)."""
    rule = {"rule_id": "r", "type": "schema", "fields": []}
    info = build_assertion_info(_DATASET_URN, rule)
    # The spec is explicit: DATA_SCHEMA, not SCHEMA
    assert info.type == AssertionTypeClass.DATA_SCHEMA
    assert info.type == "DATA_SCHEMA"


def test_sql_type_constant():
    """DATAHUB_INTEGRATION.md table: sql → AssertionTypeClass.SQL."""
    rule = {"rule_id": "r", "type": "sql", "statement": "SELECT 1"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.SQL


def test_custom_type_constant():
    """DATAHUB_INTEGRATION.md table: custom → AssertionTypeClass.CUSTOM."""
    rule = {"rule_id": "r", "type": "custom"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.CUSTOM


# ── Group A1 cont.: source.type == EXTERNAL ──────────────────────────────────


def test_source_type_is_external_freshness():
    """DATAHUB_INTEGRATION.md convention 2: source.type=EXTERNAL on every DataSpoke-emitted assertion."""
    rule = {"rule_id": "r", "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL


def test_source_type_is_external_volume():
    """DATAHUB_INTEGRATION.md convention 2: EXTERNAL not NATIVE (NATIVE reserved for DataHub Cloud runner)."""
    rule = {"rule_id": "r", "type": "volume", "condition": {}}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL
    assert info.source.type != "NATIVE"


def test_source_type_is_external_field():
    """DATAHUB_INTEGRATION.md convention 2: source.type=EXTERNAL for field rules."""
    rule = {"rule_id": "r", "type": "field", "field": "col"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL


def test_source_type_is_external_schema():
    """DATAHUB_INTEGRATION.md convention 2: source.type=EXTERNAL for schema rules."""
    rule = {"rule_id": "r", "type": "schema", "fields": []}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL


def test_source_type_is_external_sql():
    """DATAHUB_INTEGRATION.md convention 2: source.type=EXTERNAL for sql rules."""
    rule = {"rule_id": "r", "type": "sql", "statement": "SELECT 1"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL


def test_source_type_is_external_custom():
    """DATAHUB_INTEGRATION.md convention 2: source.type=EXTERNAL for custom rules."""
    rule = {"rule_id": "r", "type": "custom"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL


# ── Group A1 cont.: lastUpdated actor + time ─────────────────────────────────


def test_last_updated_actor_is_dataspoke():
    """DATAHUB_INTEGRATION.md convention 4: lastUpdated.actor must be a corpuser URN for the DataSpoke service-user."""
    rule = {"rule_id": "r", "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    # Convention 4: populate lastUpdated with the DataSpoke service-user URN
    assert info.lastUpdated is not None
    assert info.lastUpdated.actor.startswith("urn:li:corpuser:")
    assert info.lastUpdated.actor != "urn:li:corpuser:"  # non-empty suffix


def test_last_updated_time_is_recent():
    """DATAHUB_INTEGRATION.md convention 4: lastUpdated.time must be a recent epoch-ms timestamp."""
    before_ms = int(time.time() * 1000)
    rule = {"rule_id": "r", "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    after_ms = int(time.time() * 1000)

    # 5 minutes window for test execution tolerance
    five_min_ms = 5 * 60 * 1000
    assert before_ms - five_min_ms <= info.lastUpdated.time <= after_ms + five_min_ms


# ── Group A1 cont.: deterministic URN ────────────────────────────────────────


def test_assertion_urn_deterministic_same_inputs():
    """DATAHUB_INTEGRATION.md convention 3: same inputs always produce the same URN (re-emit idempotent)."""
    urn1 = build_assertion_urn(_DATASET_URN, "rule_x")
    urn2 = build_assertion_urn(_DATASET_URN, "rule_x")
    assert urn1 == urn2


def test_assertion_urn_changes_with_rule_id():
    """DATAHUB_INTEGRATION.md convention 3: different rule_id produces different URN."""
    urn_a = build_assertion_urn(_DATASET_URN, "rule_a")
    urn_b = build_assertion_urn(_DATASET_URN, "rule_b")
    assert urn_a != urn_b


# ── Group A1 cont.: field metric rule ────────────────────────────────────────


def test_field_metric_rule_produces_field_metric_type():
    """Pattern D anti-pattern regression: field rule with metric= must use FIELD_METRIC type."""
    rule = {
        "rule_id": "r_fm",
        "type": "field",
        "field": "rating_score",
        "metric": "null_count",
        "condition": {"type": "less_than_or_equal_to", "value": 0},
    }
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion is not None
    assert info.fieldAssertion.type == FieldAssertionTypeClass.FIELD_METRIC
    assert info.fieldAssertion.fieldMetricAssertion is not None, (
        "Pattern D anti-pattern: fieldMetricAssertion=None when metric is set"
    )
    # fieldValuesAssertion must be None when FIELD_METRIC is used
    assert info.fieldAssertion.fieldValuesAssertion is None


def test_field_values_rule_produces_field_values_type():
    """field rule without metric= uses FIELD_VALUES (the other branch)."""
    rule = {
        "rule_id": "r_fv",
        "type": "field",
        "field": "price",
        "condition": {"type": "greater_than", "value": 0},
    }
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion is not None
    assert info.fieldAssertion.type == FieldAssertionTypeClass.FIELD_VALUES
    assert info.fieldAssertion.fieldValuesAssertion is not None
    assert info.fieldAssertion.fieldMetricAssertion is None


# ── Group A1 cont.: _FIELD_METRIC_MAP coverage ───────────────────────────────


def test_field_metric_null_count_maps_correctly():
    """Pattern D: null_count → FieldMetricTypeClass.NULL_COUNT."""
    rule = {"rule_id": "r", "type": "field", "field": "col", "metric": "null_count"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion.fieldMetricAssertion.metric == FieldMetricTypeClass.NULL_COUNT


def test_field_metric_unique_count_maps_correctly():
    """Pattern D: unique_count → FieldMetricTypeClass.UNIQUE_COUNT."""
    rule = {"rule_id": "r", "type": "field", "field": "col", "metric": "unique_count"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion.fieldMetricAssertion.metric == FieldMetricTypeClass.UNIQUE_COUNT


def test_field_metric_min_maps_correctly():
    """Pattern D: min → FieldMetricTypeClass.MIN."""
    rule = {"rule_id": "r", "type": "field", "field": "col", "metric": "min"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion.fieldMetricAssertion.metric == FieldMetricTypeClass.MIN


def test_field_metric_max_maps_correctly():
    """Pattern D: max → FieldMetricTypeClass.MAX."""
    rule = {"rule_id": "r", "type": "field", "field": "col", "metric": "max"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.fieldAssertion.fieldMetricAssertion.metric == FieldMetricTypeClass.MAX


# ── Group A1 cont.: custom rule always populates customAssertion ──────────────


def test_custom_rule_populates_custom_assertion_via_fallback():
    """Unknown rule_type falls back to custom evaluator path — customAssertion must not be None."""
    rule = {"rule_id": "r", "type": "unknown_type_xyz"}
    info = build_assertion_info(_DATASET_URN, rule)
    # Falls back to CUSTOM; customAssertion must be populated (not swallowed)
    assert info.type == AssertionTypeClass.CUSTOM
    assert info.customAssertion is not None, (
        "Catch-all must not swallow errors — customAssertion should be populated"
    )


def test_custom_rule_explicit_populates_custom_assertion():
    """Explicit custom type always populates customAssertion."""
    rule = {"rule_id": "r", "type": "custom", "subtype": "sql_timeseries", "sql": "SELECT 1"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.customAssertion is not None
    assert info.customAssertion.type == "sql_timeseries"


# ── Group A1 cont.: build_run_event runId preservation ───────────────────────


def test_build_run_event_run_id_preserved():
    """DATAHUB_INTEGRATION.md convention 5: runId must equal the passed run_id (no UUID regen)."""
    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="workflow-run-abc-123",
        result="SUCCESS",
        values={"row_count": 500},
        partition={},
    )
    assert event.runId == "workflow-run-abc-123", (
        "Anti-pattern: runId=uuid4() regenerated per rule breaks DataHub timeline grouping"
    )


def test_build_run_event_partition_empty_is_full_table():
    """DataHub `AssertionRunEvent.pdl` defaults `partitionSpec` to
    `{type: FULL_TABLE, partition: "FULL_TABLE_SNAPSHOT"}` for non-partitioned
    runs; an empty partition dict must produce that exact shape."""
    from datahub.metadata.schema_classes import PartitionTypeClass

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="run-1",
        result="SUCCESS",
        values={},
        partition={},
    )
    assert event.partitionSpec.type == PartitionTypeClass.FULL_TABLE
    # PartitionSpec.partition is required (non-optional) per DataHub PDL; the
    # documented default sentinel for full-table snapshots is "FULL_TABLE_SNAPSHOT".
    assert event.partitionSpec.partition == "FULL_TABLE_SNAPSHOT"


def test_build_run_event_partition_non_empty_is_partition_type():
    """DATAHUB_INTEGRATION.md convention 7 (partitionSpec): non-empty partition → PARTITION with serialized dict."""
    import ast

    from datahub.metadata.schema_classes import PartitionTypeClass

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    partition = {"load_date": "2025-01-15"}
    event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="run-1",
        result="SUCCESS",
        values={},
        partition=partition,
    )
    assert event.partitionSpec.type == PartitionTypeClass.PARTITION
    assert event.partitionSpec.partition is not None
    # Round-trip: impl serializes via str(dict) (Python repr); verify the input dict is recoverable.
    parsed = ast.literal_eval(event.partitionSpec.partition)
    assert parsed == {"load_date": "2025-01-15"}  # round-trips to input


# ── Group A2: emit contracts ──────────────────────────────────────────────────


async def test_register_assertion_propagates_emit_failure(datahub):
    """DATAHUB_INTEGRATION.md convention 6 + Pattern D anti-pattern: emit failure must propagate (no swallow)."""
    from src.shared.exceptions import DataHubUnavailableError

    datahub.get_assertion_info = AsyncMock(return_value=None)
    datahub.emit_assertion = AsyncMock(side_effect=DataHubUnavailableError("GMS down"))

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    rule = {"rule_id": "r1", "type": "freshness"}
    assertion_info = build_assertion_info(_DATASET_URN, rule)

    with pytest.raises(DataHubUnavailableError):
        await register_assertion(datahub, assertion_urn, assertion_info)


async def test_register_assertion_idempotent_when_already_exists(datahub):
    """Calling register_assertion when the URN already exists must not raise.

    Spec invariant: re-emit on config edit is idempotent (DATAHUB_INTEGRATION
    convention 3). Whether the impl skips the emit or relies on DataHub's
    upsert idempotency is impl detail; only the no-error contract is observable.
    """
    existing = MagicMock()
    datahub.get_assertion_info = AsyncMock(return_value=existing)
    datahub.emit_assertion = AsyncMock()

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    rule = {"rule_id": "r1", "type": "freshness"}
    assertion_info = build_assertion_info(_DATASET_URN, rule)

    # Should not raise:
    await register_assertion(datahub, assertion_urn, assertion_info)
    await register_assertion(datahub, assertion_urn, assertion_info)


async def test_report_result_returns_true_on_success(datahub):
    """DATAHUB_INTEGRATION.md convention 7: report_result returns True on successful emission."""
    datahub.emit_assertion = AsyncMock(return_value=None)

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    run_event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="run-1",
        result="SUCCESS",
        values={},
        partition={},
    )
    result = await report_result(datahub, assertion_urn, run_event)
    assert result is True


async def test_report_result_returns_false_on_failure_no_raise(datahub):
    """DATAHUB_INTEGRATION.md convention 7: run event failures produce ERROR result, not exception propagation."""
    datahub.emit_assertion = AsyncMock(side_effect=RuntimeError("network error"))

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    run_event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="run-1",
        result="SUCCESS",
        values={},
        partition={},
    )
    # Must return False, not raise
    result = await report_result(datahub, assertion_urn, run_event)
    assert result is False


async def test_report_result_logs_warning_on_failure(datahub, caplog):
    """DATAHUB_INTEGRATION.md convention 7: failures are not silent — logged at warning level."""
    import logging

    datahub.emit_assertion = AsyncMock(side_effect=RuntimeError("GMS timeout"))

    assertion_urn = build_assertion_urn(_DATASET_URN, "r1")
    run_event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="run-1",
        result="SUCCESS",
        values={},
        partition={},
    )

    with caplog.at_level(logging.WARNING, logger="src.backend.validation.assertions"):
        await report_result(datahub, assertion_urn, run_event)

    # The warning must have been logged (exact message not pinned — spec says "log warning")
    assert len(caplog.records) > 0
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
