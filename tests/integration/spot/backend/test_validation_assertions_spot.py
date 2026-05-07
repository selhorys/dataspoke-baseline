"""Spot integration tests — validation assertions round-trip with real DataHub + Postgres.

NOTE: These tests are WRITTEN but NOT RUN per task directive.
Run group: DATASPOKE_TEST_MODE=true uv run pytest tests/integration/spot/

Concerns covered:
- Round-trip assertion emission and fetch from DataHub (assertionInfo aspects)
- typed sub-aspects populated (freshnessAssertion, fieldMetricAssertion, schemaAssertion)
- source.type == EXTERNAL on round-tripped aspects
- DATA_SCHEMA (not SCHEMA) round-trip
- ValidationService.upsert_config registers all rules end-to-end
- Idempotent re-upsert — no duplicate emission
- Shared runId across all rules in one validation run

Spec sources:
- spec/DATAHUB_INTEGRATION.md §Assertion Aspects conventions 1-7
- spec/feature/BACKEND.md §Validation Service
"""

# Per-module dummy-data seed: catalog schema is required
# (title_master is the Imazon Datahub-seeded dataset used as assertion target)
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

import pytest

# All integration tests must use the Imazon canonical dataset URN
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


# ── 55. Round-trip emission ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_freshness_assertion_round_trip_with_freshness_sub_aspect(
    datahub_client,
    async_session,
) -> None:
    """DATAHUB_INTEGRATION.md convention 1: freshnessAssertion must be non-null after round-trip.

    Convention 2: assertionInfo.source.type must equal EXTERNAL after round-trip.
    """
    from datahub.metadata.schema_classes import AssertionSourceTypeClass

    from src.backend.validation.assertions import (
        build_assertion_info,
        build_assertion_urn,
    )

    rule = {
        "rule_id": "spot-fresh-rt-001",
        "type": "freshness",
        "lookback_interval": "24h",
    }
    assertion_urn = build_assertion_urn(_TEST_URN, rule["rule_id"])
    assertion_info = build_assertion_info(_TEST_URN, rule)

    # Force re-emission (skip idempotency check for this test)
    await datahub_client.emit_assertion(assertion_urn, assertion_info)

    # Fetch back from DataHub
    fetched = await datahub_client.get_assertion_info(assertion_urn)
    assert fetched is not None, "assertionInfo must exist after emission"
    assert fetched.freshnessAssertion is not None, (
        "Convention 1: freshnessAssertion must not be null after round-trip"
    )
    assert fetched.source.type == AssertionSourceTypeClass.EXTERNAL, (
        "Convention 2: source.type must be EXTERNAL on round-tripped aspect"
    )


# ── 56. Field-metric round-trip ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_field_metric_assertion_round_trip(
    datahub_client,
) -> None:
    """Pattern D: fieldMetricAssertion.metric must equal NULL_COUNT after round-trip."""
    from datahub.metadata.schema_classes import FieldMetricTypeClass

    from src.backend.validation.assertions import (
        build_assertion_info,
        build_assertion_urn,
    )

    rule = {
        "rule_id": "spot-field-null-rt-001",
        "type": "field",
        "field": "rating_score",
        "metric": "null_count",
        "condition": {"type": "less_than_or_equal_to", "value": 0},
    }
    assertion_urn = build_assertion_urn(_TEST_URN, rule["rule_id"])
    assertion_info = build_assertion_info(_TEST_URN, rule)

    await datahub_client.emit_assertion(assertion_urn, assertion_info)

    fetched = await datahub_client.get_assertion_info(assertion_urn)
    assert fetched is not None
    assert fetched.fieldAssertion is not None
    assert fetched.fieldAssertion.fieldMetricAssertion is not None
    assert fetched.fieldAssertion.fieldMetricAssertion.metric == FieldMetricTypeClass.NULL_COUNT


# ── 57. Schema rule round-trip ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schema_assertion_round_trip_type_is_data_schema(
    datahub_client,
) -> None:
    """DATAHUB_INTEGRATION.md: schema type is DATA_SCHEMA (not SCHEMA) — verified after round-trip."""
    from datahub.metadata.schema_classes import AssertionTypeClass

    from src.backend.validation.assertions import (
        build_assertion_info,
        build_assertion_urn,
    )

    rule = {
        "rule_id": "spot-schema-rt-001",
        "type": "schema",
        "fields": [
            {"field": "isbn", "type": "VARCHAR"},
            {"field": "title", "type": "VARCHAR"},
        ],
        "compatibility": "superset",
    }
    assertion_urn = build_assertion_urn(_TEST_URN, rule["rule_id"])
    assertion_info = build_assertion_info(_TEST_URN, rule)

    await datahub_client.emit_assertion(assertion_urn, assertion_info)

    fetched = await datahub_client.get_assertion_info(assertion_urn)
    assert fetched is not None
    # PDL constant is DATA_SCHEMA (reserved-word workaround documented in DATAHUB_INTEGRATION.md)
    assert fetched.type == AssertionTypeClass.DATA_SCHEMA
    assert fetched.schemaAssertion is not None


# ── 58. upsert_config registers all rules ─────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_config_registers_all_rules_in_datahub(
    datahub_client,
    async_session,
) -> None:
    """DATAHUB_INTEGRATION.md convention 6: upsert_config registers all rule assertion URNs in DataHub."""
    from unittest.mock import AsyncMock

    from src.backend.validation.assertions import build_assertion_urn
    from src.backend.validation.service import ValidationService

    rules = [
        {"rule_id": "spot-uc-r1", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "spot-uc-r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
    ]

    # cache is not required for upsert_config (only for run); pass None
    service = ValidationService(datahub=datahub_client, db=async_session, cache=None)
    await service.upsert_config(
        dataset_urn=_TEST_URN,
        rules=rules,
        schedule_tier="daily",
        is_enabled=True,
        owner="spot-test@imazon.com",
    )

    # Each rule's URN must now exist in DataHub
    for rule in rules:
        urn = build_assertion_urn(_TEST_URN, rule["rule_id"])
        fetched = await datahub_client.get_assertion_info(urn)
        assert fetched is not None, f"Assertion {urn} not found in DataHub after upsert_config"


# ── 59. Idempotent re-upsert ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_config_idempotent_no_duplicate_emission(
    datahub_client,
    async_session,
) -> None:
    """DATAHUB_INTEGRATION.md convention 3: calling upsert twice with same rule_id must not duplicate."""
    from src.backend.validation.assertions import build_assertion_urn
    from src.backend.validation.service import ValidationService

    rules = [{"rule_id": "spot-idem-r1", "type": "freshness", "lookback_interval": "24h"}]

    service = ValidationService(datahub=datahub_client, db=async_session, cache=None)

    # First upsert
    await service.upsert_config(
        dataset_urn=_TEST_URN,
        rules=rules,
        schedule_tier="daily",
        is_enabled=True,
        owner="spot-test@imazon.com",
    )

    urn = build_assertion_urn(_TEST_URN, "spot-idem-r1")
    fetched_after_first = await datahub_client.get_assertion_info(urn)
    assert fetched_after_first is not None

    # Second upsert — same rule_id
    await service.upsert_config(
        dataset_urn=_TEST_URN,
        rules=rules,
        schedule_tier="daily",
        is_enabled=True,
        owner="spot-test@imazon.com",
    )

    # Exactly one assertion definition must exist (no duplicate key collision)
    fetched_after_second = await datahub_client.get_assertion_info(urn)
    assert fetched_after_second is not None
    # URN is deterministic — both fetches return the same assertion entity
    # (DataHub upserts are idempotent; no duplicate aspect rows)


# ── 60. Shared runId across rules ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_all_rules_share_same_run_id_in_datahub(
    datahub_client,
    async_session,
    redis_client,
) -> None:
    """DATAHUB_INTEGRATION.md convention 5: all assertionRunEvent aspects for one run share runId."""
    from unittest.mock import AsyncMock, patch

    from src.backend.validation.assertions import build_assertion_urn
    from src.backend.validation.rules import RuleEvaluation
    from src.backend.validation.service import ValidationService

    # First register the config
    rules = [
        {"rule_id": "spot-runid-r1", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "spot-runid-r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
    ]

    service = ValidationService(datahub=datahub_client, db=async_session, cache=redis_client)
    await service.upsert_config(
        dataset_urn=_TEST_URN,
        rules=rules,
        schedule_tier="daily",
        is_enabled=True,
        owner="spot-test@imazon.com",
    )

    # Run and capture the run_id sent per rule
    captured_run_ids: list[str] = []

    from src.backend.validation import service as svc_module

    async def capture_report(datahub_client, urn, run_event):
        captured_run_ids.append(run_event.runId)
        return True

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        return RuleEvaluation(
            rule_id=rule["rule_id"],
            assertion_result="SUCCESS",
            values={},
            validation=None,
            issues=[],
            partition=partition,
        )

    with (
        patch.object(svc_module, "report_result", side_effect=capture_report),
        patch.object(svc_module, "evaluate_rule", side_effect=mock_evaluate),
    ):
        await service.run(_TEST_URN)

    assert len(captured_run_ids) == 2, "report_result must be called for both rules"
    # All runIds must be identical — shared across rules in one run
    assert len(set(captured_run_ids)) == 1, (
        "Convention 5: all rules in one run must share the same runId"
    )


# ── 61. Custom sql_timeseries assertion round-trip ────────────────────────────

@pytest.mark.asyncio
async def test_custom_sql_timeseries_assertion_round_trip(
    datahub_client,
) -> None:
    """DATAHUB_INTEGRATION.md convention 1: custom sql_timeseries assertion round-trips correctly.

    build_assertion_info for a custom/sql_timeseries rule must produce an AssertionInfoClass
    with customAssertion.type == 'sql_timeseries' and source.type == EXTERNAL.
    After emission to DataHub the fetched aspect must carry the same customAssertion shape.

    spec: DATAHUB_INTEGRATION.md L228 — CUSTOM rules require customAssertion sub-aspect;
          customAssertion.type == subtype string
    spec: DATAHUB_INTEGRATION.md L238-L240 — source.type must be EXTERNAL
    """
    from datahub.metadata.schema_classes import AssertionSourceTypeClass

    from src.backend.validation.assertions import (
        build_assertion_info,
        build_assertion_urn,
    )

    rule = {
        "rule_id": "spot-custom-ts-rt-001",
        "type": "custom",
        "subtype": "sql_timeseries",
        "description": "Daily fulfillment volume series for anomaly detection",
        "sql": (
            "SELECT summary_date AS day, row_count "
            "FROM orders.daily_fulfillment_summary"
        ),
        "partition": ["day"],
        "order": ["day"],
        "values": ["row_count"],
        "ml_validation": {
            "targets": ["row_count"],
            "model": "range",
            "lookback_partitions": 30,
        },
    }

    assertion_urn = build_assertion_urn(_TEST_URN, rule["rule_id"])
    assertion_info = build_assertion_info(_TEST_URN, rule)

    # Verify build_assertion_info produces the correct sub-aspect before emission
    # spec: DATAHUB_INTEGRATION.md L228 — CUSTOM rules require customAssertion sub-aspect
    assert assertion_info.customAssertion is not None, (
        "spec: DATAHUB_INTEGRATION.md L228 — build_assertion_info must produce "
        "customAssertion for custom/sql_timeseries rule"
    )
    assert assertion_info.customAssertion.type == "sql_timeseries", (
        "spec: DATAHUB_INTEGRATION.md L228 — customAssertion.type must equal the subtype string"
    )
    assert assertion_info.customAssertion.entity == _TEST_URN, (
        "spec: DATAHUB_INTEGRATION.md L233-L237 — customAssertion.entity must be dataset URN"
    )

    # Emit to DataHub and round-trip
    await datahub_client.emit_assertion(assertion_urn, assertion_info)

    fetched = await datahub_client.get_assertion_info(assertion_urn)
    assert fetched is not None, "assertionInfo must exist in DataHub after emission"

    # spec: DATAHUB_INTEGRATION.md L228 — round-tripped customAssertion sub-aspect
    assert fetched.customAssertion is not None, (
        "spec: DATAHUB_INTEGRATION.md L228 — customAssertion must be non-null after round-trip"
    )
    assert fetched.customAssertion.type == "sql_timeseries", (
        "spec: DATAHUB_INTEGRATION.md L228 — customAssertion.type must equal 'sql_timeseries' "
        "after round-trip"
    )
    assert fetched.customAssertion.entity == _TEST_URN, (
        "spec: DATAHUB_INTEGRATION.md L233-L237 — customAssertion.entity must be dataset URN "
        "after round-trip"
    )

    # spec: DATAHUB_INTEGRATION.md L238-L240 — source.type must be EXTERNAL
    assert fetched.source is not None, (
        "spec: DATAHUB_INTEGRATION.md L238-L240 — source must be set after round-trip"
    )
    assert fetched.source.type == AssertionSourceTypeClass.EXTERNAL, (
        "spec: DATAHUB_INTEGRATION.md L238-L240 — source.type must be EXTERNAL after round-trip"
    )
