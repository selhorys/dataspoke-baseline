"""Unit tests for validation assertion bridge (mocked DataHub)."""

import time
from unittest.mock import AsyncMock, MagicMock

from datahub.metadata.schema_classes import (
    AssertionResultTypeClass,
    AssertionRunStatusClass,
    AssertionTypeClass,
    PartitionTypeClass,
)

from src.backend.validation.assertions import (
    build_assertion_info,
    build_assertion_urn,
    build_run_event,
    register_assertion,
    report_result,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"
_RULE_ID = "freshness_rule_1"


# ── build_assertion_urn ────────────────────────────────────────────────────────


def test_build_assertion_urn_format():
    """Returned URN must have the urn:li:assertion: prefix."""
    urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    assert urn.startswith("urn:li:assertion:")


def test_build_assertion_urn_deterministic():
    """Same inputs always produce the same URN."""
    urn1 = build_assertion_urn(_DATASET_URN, _RULE_ID)
    urn2 = build_assertion_urn(_DATASET_URN, _RULE_ID)
    assert urn1 == urn2


def test_build_assertion_urn_different_rule_ids():
    """Different rule_ids produce different URNs."""
    urn1 = build_assertion_urn(_DATASET_URN, "rule_a")
    urn2 = build_assertion_urn(_DATASET_URN, "rule_b")
    assert urn1 != urn2


def test_build_assertion_urn_different_dataset_urns():
    """Different dataset URNs produce different assertion URNs."""
    urn1 = build_assertion_urn("urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t1,DEV)", _RULE_ID)
    urn2 = build_assertion_urn("urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t2,DEV)", _RULE_ID)
    assert urn1 != urn2


def test_build_assertion_urn_consistent_across_calls():
    """URN is stable — same result on 10 consecutive calls."""
    urns = [build_assertion_urn(_DATASET_URN, _RULE_ID) for _ in range(10)]
    assert len(set(urns)) == 1


# ── build_assertion_info ───────────────────────────────────────────────────────


def test_build_assertion_info_freshness_type():
    rule = {"rule_id": _RULE_ID, "type": "freshness", "lookback_interval": "24h"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.FRESHNESS


def test_build_assertion_info_volume_type():
    rule = {
        "rule_id": "vol_r1",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 100},
    }
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.VOLUME


def test_build_assertion_info_field_type():
    rule = {
        "rule_id": "field_r1",
        "type": "field",
        "field": "rating_score",
        "metric": "null_proportion",
    }
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.FIELD


def test_build_assertion_info_schema_type():
    rule = {"rule_id": "schema_r1", "type": "schema", "expected_fields": []}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.DATA_SCHEMA


def test_build_assertion_info_sql_type():
    rule = {"rule_id": "sql_r1", "type": "sql", "query": "SELECT 1"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.SQL


def test_build_assertion_info_custom_type():
    rule = {"rule_id": "cust_r1", "type": "custom"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.CUSTOM


def test_build_assertion_info_unknown_type_falls_back_to_custom():
    """Unknown rule type falls back to CUSTOM assertion type."""
    rule = {"rule_id": "unknown_r1", "type": "nonexistent"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.type == AssertionTypeClass.CUSTOM


def test_build_assertion_info_source_type_is_external():
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    from datahub.metadata.schema_classes import AssertionSourceTypeClass
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL


def test_build_assertion_info_custom_properties_contain_rule_id():
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.customProperties["dataspoke_rule_id"] == _RULE_ID


def test_build_assertion_info_custom_properties_contain_rule_type():
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.customProperties["dataspoke_rule_type"] == "freshness"


def test_build_assertion_info_description_uses_provided():
    rule = {"rule_id": _RULE_ID, "type": "freshness", "description": "My custom description"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert info.description == "My custom description"


def test_build_assertion_info_description_defaults_when_absent():
    rule = {"rule_id": _RULE_ID, "type": "volume"}
    info = build_assertion_info(_DATASET_URN, rule)
    assert "volume" in info.description.lower()


# ── build_run_event ────────────────────────────────────────────────────────────


def _make_run_event(
    result: str = "SUCCESS",
    values: dict | None = None,
    partition: dict | None = None,
) -> object:
    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    # Use sentinel: None means "use defaults", empty dict means "empty values"
    resolved_values = {"hours_since_last_update": 2.0} if values is None else values
    return build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="test-run-1",
        result=result,
        values=resolved_values,
        partition=partition if partition is not None else {},
    )


def test_build_run_event_success_result_type():
    event = _make_run_event(result="SUCCESS")
    assert event.result.type == AssertionResultTypeClass.SUCCESS


def test_build_run_event_failure_result_type():
    event = _make_run_event(result="FAILURE")
    assert event.result.type == AssertionResultTypeClass.FAILURE


def test_build_run_event_error_result_type():
    event = _make_run_event(result="ERROR")
    assert event.result.type == AssertionResultTypeClass.ERROR


def test_build_run_event_unknown_result_falls_back_to_error():
    """Unrecognized result string falls back to ERROR."""
    event = _make_run_event(result="UNKNOWN_RESULT")
    assert event.result.type == AssertionResultTypeClass.ERROR


def test_build_run_event_status_is_complete():
    event = _make_run_event()
    assert event.status == AssertionRunStatusClass.COMPLETE


def test_build_run_event_assertion_urn_matches():
    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    event = build_run_event(
        assertion_urn=assertion_urn,
        dataset_urn=_DATASET_URN,
        run_id="run-xyz",
        result="SUCCESS",
        values={},
        partition={},
    )
    assert event.assertionUrn == assertion_urn


def test_build_run_event_assertee_urn_matches_dataset():
    event = _make_run_event()
    assert event.asserteeUrn == _DATASET_URN


def test_build_run_event_run_id_preserved():
    event = build_run_event(
        assertion_urn=build_assertion_urn(_DATASET_URN, _RULE_ID),
        dataset_urn=_DATASET_URN,
        run_id="specific-run-id",
        result="SUCCESS",
        values={},
        partition={},
    )
    assert event.runId == "specific-run-id"


def test_build_run_event_timestamp_is_recent():
    before = int(time.time() * 1000)
    event = _make_run_event()
    after = int(time.time() * 1000)
    assert before <= event.timestampMillis <= after


def test_build_run_event_empty_partition_uses_full_table():
    event = _make_run_event(partition={})
    assert event.partitionSpec.type == PartitionTypeClass.FULL_TABLE
    assert event.partitionSpec.partition is None


def test_build_run_event_non_empty_partition_uses_partition_type():
    event = _make_run_event(partition={"load_date": "2025-03-10"})
    assert event.partitionSpec.type == PartitionTypeClass.PARTITION
    assert event.partitionSpec.partition is not None


def test_build_run_event_values_serialized_as_string_map():
    event = _make_run_event(values={"hours_since_last_update": 2.5, "extra": True})
    assert event.result.nativeResults is not None
    assert event.result.nativeResults["hours_since_last_update"] == "2.5"
    assert event.result.nativeResults["extra"] == "True"


def test_build_run_event_empty_values_produces_no_native_results():
    event = _make_run_event(values={})
    assert event.result.nativeResults is None


# ── register_assertion ─────────────────────────────────────────────────────────


async def test_register_assertion_emits_when_not_exists(datahub):
    """When assertion doesn't exist, emit_assertion is called once."""
    datahub.get_assertion_info = AsyncMock(return_value=None)
    datahub.emit_assertion = AsyncMock()

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    assertion_info = build_assertion_info(_DATASET_URN, rule)

    await register_assertion(datahub, assertion_urn, assertion_info)

    datahub.emit_assertion.assert_awaited_once_with(assertion_urn, assertion_info)


async def test_register_assertion_skips_when_already_exists(datahub):
    """When assertion already exists in DataHub, emit_assertion is NOT called."""
    existing_info = MagicMock()
    datahub.get_assertion_info = AsyncMock(return_value=existing_info)
    datahub.emit_assertion = AsyncMock()

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    assertion_info = build_assertion_info(_DATASET_URN, rule)

    await register_assertion(datahub, assertion_urn, assertion_info)

    datahub.emit_assertion.assert_not_awaited()


async def test_register_assertion_best_effort_on_failure(datahub):
    """DataHub failure during register is swallowed — no exception raised."""
    datahub.get_assertion_info = AsyncMock(side_effect=RuntimeError("GMS unavailable"))
    datahub.emit_assertion = AsyncMock()

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    assertion_info = build_assertion_info(_DATASET_URN, rule)

    # Must not raise
    await register_assertion(datahub, assertion_urn, assertion_info)

    datahub.emit_assertion.assert_not_awaited()


async def test_register_assertion_emit_failure_does_not_raise(datahub):
    """emit_assertion failure is swallowed — best-effort contract."""
    datahub.get_assertion_info = AsyncMock(return_value=None)
    datahub.emit_assertion = AsyncMock(side_effect=RuntimeError("emit failed"))

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    rule = {"rule_id": _RULE_ID, "type": "freshness"}
    assertion_info = build_assertion_info(_DATASET_URN, rule)

    # Must not raise
    await register_assertion(datahub, assertion_urn, assertion_info)


# ── report_result ──────────────────────────────────────────────────────────────


async def test_report_result_calls_emit_assertion(datahub):
    """report_result calls emit_assertion with the run event."""
    datahub.emit_assertion = AsyncMock()

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    run_event = _make_run_event(result="SUCCESS")

    await report_result(datahub, assertion_urn, run_event)

    datahub.emit_assertion.assert_awaited_once_with(assertion_urn, run_event)


async def test_report_result_failure_result(datahub):
    """report_result also works for FAILURE result events."""
    datahub.emit_assertion = AsyncMock()

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    run_event = _make_run_event(result="FAILURE")

    await report_result(datahub, assertion_urn, run_event)

    datahub.emit_assertion.assert_awaited_once()
    call_args = datahub.emit_assertion.call_args
    assert call_args[0][0] == assertion_urn
    assert call_args[0][1].result.type == AssertionResultTypeClass.FAILURE


async def test_report_result_best_effort_on_failure(datahub):
    """DataHub failure during report is swallowed — no exception raised."""
    datahub.emit_assertion = AsyncMock(side_effect=RuntimeError("GMS unavailable"))

    assertion_urn = build_assertion_urn(_DATASET_URN, _RULE_ID)
    run_event = _make_run_event(result="SUCCESS")

    # Must not raise
    await report_result(datahub, assertion_urn, run_event)


async def test_report_result_uses_correct_assertion_urn(datahub):
    """emit_assertion is called with the exact assertion_urn passed in."""
    datahub.emit_assertion = AsyncMock()

    urn_a = build_assertion_urn(_DATASET_URN, "rule_a")
    urn_b = build_assertion_urn(_DATASET_URN, "rule_b")
    run_event = _make_run_event()

    await report_result(datahub, urn_a, run_event)
    await report_result(datahub, urn_b, run_event)

    assert datahub.emit_assertion.await_count == 2
    first_call_urn = datahub.emit_assertion.call_args_list[0][0][0]
    second_call_urn = datahub.emit_assertion.call_args_list[1][0][0]
    assert first_call_urn == urn_a
    assert second_call_urn == urn_b
    assert first_call_urn != second_call_urn
