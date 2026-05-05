"""Unit tests — registration timing, patch behavior, ERROR-on-emit (Groups A3, A4).

Spec sources:
- spec/DATAHUB_INTEGRATION.md §Assertion Aspects conventions 5, 6, 7
- spec/feature/BACKEND.md §Validation Service — registration timing wording,
  run-event ERROR semantics
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.exceptions import DataHubUnavailableError, EntityNotFoundError
from tests.unit.backend.conftest import mock_db_refresh, mock_scalar_query

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.catalog.title_master,DEV)"


def _make_config_row(
    rules: list | None = None,
    is_enabled: bool = True,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = _DATASET_URN
    row.rules = rules if rules is not None else [
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
        {"rule_id": "r3", "type": "field", "field": "isbn", "metric": "null_count",
         "condition": {"type": "less_than_or_equal_to", "value": 0}},
    ]
    row.schedule_tier = "daily"
    row.is_enabled = is_enabled
    row.owner = "de@imazon.com"
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(assertion_result: str = "SUCCESS", rule_id: str = "r1"):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = _DATASET_URN
    row.rule_id = rule_id
    row.partition = {}
    row.values = {}
    row.validation = None
    row.assertion_result = assertion_result
    row.issues = []
    row.run_id = uuid.uuid4()
    row.measured_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db, cache):
    return ValidationService(datahub=datahub, db=db, cache=cache)


# ── Group A3: upsert_config registration timing ───────────────────────────────


async def test_upsert_config_calls_register_assertion_for_each_rule(service, datahub, db, cache):
    """DATAHUB_INTEGRATION.md convention 6: register_assertion called for each rule after DB commit."""
    rules = [
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
        {"rule_id": "r3", "type": "field", "field": "col", "metric": "null_count",
         "condition": {}},
    ]

    # DB returns None for existing check (creates new), then returns config row after commit
    result_mock_none = MagicMock()
    result_mock_none.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock_none)

    datahub.get_assertion_info = AsyncMock(return_value=None)
    datahub.emit_assertion = AsyncMock()

    mock_db_refresh(db)

    with patch("src.backend.validation.service.ensure_dataset_registered", new=AsyncMock()):
        await service.upsert_config(
            dataset_urn=_DATASET_URN,
            rules=rules,
            schedule_tier="daily",
            is_enabled=True,
            owner="de@imazon.com",
        )

    # register_assertion = get_assertion_info + (possibly) emit_assertion per rule
    # get_assertion_info must be called 3 times (once per rule)
    assert datahub.get_assertion_info.await_count == 3


async def test_upsert_config_propagates_datahub_unavailable_error(service, datahub, db):
    """DATAHUB_INTEGRATION.md convention 6: DataHub error during registration surfaces as exception."""
    rules = [{"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"}]

    result_mock_none = MagicMock()
    result_mock_none.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock_none)

    datahub.get_assertion_info = AsyncMock(return_value=None)
    datahub.emit_assertion = AsyncMock(side_effect=DataHubUnavailableError("GMS down"))

    mock_db_refresh(db)

    with patch("src.backend.validation.service.ensure_dataset_registered", new=AsyncMock()):
        with pytest.raises(DataHubUnavailableError):
            await service.upsert_config(
                dataset_urn=_DATASET_URN,
                rules=rules,
                schedule_tier="daily",
                is_enabled=True,
                owner="de@imazon.com",
            )


async def test_patch_config_registers_when_rules_in_patch(service, datahub, db):
    """BACKEND.md §Validation Service: patch with rules triggers register_assertion for each rule."""
    existing = _make_config_row(rules=[{"rule_id": "r1", "type": "freshness"}])
    mock_scalar_query(db, existing)
    mock_db_refresh(db)

    datahub.get_assertion_info = AsyncMock(return_value=None)
    datahub.emit_assertion = AsyncMock()

    new_rules = [
        {"rule_id": "r_new1", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
        {"rule_id": "r_new2", "type": "field", "field": "col", "metric": "null_count", "condition": {}},
    ]

    await service.patch_config(_DATASET_URN, {"rules": new_rules})

    # register_assertion called once per rule in the patch
    assert datahub.get_assertion_info.await_count == 2


async def test_patch_config_does_not_register_when_rules_absent(service, datahub, db):
    """BACKEND.md §Validation Service: patch without rules skips register_assertion entirely."""
    existing = _make_config_row()
    mock_scalar_query(db, existing)
    mock_db_refresh(db)

    datahub.get_assertion_info = AsyncMock()
    datahub.emit_assertion = AsyncMock()

    # Patch only is_enabled — no rules key
    await service.patch_config(_DATASET_URN, {"is_enabled": True})

    datahub.get_assertion_info.assert_not_awaited()
    datahub.emit_assertion.assert_not_awaited()


async def test_patch_config_does_not_register_when_rules_is_none(service, datahub, db):
    """BACKEND.md §Validation Service: patch with rules=None skips registration (None-tolerant)."""
    existing = _make_config_row()
    mock_scalar_query(db, existing)
    mock_db_refresh(db)

    datahub.get_assertion_info = AsyncMock()
    datahub.emit_assertion = AsyncMock()

    # rules explicitly None
    await service.patch_config(_DATASET_URN, {"rules": None, "is_enabled": False})

    datahub.get_assertion_info.assert_not_awaited()
    datahub.emit_assertion.assert_not_awaited()


# ── Group A4: _run_inner ERROR-on-emit behavior ───────────────────────────────


async def test_run_inner_emit_failure_produces_error_assertion_result(service, datahub, db, cache):
    """DATAHUB_INTEGRATION.md convention 7: emit failure → assertion_result=ERROR in persisted row."""
    from src.backend.validation.rules import RuleEvaluation

    config_row = _make_config_row(rules=[
        {"rule_id": "r_good", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "r_bad", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
    ])

    # First call returns config for get_config
    result_with_config = MagicMock()
    result_with_config.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_with_config)
    mock_db_refresh(db)

    # report_result: success for first call (r_good), fail for second call (r_bad)
    # URNs are GUIDs so we track call order instead of URN string matching
    report_call_sequence = [True, False]  # first rule succeeds, second fails
    report_call_index = [0]

    async def mock_report_result(datahub_client, urn, run_event):
        idx = report_call_index[0]
        report_call_index[0] += 1
        return report_call_sequence[idx] if idx < len(report_call_sequence) else True

    # evaluate_rule: both rules succeed their evaluations (pre-emit)
    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        return RuleEvaluation(
            rule_id=rule["rule_id"],
            assertion_result="SUCCESS",
            values={"hours_since_last_update": 1.0},
            validation=None,
            issues=[],
            partition=partition,
        )

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()
    cache.publish = AsyncMock()
    cache.set = AsyncMock()

    persisted_rows = []

    def capture_add(obj):
        persisted_rows.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    with (
        patch("src.backend.validation.service.evaluate_rule", side_effect=mock_evaluate),
        patch("src.backend.validation.service.report_result", side_effect=mock_report_result),
        patch("src.backend.validation.service.register_assertion", new=AsyncMock()),
    ):
        summary = await service.run(_DATASET_URN, partition=None)

    # Filter to ValidationResult rows only (not Event rows)
    from src.shared.db.models import ValidationResult as VRModel
    validation_rows = [r for r in persisted_rows if isinstance(r, VRModel)]
    assert len(validation_rows) == 2, f"Expected 2 ValidationResult rows, got {len(validation_rows)}"

    # The second rule (r_bad) should have emit failure → ERROR
    error_rows = [r for r in validation_rows if r.assertion_result == "ERROR"]
    assert len(error_rows) == 1, (
        f"Exactly one rule should be ERROR (the one with emit failure). "
        f"Got: {[(r.rule_id, r.assertion_result) for r in validation_rows]}"
    )

    # Spec invariants: assertion_result is ERROR and issues list is non-empty
    error_row = error_rows[0]
    assert error_row.rule_id == "r_bad"
    assert error_row.assertion_result == "ERROR"
    assert len(error_row.issues or []) > 0, "ERROR result must carry at least one issue"


async def test_run_inner_emit_failure_increments_errored_not_failed(service, datahub, db, cache):
    """DATAHUB_INTEGRATION.md convention 7: emit failure counted as errored, not failed."""
    from src.backend.validation.rules import RuleEvaluation

    config_row = _make_config_row(rules=[
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"},
    ])

    result_with_config = MagicMock()
    result_with_config.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_with_config)
    mock_db_refresh(db)

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        # Pre-emit evaluation was SUCCESS
        return RuleEvaluation(
            rule_id=rule["rule_id"],
            assertion_result="SUCCESS",
            values={},
            validation=None,
            issues=[],
            partition=partition,
        )

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()
    cache.publish = AsyncMock()
    cache.set = AsyncMock()

    with (
        patch("src.backend.validation.service.evaluate_rule", side_effect=mock_evaluate),
        patch("src.backend.validation.service.report_result", new=AsyncMock(return_value=False)),
        patch("src.backend.validation.service.register_assertion", new=AsyncMock()),
    ):
        summary = await service.run(_DATASET_URN, partition=None)

    assert summary.errored == 1, "Emit failure must increment errored counter"
    assert summary.passed == 0, "Pre-emit SUCCESS must not count as passed when emit fails"
    assert summary.failed == 0


async def test_run_inner_shared_run_id_across_rules(service, datahub, db, cache):
    """DATAHUB_INTEGRATION.md convention 5: all rules in one run share the same runId."""
    from src.backend.validation.rules import RuleEvaluation

    config_row = _make_config_row(rules=[
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
        {"rule_id": "r3", "type": "field", "field": "col", "metric": "null_count", "condition": {}},
    ])

    result_with_config = MagicMock()
    result_with_config.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_with_config)
    mock_db_refresh(db)

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        return RuleEvaluation(
            rule_id=rule["rule_id"],
            assertion_result="SUCCESS",
            values={},
            validation=None,
            issues=[],
            partition=partition,
        )

    # Capture the runId sent to each report_result call
    captured_run_ids = []

    async def capture_report_result(datahub_client, urn, run_event):
        captured_run_ids.append(run_event.runId)
        return True

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()
    cache.publish = AsyncMock()
    cache.set = AsyncMock()

    with (
        patch("src.backend.validation.service.evaluate_rule", side_effect=mock_evaluate),
        patch("src.backend.validation.service.report_result", side_effect=capture_report_result),
        patch("src.backend.validation.service.register_assertion", new=AsyncMock()),
    ):
        await service.run(_DATASET_URN, partition=None)

    assert len(captured_run_ids) == 3, "report_result must be called for all 3 rules"
    # All runIds must be identical
    assert len(set(captured_run_ids)) == 1, (
        "Anti-pattern: runId regenerated per rule breaks DataHub timeline grouping"
    )


async def test_run_inner_no_register_assertion_calls(service, datahub, db, cache):
    """BACKEND.md §Validation Service: registration is at upsert only; run calls no register_assertion."""
    from src.backend.validation.rules import RuleEvaluation

    config_row = _make_config_row(rules=[
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"},
    ])

    result_with_config = MagicMock()
    result_with_config.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_with_config)
    mock_db_refresh(db)

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        return RuleEvaluation(
            rule_id=rule["rule_id"],
            assertion_result="SUCCESS",
            values={},
            validation=None,
            issues=[],
            partition=partition,
        )

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()
    cache.publish = AsyncMock()
    cache.set = AsyncMock()

    register_mock = AsyncMock()

    with (
        patch("src.backend.validation.service.evaluate_rule", side_effect=mock_evaluate),
        patch("src.backend.validation.service.report_result", new=AsyncMock(return_value=True)),
        patch("src.backend.validation.service.register_assertion", new=register_mock),
    ):
        await service.run(_DATASET_URN, partition=None)

    register_mock.assert_not_awaited()
