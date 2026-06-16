"""Pure builder tests for src/backend/validation/assertions.py.

No DB, no DataHub — only inspects the constructed aspect objects.

spec: VALIDATION.md §DataHub Aspect Mapping
spec: VALIDATION.md §Assertion URN
"""

import time
from datetime import UTC, datetime

from datahub.metadata.schema_classes import (
    AssertionResultTypeClass,
    AssertionRunStatusClass,
    AssertionSourceTypeClass,
    AssertionTypeClass,
)

from src.backend.validation.assertions import (
    build_assertion_info,
    build_assertion_urn,
    build_run_event,
)

# ── build_assertion_urn ───────────────────────────────────────────────────────


class TestBuildAssertionUrn:
    def test_same_dataset_urn_produces_same_assertion_urn(self) -> None:
        # spec: VALIDATION.md §Assertion URN — deterministic; recomputable from urn
        urn = (
            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "example_db.orders.daily_fulfillment_summary,DEV)"
        )
        assert build_assertion_urn(urn) == build_assertion_urn(urn)

    def test_different_dataset_urns_produce_different_assertion_urns(self) -> None:
        # spec: VALIDATION.md §Assertion URN — uniqueness: different datasets → different URNs
        urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tableA,DEV)"
        urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tableB,DEV)"
        assert build_assertion_urn(urn_a) != build_assertion_urn(urn_b)

    def test_assertion_urn_format(self) -> None:
        # spec: VALIDATION.md §Assertion URN — starts with urn:li:assertion: followed by a GUID.
        # We do NOT pin the exact hash format (e.g. MD5 32-hex) here — that would tie the
        # test to datahub_guid's internal implementation. Determinism is already covered by
        # test_same_dataset_urn_produces_same_assertion_urn.
        urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,DEV)"
        assertion_urn = build_assertion_urn(urn)
        assert assertion_urn.startswith("urn:li:assertion:"), (
            f"URN must start with urn:li:assertion:, got: {assertion_urn!r}"
        )
        assert len(assertion_urn) > len("urn:li:assertion:"), (
            f"URN has no GUID suffix: {assertion_urn!r}"
        )


# ── build_assertion_info ──────────────────────────────────────────────────────


_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
# Variables are {name, description} objects; build_assertion_info joins NAMES only.
_VARIABLES = [
    {"name": "row_cnt", "description": "Daily row count"},
    {"name": "col1_mean", "description": "Mean of col1"},
    {"name": "col2_null_cnt", "description": ""},
]
_DESCRIPTION = "Daily row count plus key column means and null counts"


class TestBuildAssertionInfo:
    def setup_method(self) -> None:
        self.info = build_assertion_info(_DATASET_URN, _DESCRIPTION, _VARIABLES)

    def test_type_is_custom(self) -> None:
        # spec: VALIDATION.md §assertionInfo — type: CUSTOM
        assert self.info.type == AssertionTypeClass.CUSTOM

    def test_source_type_is_external(self) -> None:
        # spec: VALIDATION.md §assertionInfo — source.type: EXTERNAL
        assert self.info.source is not None
        assert self.info.source.type == AssertionSourceTypeClass.EXTERNAL

    def test_custom_assertion_type(self) -> None:
        # spec: VALIDATION.md §assertionInfo — customAssertion.type = "DATASPOKE_VALIDATION"
        assert self.info.customAssertion is not None
        assert self.info.customAssertion.type == "DATASPOKE_VALIDATION"

    def test_custom_assertion_entity(self) -> None:
        # spec: VALIDATION.md §assertionInfo — customAssertion.entity = dataset_urn
        assert self.info.customAssertion.entity == _DATASET_URN

    def test_custom_assertion_logic_is_comma_joined_variables(self) -> None:
        # spec: VALIDATION.md §assertionInfo — customAssertion.logic = ", ".join(variables)
        expected = "row_cnt, col1_mean, col2_null_cnt"
        assert self.info.customAssertion.logic == expected

    def test_description_is_propagated(self) -> None:
        # spec: VALIDATION.md §assertionInfo — description = conf.description
        assert self.info.description == _DESCRIPTION

    def test_last_updated_actor_is_dataspoke(self) -> None:
        # spec: VALIDATION.md §assertionInfo — lastUpdated.actor = urn:li:corpuser:dataspoke
        assert self.info.lastUpdated is not None
        assert self.info.lastUpdated.actor == "urn:li:corpuser:dataspoke"

    def test_last_updated_time_is_recent(self) -> None:
        # spec: VALIDATION.md §assertionInfo — lastUpdated.time is recent (≤ 5s ago)
        now_ms = int(time.time() * 1000)
        assert self.info.lastUpdated.time is not None
        age_ms = now_ms - self.info.lastUpdated.time
        assert 0 <= age_ms <= 5000, f"lastUpdated.time is {age_ms}ms old"


# ── build_run_event ───────────────────────────────────────────────────────────


class TestBuildRunEvent:
    _ASSERTION_URN = "urn:li:assertion:abc123" + "0" * 26

    def _build(
        self,
        *,
        score: float,
        data_time: datetime,
        variables: dict | None = None,
    ):
        return build_run_event(
            assertion_urn=self._ASSERTION_URN,
            dataset_urn=_DATASET_URN,
            data_time=data_time,
            score=score,
            variables=variables or {"row_cnt": 50.0, "col1_mean": 31.1, "col2_null_cnt": 15.0},
        )

    def test_timestamp_millis_uses_data_time_not_server_now(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — timestampMillis = data_time epoch ms
        # Use a data_time from a past year: its epoch_ms is far smaller than current epoch.
        past_time = datetime(2020, 1, 1, tzinfo=UTC)
        event = self._build(score=1.0, data_time=past_time)
        expected_ms = int(past_time.timestamp() * 1000)
        assert event.timestampMillis == expected_ms, (
            f"timestampMillis {event.timestampMillis} != expected {expected_ms}; "
            "must use data_time, not server now()"
        )
        # Also verify it's far in the past (not server now)
        current_ms = int(time.time() * 1000)
        assert event.timestampMillis < current_ms - 100_000_000, (
            "timestampMillis looks like server now, not data_time"
        )

    def test_score_1_0_produces_success_result(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — result.type = SUCCESS if score == 1.0
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=1.0, data_time=data_time)
        assert event.result is not None
        assert event.result.type == AssertionResultTypeClass.SUCCESS

    def test_score_0_5_produces_failure_result(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — result.type = FAILURE if score != 1.0
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=0.5, data_time=data_time)
        assert event.result.type == AssertionResultTypeClass.FAILURE

    def test_score_0_0_produces_failure_result(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — result.type = FAILURE if score != 1.0
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=0.0, data_time=data_time)
        assert event.result.type == AssertionResultTypeClass.FAILURE

    def test_score_just_under_one_produces_failure_result(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — result.type = SUCCESS iff score == 1.0.
        # Locks in the strict-equality contract: even 0.9999999999 is FAILURE.
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=0.9999999999, data_time=data_time)
        assert event.result.type == AssertionResultTypeClass.FAILURE, (
            "score 0.9999999999 must produce FAILURE; SUCCESS requires score == 1.0 exactly"
        )

    def test_actual_agg_value_equals_score(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — result.actualAggValue = score
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=0.75, data_time=data_time)
        assert event.result.actualAggValue == 0.75

    def test_native_results_contains_all_variables_plus_score(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — nativeResults includes each variable + "score"
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        vars_in = {"row_cnt": 50.0, "col1_mean": 31.1}
        event = build_run_event(
            assertion_urn=self._ASSERTION_URN,
            dataset_urn=_DATASET_URN,
            data_time=data_time,
            score=0.9,
            variables=vars_in,
        )
        assert event.result is not None
        native = event.result.nativeResults
        assert native is not None
        assert "row_cnt" in native
        assert "col1_mean" in native
        assert "score" in native

    def test_native_results_values_are_repr_float_strings(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — nativeResults values = repr(float(...))
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = build_run_event(
            assertion_urn=self._ASSERTION_URN,
            dataset_urn=_DATASET_URN,
            data_time=data_time,
            score=1.0,
            variables={"row_cnt": 50.0},
        )
        native = event.result.nativeResults
        assert native["row_cnt"] == repr(50.0)
        assert native["score"] == repr(1.0)

    def test_runtime_context_ingestion_time_is_recent_epoch_ms_string(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — runtimeContext["ingestion_time"] is server now
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=1.0, data_time=data_time)
        assert event.runtimeContext is not None
        ingestion_time_str = event.runtimeContext.get("ingestion_time")
        assert ingestion_time_str is not None
        ingestion_ms = int(ingestion_time_str)
        current_ms = int(time.time() * 1000)
        assert abs(current_ms - ingestion_ms) <= 5000, (
            f"ingestion_time {ingestion_ms} is not recent (delta={current_ms - ingestion_ms}ms)"
        )

    def test_status_is_complete(self) -> None:
        # spec: VALIDATION.md §assertionRunEvent — status = COMPLETE
        data_time = datetime(2026, 5, 1, tzinfo=UTC)
        event = self._build(score=1.0, data_time=data_time)
        assert event.status == AssertionRunStatusClass.COMPLETE
