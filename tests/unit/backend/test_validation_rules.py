"""Unit tests for validation rule evaluators (mocked DataHub)."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.rules import (
    evaluate_condition as _evaluate_condition,
)
from src.backend.validation.rules import (
    evaluate_rule,
)
from src.backend.validation.rules import (
    parse_duration_seconds as _parse_duration_seconds,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"
_PARTITION: dict = {}


# ── _parse_duration_seconds ────────────────────────────────────────────────────


def test_parse_duration_seconds_hours():
    assert _parse_duration_seconds("6h") == 6 * 3600.0


def test_parse_duration_seconds_hours_long():
    assert _parse_duration_seconds("6 hours") == 6 * 3600.0


def test_parse_duration_seconds_one_hour():
    assert _parse_duration_seconds("1 hour") == 3600.0


def test_parse_duration_seconds_minutes():
    assert _parse_duration_seconds("30m") == 30 * 60.0


def test_parse_duration_seconds_minutes_long():
    assert _parse_duration_seconds("30 minutes") == 30 * 60.0


def test_parse_duration_seconds_days():
    assert _parse_duration_seconds("2d") == 2 * 86400.0


def test_parse_duration_seconds_days_long():
    assert _parse_duration_seconds("2 days") == 2 * 86400.0


def test_parse_duration_seconds_one_day():
    assert _parse_duration_seconds("1 day") == 86400.0


def test_parse_duration_seconds_seconds():
    assert _parse_duration_seconds("90s") == 90.0


def test_parse_duration_seconds_seconds_long():
    assert _parse_duration_seconds("90 seconds") == 90.0


def test_parse_duration_seconds_float():
    assert _parse_duration_seconds("1.5h") == 1.5 * 3600.0


def test_parse_duration_seconds_invalid_unit():
    with pytest.raises(ValueError, match="Unknown duration unit"):
        _parse_duration_seconds("5x")


def test_parse_duration_seconds_invalid_format():
    with pytest.raises(ValueError, match="Cannot parse duration"):
        _parse_duration_seconds("bad-value")


# ── _evaluate_condition ────────────────────────────────────────────────────────


def test_evaluate_condition_empty_passes():
    passed, _ = _evaluate_condition(42, {})
    assert passed is True


def test_evaluate_condition_between_in_range():
    passed, msg = _evaluate_condition(50, {"type": "between", "min": 10, "max": 100})
    assert passed is True
    assert msg == ""


def test_evaluate_condition_between_at_min():
    passed, _ = _evaluate_condition(10, {"type": "between", "min": 10, "max": 100})
    assert passed is True


def test_evaluate_condition_between_at_max():
    passed, _ = _evaluate_condition(100, {"type": "between", "min": 10, "max": 100})
    assert passed is True


def test_evaluate_condition_between_out_of_range():
    passed, msg = _evaluate_condition(5, {"type": "between", "min": 10, "max": 100})
    assert passed is False
    assert "not between" in msg


def test_evaluate_condition_less_than_passes():
    passed, _ = _evaluate_condition(5, {"type": "less_than", "value": 10})
    assert passed is True


def test_evaluate_condition_less_than_fails():
    passed, msg = _evaluate_condition(10, {"type": "less_than", "value": 10})
    assert passed is False
    assert "not less than" in msg


def test_evaluate_condition_less_than_alias():
    passed, _ = _evaluate_condition(5, {"type": "lt", "value": 10})
    assert passed is True


def test_evaluate_condition_less_than_or_equal_passes_on_equal():
    passed, _ = _evaluate_condition(10, {"type": "less_than_or_equal", "value": 10})
    assert passed is True


def test_evaluate_condition_less_than_or_equal_alias():
    passed, _ = _evaluate_condition(9, {"type": "lte", "value": 10})
    assert passed is True


def test_evaluate_condition_greater_than_passes():
    passed, _ = _evaluate_condition(20, {"type": "greater_than", "value": 10})
    assert passed is True


def test_evaluate_condition_greater_than_fails():
    passed, msg = _evaluate_condition(10, {"type": "greater_than", "value": 10})
    assert passed is False
    assert "not greater than" in msg


def test_evaluate_condition_greater_than_alias():
    passed, _ = _evaluate_condition(11, {"type": "gt", "value": 10})
    assert passed is True


def test_evaluate_condition_greater_than_or_equal_passes_on_equal():
    passed, _ = _evaluate_condition(10, {"type": "greater_than_or_equal", "value": 10})
    assert passed is True


def test_evaluate_condition_greater_than_or_equal_alias():
    passed, _ = _evaluate_condition(10, {"type": "gte", "value": 10})
    assert passed is True


def test_evaluate_condition_equal_passes():
    passed, _ = _evaluate_condition(42.0, {"type": "equal", "value": 42})
    assert passed is True


def test_evaluate_condition_equal_alias():
    passed, _ = _evaluate_condition(42.0, {"type": "eq", "value": 42})
    assert passed is True


def test_evaluate_condition_equal_fails():
    passed, msg = _evaluate_condition(41.0, {"type": "equal", "value": 42})
    assert passed is False
    assert "does not equal" in msg


def test_evaluate_condition_not_equal_passes():
    passed, _ = _evaluate_condition(41.0, {"type": "not_equal", "value": 42})
    assert passed is True


def test_evaluate_condition_not_equal_alias():
    passed, _ = _evaluate_condition(41.0, {"type": "neq", "value": 42})
    assert passed is True


def test_evaluate_condition_not_equal_fails():
    passed, msg = _evaluate_condition(42.0, {"type": "not_equal", "value": 42})
    assert passed is False
    assert "equals" in msg


def test_evaluate_condition_unknown_type():
    passed, msg = _evaluate_condition(42.0, {"type": "bogus_condition"})
    assert passed is False
    assert "Unknown condition type" in msg


def test_evaluate_condition_non_numeric_value():
    passed, msg = _evaluate_condition("hello", {"type": "greater_than", "value": 10})
    assert passed is False
    assert "non-numeric" in msg


# ── evaluate_rule dispatch ─────────────────────────────────────────────────────


async def test_evaluate_rule_dispatches_freshness(datahub):
    """evaluate_rule with type=freshness dispatches to _evaluate_freshness."""
    now_ms = int(time.time() * 1000)
    op = MagicMock()
    op.lastUpdatedTimestamp = now_ms
    op.timestampMillis = now_ms
    datahub.get_timeseries = AsyncMock(return_value=[op])

    rule = {"rule_id": "fresh_r1", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.rule_id == "fresh_r1"
    assert result.assertion_result == "SUCCESS"
    assert "hours_since_last_update" in result.values


async def test_evaluate_rule_dispatches_volume(datahub):
    """evaluate_rule with type=volume dispatches to _evaluate_volume."""
    profile = MagicMock()
    profile.rowCount = 5000
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "vol_r1",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 100},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.rule_id == "vol_r1"
    assert result.assertion_result == "SUCCESS"
    assert result.values["row_count"] == 5000


async def test_evaluate_rule_dispatches_field(datahub):
    """evaluate_rule with type=field dispatches to _evaluate_field."""
    fp = MagicMock()
    fp.fieldPath = "rating_score"
    fp.nullProportion = 0.05
    profile = MagicMock()
    profile.fieldProfiles = [fp]
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "field_r1",
        "type": "field",
        "field": "rating_score",
        "metric": "null_proportion",
        "condition": {"type": "less_than", "value": 0.1},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.rule_id == "field_r1"
    assert result.assertion_result == "SUCCESS"
    assert result.values["null_proportion"] == pytest.approx(0.05)


async def test_evaluate_rule_dispatches_schema(datahub):
    """evaluate_rule with type=schema dispatches to _evaluate_schema."""
    f1 = MagicMock()
    f1.fieldPath = "order_id"
    f1.nativeDataType = "integer"
    schema = MagicMock()
    schema.fields = [f1]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_r1",
        "type": "schema",
        "expected_fields": [{"name": "order_id", "type": "integer"}],
        "compatibility": "superset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.rule_id == "schema_r1"
    assert result.assertion_result == "SUCCESS"


async def test_evaluate_rule_dispatches_sql(datahub):
    """evaluate_rule with type=sql returns ERROR (not implemented)."""
    rule = {"rule_id": "sql_r1", "type": "sql", "query": "SELECT COUNT(*) FROM orders"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.rule_id == "sql_r1"
    assert result.assertion_result == "ERROR"
    assert any("not yet implemented" in i.get("msg", "") for i in result.issues)


async def test_evaluate_rule_dispatches_custom(datahub):
    """evaluate_rule with unknown/custom type returns ERROR (not implemented)."""
    rule = {"rule_id": "custom_r1", "type": "custom"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.rule_id == "custom_r1"
    assert result.assertion_result == "ERROR"
    assert any("not yet implemented" in i.get("msg", "") for i in result.issues)


async def test_evaluate_rule_unknown_type_falls_back_to_custom(datahub):
    """Unknown rule type falls back to _evaluate_custom (ERROR)."""
    rule = {"rule_id": "weird_r1", "type": "nonexistent_type"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "ERROR"


async def test_evaluate_rule_exception_returns_error(datahub):
    """If the underlying evaluator raises, evaluate_rule returns ERROR gracefully."""
    datahub.get_timeseries = AsyncMock(side_effect=RuntimeError("DataHub down"))

    rule = {"rule_id": "fresh_r2", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "ERROR"
    assert result.rule_id == "fresh_r2"
    assert any("Unexpected error" in i.get("msg", "") for i in result.issues)


# ── _evaluate_freshness ────────────────────────────────────────────────────────


async def test_evaluate_freshness_success_recent_update(datahub):
    """Last update 1 hour ago with 24h lookback → SUCCESS."""
    one_hour_ago_ms = int((time.time() - 3600) * 1000)
    op = MagicMock()
    op.lastUpdatedTimestamp = one_hour_ago_ms
    op.timestampMillis = one_hour_ago_ms
    datahub.get_timeseries = AsyncMock(return_value=[op])

    rule = {"rule_id": "r_fresh", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"
    assert result.values["hours_since_last_update"] < 2.0
    assert result.issues == []


async def test_evaluate_freshness_failure_stale_data(datahub):
    """Last update 50 hours ago with 24h lookback → FAILURE."""
    fifty_hours_ago_ms = int((time.time() - 50 * 3600) * 1000)
    op = MagicMock()
    op.lastUpdatedTimestamp = fifty_hours_ago_ms
    op.timestampMillis = fifty_hours_ago_ms
    datahub.get_timeseries = AsyncMock(return_value=[op])

    rule = {"rule_id": "r_stale", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    assert result.values["hours_since_last_update"] > 48.0
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "freshness_violation" issue type is not spec-anchored


async def test_evaluate_freshness_no_operations(datahub):
    """No OperationClass records → FAILURE with no_data issue."""
    datahub.get_timeseries = AsyncMock(return_value=[])

    rule = {"rule_id": "r_no_data", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    assert result.values["hours_since_last_update"] is None
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "no_data" issue type is not spec-anchored


async def test_evaluate_freshness_operation_no_timestamp(datahub):
    """OperationClass with no timestamp → FAILURE with missing_timestamp issue."""
    op = MagicMock()
    op.lastUpdatedTimestamp = None
    op.timestampMillis = None
    datahub.get_timeseries = AsyncMock(return_value=[op])

    rule = {"rule_id": "r_no_ts", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "missing_timestamp" issue type is not spec-anchored


async def test_evaluate_freshness_uses_fallback_on_bad_interval(datahub):
    """Unparseable lookback_interval falls back to 24h default."""
    one_hour_ago_ms = int((time.time() - 3600) * 1000)
    op = MagicMock()
    op.lastUpdatedTimestamp = one_hour_ago_ms
    op.timestampMillis = one_hour_ago_ms
    datahub.get_timeseries = AsyncMock(return_value=[op])

    rule = {"rule_id": "r_bad_interval", "type": "freshness", "lookback_interval": "bad-value"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    # 1h ago is within 24h default → SUCCESS
    assert result.assertion_result == "SUCCESS"


async def test_evaluate_freshness_partition_propagated(datahub):
    """Partition dict is passed through to the RuleEvaluation."""
    now_ms = int(time.time() * 1000)
    op = MagicMock()
    op.lastUpdatedTimestamp = now_ms
    op.timestampMillis = now_ms
    datahub.get_timeseries = AsyncMock(return_value=[op])

    partition = {"load_date": "2025-03-10"}
    rule = {"rule_id": "r_part", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, partition)

    assert result.partition == partition


# ── _evaluate_volume ───────────────────────────────────────────────────────────


async def test_evaluate_volume_success_greater_than(datahub):
    """rowCount=5000 > 100 → SUCCESS."""
    profile = MagicMock()
    profile.rowCount = 5000
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "vol_r",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 100},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"
    assert result.values["row_count"] == 5000
    assert result.issues == []


async def test_evaluate_volume_failure_less_than_threshold(datahub):
    """rowCount=50 but condition requires > 100 → FAILURE."""
    profile = MagicMock()
    profile.rowCount = 50
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "vol_low",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 100},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "volume_violation" issue type is not spec-anchored;
    # spec only requires "issues" list with msg — row_count key is impl-internal
    assert result.issues[0]["row_count"] == 50


async def test_evaluate_volume_success_between(datahub):
    """rowCount within [100, 10000] → SUCCESS."""
    profile = MagicMock()
    profile.rowCount = 500
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "vol_between",
        "type": "volume",
        "condition": {"type": "between", "min": 100, "max": 10000},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"


async def test_evaluate_volume_no_profile(datahub):
    """No DatasetProfileClass records → FAILURE with no_data issue."""
    datahub.get_timeseries = AsyncMock(return_value=[])

    rule = {
        "rule_id": "vol_nodata",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 0},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    assert result.values["row_count"] is None
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "no_data" issue type is not spec-anchored


async def test_evaluate_volume_missing_row_count(datahub):
    """Profile exists but rowCount is None → FAILURE with at least one issue."""
    profile = MagicMock()
    profile.rowCount = None
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "vol_null_count",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 0},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "missing_metric" issue type is not spec-anchored


async def test_evaluate_volume_no_condition_passes(datahub):
    """Empty condition always passes."""
    profile = MagicMock()
    profile.rowCount = 1
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {"rule_id": "vol_no_cond", "type": "volume", "condition": {}}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"


# ── _evaluate_field ────────────────────────────────────────────────────────────


def _make_field_profile(field_path: str, **metrics) -> MagicMock:
    fp = MagicMock()
    fp.fieldPath = field_path
    for attr, val in metrics.items():
        setattr(fp, attr, val)
    return fp


async def test_evaluate_field_null_proportion_success(datahub):
    """Field null_proportion=0.05 < 0.1 threshold → SUCCESS."""
    fp = _make_field_profile("rating_score", nullProportion=0.05)
    profile = MagicMock()
    profile.fieldProfiles = [fp]
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "field_null",
        "type": "field",
        "field": "rating_score",
        "metric": "null_proportion",
        "condition": {"type": "less_than", "value": 0.1},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"
    assert result.values["null_proportion"] == pytest.approx(0.05)
    assert result.values["field"] == "rating_score"


async def test_evaluate_field_null_proportion_failure(datahub):
    """Field null_proportion=0.35 exceeds 0.1 threshold → FAILURE."""
    fp = _make_field_profile("rating_score_legacy", nullProportion=0.35)
    profile = MagicMock()
    profile.fieldProfiles = [fp]
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "field_null_fail",
        "type": "field",
        "field": "rating_score_legacy",
        "metric": "null_proportion",
        "condition": {"type": "less_than", "value": 0.1},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "field_violation" issue type is not spec-anchored;
    # spec only requires "issues" list with msg — field/metric keys are impl-internal
    assert result.issues[0]["field"] == "rating_score_legacy"
    assert result.issues[0]["metric"] == "null_proportion"


async def test_evaluate_field_field_not_found(datahub):
    """Requested field not in profile → FAILURE with field_not_found issue."""
    fp = _make_field_profile("other_field", nullProportion=0.0)
    profile = MagicMock()
    profile.fieldProfiles = [fp]
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "field_missing",
        "type": "field",
        "field": "nonexistent_field",
        "metric": "null_proportion",
        "condition": {"type": "less_than", "value": 0.1},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "field_not_found" issue type is not spec-anchored;
    # spec only requires "issues" list with msg — field key is impl-internal
    assert result.issues[0]["field"] == "nonexistent_field"


async def test_evaluate_field_metric_not_available(datahub):
    """Metric attribute is None on the field profile → FAILURE with missing_metric."""
    fp = _make_field_profile("isbn", nullCount=None)
    fp.nullCount = None  # explicitly None
    profile = MagicMock()
    profile.fieldProfiles = [fp]
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "field_metric_missing",
        "type": "field",
        "field": "isbn",
        "metric": "null_count",
        "condition": {"type": "less_than", "value": 10},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "missing_metric" issue type is not spec-anchored


async def test_evaluate_field_no_profile(datahub):
    """No profile records → FAILURE with no_data issue."""
    datahub.get_timeseries = AsyncMock(return_value=[])

    rule = {
        "rule_id": "field_nodata",
        "type": "field",
        "field": "rating_score",
        "metric": "null_proportion",
        "condition": {"type": "less_than", "value": 0.1},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "no_data" issue type is not spec-anchored


async def test_evaluate_field_metric_map_covers_standard_metrics(datahub):
    """Verify unique_count metric mapping works correctly."""
    fp = _make_field_profile("order_id", uniqueCount=100)
    profile = MagicMock()
    profile.fieldProfiles = [fp]
    datahub.get_timeseries = AsyncMock(return_value=[profile])

    rule = {
        "rule_id": "field_unique",
        "type": "field",
        "field": "order_id",
        "metric": "unique_count",
        "condition": {"type": "greater_than", "value": 50},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"
    assert result.values["unique_count"] == 100.0


# ── _evaluate_schema ───────────────────────────────────────────────────────────


def _make_schema_field(field_path: str, native_type: str = "") -> MagicMock:
    f = MagicMock()
    f.fieldPath = field_path
    f.nativeDataType = native_type
    return f


async def test_evaluate_schema_superset_success(datahub):
    """Actual schema contains all expected fields → SUCCESS with superset mode."""
    schema = MagicMock()
    schema.fields = [
        _make_schema_field("order_id", "integer"),
        _make_schema_field("customer_id", "integer"),
        _make_schema_field("total_amount", "decimal"),
    ]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_sup",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"},
            {"name": "customer_id", "type": "integer"},
        ],
        "compatibility": "superset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"
    assert result.issues == []


async def test_evaluate_schema_superset_failure_missing_field(datahub):
    """Required field missing from actual schema → FAILURE with missing_fields."""
    schema = MagicMock()
    schema.fields = [_make_schema_field("order_id", "integer")]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_sup_fail",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"},
            {"name": "missing_field", "type": "varchar"},
        ],
        "compatibility": "superset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "missing_fields" issue type is not spec-anchored


async def test_evaluate_schema_exact_match_success(datahub):
    """Actual schema exactly matches expected → SUCCESS."""
    schema = MagicMock()
    schema.fields = [
        _make_schema_field("order_id", "integer"),
        _make_schema_field("status", "varchar"),
    ]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_exact",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"},
            {"name": "status", "type": "varchar"},
        ],
        "compatibility": "exact_match",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"


async def test_evaluate_schema_exact_match_failure_extra_field(datahub):
    """Actual schema has extra fields not in expected → FAILURE with exact_match."""
    schema = MagicMock()
    schema.fields = [
        _make_schema_field("order_id", "integer"),
        _make_schema_field("status", "varchar"),
        _make_schema_field("extra_column", "text"),
    ]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_exact_extra",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"},
            {"name": "status", "type": "varchar"},
        ],
        "compatibility": "exact_match",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "extra_fields" issue type is not spec-anchored


async def test_evaluate_schema_subset_success(datahub):
    """All actual fields are within expected → SUCCESS with subset mode."""
    schema = MagicMock()
    schema.fields = [_make_schema_field("order_id", "integer")]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_sub",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"},
            {"name": "status", "type": "varchar"},
            {"name": "total", "type": "decimal"},
        ],
        "compatibility": "subset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "SUCCESS"


async def test_evaluate_schema_subset_failure_extra_field(datahub):
    """Actual schema has field not in expected → FAILURE with subset mode."""
    schema = MagicMock()
    schema.fields = [
        _make_schema_field("order_id", "integer"),
        _make_schema_field("unexpected_column", "text"),
    ]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_sub_fail",
        "type": "schema",
        "expected_fields": [{"name": "order_id", "type": "integer"}],
        "compatibility": "subset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "extra_fields" issue type is not spec-anchored


async def test_evaluate_schema_type_mismatch(datahub):
    """Field exists but type doesn't match → FAILURE with type_mismatch."""
    schema = MagicMock()
    schema.fields = [_make_schema_field("order_id", "varchar")]  # actual: varchar
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_type",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"}  # expected: integer
        ],
        "compatibility": "exact_match",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "type_mismatch" issue type is not spec-anchored;
    # spec only requires "issues" list with msg — expected/actual keys are impl-internal
    mismatch = next(
        (i for i in result.issues if i.get("field") == "order_id"), None
    )
    assert mismatch is not None
    assert mismatch["expected"] == "integer"
    assert mismatch["actual"] == "varchar"


async def test_evaluate_schema_no_schema_metadata(datahub):
    """No SchemaMetadataClass aspect → FAILURE with no_schema issue."""
    datahub.get_aspect = AsyncMock(return_value=None)

    rule = {
        "rule_id": "schema_no_meta",
        "type": "schema",
        "expected_fields": [{"name": "order_id"}],
        "compatibility": "superset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "FAILURE"
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "no_schema" issue type is not spec-anchored


async def test_evaluate_schema_values_contain_field_counts(datahub):
    """RuleEvaluation.values contains actual/expected/missing/extra counts."""
    schema = MagicMock()
    schema.fields = [
        _make_schema_field("order_id", "integer"),
        _make_schema_field("extra_field", "text"),
    ]
    datahub.get_aspect = AsyncMock(return_value=schema)

    rule = {
        "rule_id": "schema_counts",
        "type": "schema",
        "expected_fields": [
            {"name": "order_id", "type": "integer"},
            {"name": "missing_field", "type": "varchar"},
        ],
        "compatibility": "superset",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert "actual_field_count" in result.values
    assert "expected_field_count" in result.values
    assert "missing_field_count" in result.values
    assert result.values["actual_field_count"] == 2
    assert result.values["expected_field_count"] == 2
    assert result.values["missing_field_count"] == 1


# ── _evaluate_sql and _evaluate_custom stubs ───────────────────────────────────


async def test_evaluate_sql_returns_error_not_implemented(datahub):
    """SQL evaluator stub returns ERROR with not_implemented issue."""
    rule = {
        "rule_id": "sql_stub",
        "type": "sql",
        "query": "SELECT COUNT(*) FROM orders WHERE status = 'pending'",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "ERROR"
    assert result.values == {}
    assert result.validation is None
    assert len(result.issues) == 1
    assert result.issues[0]["type"] == "not_implemented"
    assert "not yet implemented" in result.issues[0]["msg"]


async def test_evaluate_custom_returns_error_not_implemented(datahub):
    """Custom evaluator stub returns ERROR with not_implemented issue."""
    rule = {"rule_id": "custom_stub", "type": "custom"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "ERROR"
    assert result.values == {}
    assert result.validation is None
    assert len(result.issues) == 1
    assert result.issues[0]["type"] == "not_implemented"
    assert "not yet implemented" in result.issues[0]["msg"]


# ── _evaluate_sql with db ──────────────────────────────────────────────────────


async def test_evaluate_sql_with_db_success(datahub):
    """SQL evaluator with db + statement: mocked execute_sql returns scalar, condition passes."""
    db = AsyncMock()

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("POSTGRESQL", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=[{"result": 42}]),
        ),
    ):
        rule = {
            "rule_id": "sql_live",
            "type": "sql",
            "statement": "SELECT COUNT(*) AS result FROM orders.order_items",
            "condition": {"type": "greater_than", "value": 10},
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.rule_id == "sql_live"
    assert result.assertion_result == "SUCCESS"
    assert result.values["result"] == 42


async def test_evaluate_sql_with_db_condition_fails(datahub):
    """SQL evaluator with db: scalar result fails condition → FAILURE."""
    db = AsyncMock()

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("POSTGRESQL", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=[{"result": 5}]),
        ),
    ):
        rule = {
            "rule_id": "sql_fail",
            "type": "sql",
            "statement": "SELECT COUNT(*) AS result FROM orders.order_items",
            "condition": {"type": "greater_than", "value": 100},
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.assertion_result == "FAILURE"
    assert result.values["result"] == 5
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "sql_condition_violation" issue type is not spec-anchored


async def test_evaluate_sql_with_db_no_rows_returns_error(datahub):
    """SQL evaluator with db: empty result set → ERROR with no_data issue."""
    db = AsyncMock()

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("POSTGRESQL", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=[]),
        ),
    ):
        rule = {
            "rule_id": "sql_no_rows",
            "type": "sql",
            "statement": "SELECT COUNT(*) FROM nonexistent",
            "condition": {"type": "greater_than", "value": 0},
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.assertion_result == "ERROR"
    # ERROR results must have at least one issue
    assert isinstance(result.issues, list) and len(result.issues) > 0
    # impl-internal taxonomy: "no_data" issue type is not spec-anchored


async def test_evaluate_sql_without_db_still_returns_not_implemented(datahub):
    """SQL evaluator with statement but no db → ERROR not_implemented (stub path)."""
    rule = {
        "rule_id": "sql_no_db",
        "type": "sql",
        "statement": "SELECT 1",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "ERROR"
    assert result.issues[0]["type"] == "not_implemented"


# ── _evaluate_custom with sql_timeseries subtype ───────────────────────────────


async def test_evaluate_custom_sql_timeseries_success_no_ml(datahub):
    """Custom sql_timeseries: execute_timeseries_sql succeeds, no ml_validation → SUCCESS."""
    db = AsyncMock()

    with patch(
        "src.backend.validation.timeseries.execute_timeseries_sql",
        new=AsyncMock(
            return_value={
                "partitions": {"load_date": "2025-03-11"},
                "values": {"row_count": 1200},
            }
        ),
    ):
        rule = {
            "rule_id": "ts_r1",
            "type": "custom",
            "subtype": "sql_timeseries",
            "sql": "SELECT load_date, COUNT(*) AS row_count FROM orders GROUP BY 1",
            "order": ["load_date"],
            "partition": ["load_date"],
            "values": ["row_count"],
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.rule_id == "ts_r1"
    assert result.assertion_result == "SUCCESS"
    assert result.values == {"row_count": 1200}
    assert result.partition == {"load_date": "2025-03-11"}
    assert result.validation is None


async def test_evaluate_custom_sql_timeseries_with_ml_all_pass(datahub):
    """Custom sql_timeseries + ml_validation: all targets pass → SUCCESS."""
    db = AsyncMock()

    with (
        patch(
            "src.backend.validation.timeseries.execute_timeseries_sql",
            new=AsyncMock(
                return_value={
                    "partitions": {"load_date": "2025-03-11"},
                    "values": {"row_count": 1200},
                }
            ),
        ),
        patch(
            "src.backend.validation.ml_validation.validate_values",
            new=AsyncMock(return_value={"row_count": True}),
        ),
    ):
        rule = {
            "rule_id": "ts_ml_r1",
            "type": "custom",
            "subtype": "sql_timeseries",
            "sql": "SELECT load_date, COUNT(*) AS row_count FROM orders GROUP BY 1",
            "order": ["load_date"],
            "partition": ["load_date"],
            "values": ["row_count"],
            "ml_validation": {"targets": ["row_count"], "model": "range"},
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.assertion_result == "SUCCESS"
    assert result.validation == {"row_count": True}
    assert result.issues == []


async def test_evaluate_custom_sql_timeseries_with_ml_some_fail(datahub):
    """Custom sql_timeseries + ml_validation: one target fails → FAILURE."""
    db = AsyncMock()

    with (
        patch(
            "src.backend.validation.timeseries.execute_timeseries_sql",
            new=AsyncMock(
                return_value={
                    "partitions": {"load_date": "2025-03-11"},
                    "values": {"row_count": 9999},
                }
            ),
        ),
        patch(
            "src.backend.validation.ml_validation.validate_values",
            new=AsyncMock(return_value={"row_count": False}),
        ),
    ):
        rule = {
            "rule_id": "ts_ml_fail",
            "type": "custom",
            "subtype": "sql_timeseries",
            "sql": "SELECT load_date, COUNT(*) AS row_count FROM orders GROUP BY 1",
            "order": ["load_date"],
            "partition": ["load_date"],
            "values": ["row_count"],
            "ml_validation": {"targets": ["row_count"], "model": "range"},
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.assertion_result == "FAILURE"
    assert result.validation == {"row_count": False}
    # FAILURE must emit at least one issue with non-empty msg
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert all(i.get("msg") for i in result.issues)
    # impl-internal taxonomy: "ml_validation_failure" issue type is not spec-anchored;
    # spec only requires "issues" list with msg — failed_targets key is impl-internal
    ml_issue = next(
        (i for i in result.issues if "failed_targets" in i), None
    )
    assert ml_issue is not None, "Expected an issue containing failed_targets"
    assert "row_count" in ml_issue["failed_targets"]


async def test_evaluate_custom_unknown_subtype_returns_not_implemented(datahub):
    """Custom rule with unknown subtype → ERROR not_implemented."""
    db = AsyncMock()

    rule = {
        "rule_id": "custom_unknown",
        "type": "custom",
        "subtype": "proprietary_engine",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.assertion_result == "ERROR"
    assert result.issues[0]["type"] == "not_implemented"
    assert "not yet implemented" in result.issues[0]["msg"]


async def test_evaluate_custom_sql_timeseries_without_db_returns_not_implemented(datahub):
    """Custom sql_timeseries without db → ERROR not_implemented (no db guard)."""
    rule = {
        "rule_id": "ts_no_db",
        "type": "custom",
        "subtype": "sql_timeseries",
        "sql": "SELECT 1",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION)

    assert result.assertion_result == "ERROR"
    assert result.issues[0]["type"] == "not_implemented"


async def test_evaluate_custom_sql_timeseries_execute_raises_returns_error(datahub):
    """execute_timeseries_sql raising an exception → ERROR with source_error issue."""
    db = AsyncMock()

    with patch(
        "src.backend.validation.timeseries.execute_timeseries_sql",
        new=AsyncMock(side_effect=RuntimeError("source connection refused")),
    ):
        rule = {
            "rule_id": "ts_exec_fail",
            "type": "custom",
            "subtype": "sql_timeseries",
            "sql": "SELECT load_date, row_count FROM summary",
            "order": ["load_date"],
            "partition": ["load_date"],
            "values": ["row_count"],
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    assert result.assertion_result == "ERROR"
    # ERROR results must have at least one issue containing the original error message
    assert isinstance(result.issues, list) and len(result.issues) > 0
    assert any("source connection refused" in i.get("msg", "") for i in result.issues)
    # impl-internal taxonomy: "source_error" issue type is not spec-anchored


async def test_evaluate_custom_sql_timeseries_ml_exception_swallowed_returns_success(datahub):
    """ml_validation raising an exception is silently swallowed → assertion still SUCCESS."""
    db = AsyncMock()

    with (
        patch(
            "src.backend.validation.timeseries.execute_timeseries_sql",
            new=AsyncMock(
                return_value={
                    "partitions": {"load_date": "2025-03-11"},
                    "values": {"row_count": 1000},
                }
            ),
        ),
        patch(
            "src.backend.validation.ml_validation.validate_values",
            new=AsyncMock(side_effect=RuntimeError("ML service unavailable")),
        ),
    ):
        rule = {
            "rule_id": "ts_ml_exc",
            "type": "custom",
            "subtype": "sql_timeseries",
            "sql": "SELECT load_date, COUNT(*) AS row_count FROM orders GROUP BY 1",
            "order": ["load_date"],
            "partition": ["load_date"],
            "values": ["row_count"],
            "ml_validation": {"targets": ["row_count"], "model": "range"},
        }
        result = await evaluate_rule(datahub, _DATASET_URN, rule, _PARTITION, db=db)

    # ML exception is swallowed; validation is None and result is SUCCESS
    assert result.assertion_result == "SUCCESS"
    assert result.validation is None
    assert result.issues == []
