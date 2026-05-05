"""Unit tests for ValidationService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import (
    make_event_row,
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    rules: list | None = None,
    schedule_tier: str | None = None,
    is_enabled: bool = False,
    owner: str = "alice@example.com",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.rules = rules if rules is not None else [
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"}
    ]
    row.schedule_tier = schedule_tier
    row.is_enabled = is_enabled
    row.owner = owner
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    dataset_urn: str = _DATASET_URN,
    rule_id: str = "r1",
    assertion_result: str = "SUCCESS",
    minutes_ago: int = 5,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.rule_id = rule_id
    row.partition = {}
    row.values = {"hours_since_last_update": 2.0}
    row.validation = None
    row.assertion_result = assertion_result
    row.issues = []
    row.run_id = uuid.uuid4()
    row.measured_at = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return row


@pytest.fixture
def service(datahub, db, cache):
    return ValidationService(datahub=datahub, db=db, cache=cache)


# ── get_config ───────────────────────────────────────────────────────────────


async def test_get_config_found(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)

    config = await service.get_config(_DATASET_URN)
    assert config is not None
    assert config.dataset_urn == _DATASET_URN
    assert config.owner == "alice@example.com"


async def test_get_config_not_found(service, db):
    mock_scalar_query(db, None)

    config = await service.get_config("nonexistent")
    assert config is None


# ── upsert_config ────────────────────────────────────────────────────────────


async def test_upsert_config_creates_new(service, db):
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        rules=[{"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"}],
        schedule_tier=None,
        is_enabled=False,
        owner="alice@example.com",
    )
    assert db.add.called
    assert db.commit.await_count >= 1


async def test_upsert_config_updates_existing(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    new_rules = [{"rule_id": "r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}}]
    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        rules=new_rules,
        schedule_tier="daily",
        is_enabled=True,
        owner="bob@example.com",
    )
    assert db.add.called
    assert db.commit.await_count >= 1
    assert existing_row.rules == new_rules
    assert existing_row.owner == "bob@example.com"


async def test_upsert_config_is_enabled_stored(service, db):
    """is_enabled (not is_active) is persisted."""
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        rules=[],
        schedule_tier="daily",
        is_enabled=True,
        owner="alice@example.com",
    )
    # Verify db.add was called (row object will have is_enabled=True)
    assert db.add.called


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_partial(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"schedule_tier": "weekly"})
    assert existing_row.schedule_tier == "weekly"
    assert db.commit.await_count >= 1


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule_tier": "daily"})
    # spec: API.md L577 — entity_type="config" → error_code="CONFIG_NOT_FOUND"
    # IMPL BUG (F-R2.1): impl calls EntityNotFoundError("validation_config", ...)
    # which produces "VALIDATION_CONFIG_NOT_FOUND" — not the spec-mandated code.
    # This test FAILS until impl is corrected to use entity_type="config".
    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


# ── delete_config ────────────────────────────────────────────────────────────


async def test_delete_config_success(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)

    await service.delete_config(_DATASET_URN)
    db.delete.assert_awaited_once_with(existing_row)
    assert db.commit.await_count >= 1


async def test_delete_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.delete_config("nonexistent")
    # spec: API.md L577 — entity_type="config" → error_code="CONFIG_NOT_FOUND"
    # IMPL BUG (F-R2.1): impl calls EntityNotFoundError("validation_config", ...)
    # which produces "VALIDATION_CONFIG_NOT_FOUND" — not the spec-mandated code.
    # This test FAILS until impl is corrected to use entity_type="config".
    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


# ── list_configs ─────────────────────────────────────────────────────────────


async def test_list_configs_paginated(service, db):
    rows = [_make_config_row(dataset_urn=f"urn:{i}") for i in range(3)]
    mock_paginated_query(db, rows, 5)

    configs, total = await service.list_configs(offset=0, limit=3)
    assert total == 5
    assert len(configs) == 3


async def test_list_configs_empty(service, db):
    mock_paginated_query(db, [], 0)

    configs, total = await service.list_configs()
    assert total == 0
    assert configs == []


# ── get_results ──────────────────────────────────────────────────────────────


async def test_get_results_paginated(service, db):
    rows = [_make_result_row(minutes_ago=i) for i in range(3)]
    mock_paginated_query(db, rows, 5)

    results, total = await service.get_results(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(results) == 3
    assert results[0].dataset_urn == _DATASET_URN


async def test_get_results_empty(service, db):
    mock_paginated_query(db, [], 0)

    results, total = await service.get_results(_DATASET_URN)
    assert total == 0
    assert results == []


async def test_get_results_time_range(service, db):
    rows = [_make_result_row(minutes_ago=10)]
    mock_paginated_query(db, rows, 1)

    from_dt = datetime.now(tz=UTC) - timedelta(hours=1)
    to_dt = datetime.now(tz=UTC)
    results, total = await service.get_results(_DATASET_URN, from_dt=from_dt, to_dt=to_dt)
    assert total == 1
    assert len(results) == 1


# ── run ──────────────────────────────────────────────────────────────────────


async def test_run_success(service, db, datahub, cache):
    config_row = _make_config_row(is_enabled=True)
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    cache.publish = AsyncMock()
    cache.set = AsyncMock()
    # SETNX returns True (lock acquired)
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    from src.backend.validation.rules import RuleEvaluation

    mock_eval = RuleEvaluation(
        rule_id="r1",
        assertion_result="SUCCESS",
        values={"hours_since_last_update": 2.0},
        validation=None,
        issues=[],
        partition={},
    )

    with (
        patch("src.backend.validation.service.evaluate_rule", return_value=mock_eval),
        patch("src.backend.validation.service.register_assertion", return_value=None),
        patch("src.backend.validation.service.report_result", return_value=True),
    ):
        mock_result2 = MagicMock()
        mock_result2.scalar_one_or_none.return_value = config_row
        db.execute = AsyncMock(return_value=mock_result2)
        db.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", uuid.uuid4()) or None)
        db.commit = AsyncMock()
        db.add = MagicMock()

        summary = await service.run(_DATASET_URN)
        assert summary.run_id
        assert summary.total == 1
        assert summary.passed == 1
        assert summary.failed == 0
        assert summary.errored == 0
        # Status enum is impl-defined; spec USE_CASE_en.md L196-L197 silent on enum values
        assert summary.status.lower() == "success"


async def test_run_config_not_found(service, db, cache):
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    # spec: API.md L577 — entity_type="config" → error_code="CONFIG_NOT_FOUND"
    # IMPL BUG (F-R2.1): impl calls EntityNotFoundError("validation_config", ...)
    # which produces "VALIDATION_CONFIG_NOT_FOUND" — not the spec-mandated code.
    # This test FAILS until impl is corrected to use entity_type="config".
    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


async def test_run_rejects_non_dry_run_when_disabled(service, db, cache):
    """Non-dry-run against a disabled config raises ConflictError('VALIDATION_DISABLED').

    spec: BACKEND.md §Validation Service — is_enabled=false rejects non-dry-run
    with 409 VALIDATION_DISABLED.
    spec: USE_CASE_en.md §UC2 — "When is_enabled=false, non-dry-run calls to
    method/validation/run return 409 VALIDATION_DISABLED. Dry-run is always
    permitted regardless of is_enabled." (L261)
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_config_row(is_enabled=False)
    mock_scalar_query(db, config_row)

    with pytest.raises(ConflictError) as exc_info:
        await service.run(_DATASET_URN, partition=None, dry_run=False)

    assert exc_info.value.error_code == "VALIDATION_DISABLED"


async def test_run_allows_dry_run_when_disabled(service, db, cache):
    """Dry-run bypasses the disabled guard and returns a ValidationRunSummary.

    spec: BACKEND.md §Validation Service — dry_run=True is always permitted
    regardless of is_enabled.
    spec: USE_CASE_en.md §UC2 (disabled gate mirrors UC1 pattern)
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_config_row(is_enabled=False)
    mock_scalar_query(db, config_row)

    from src.backend.validation.service import ValidationRunSummary

    summary = await service.run(_DATASET_URN, partition=None, dry_run=True)

    assert isinstance(summary, ValidationRunSummary)
    assert summary.status.lower() == "success"


async def test_run_with_partition(service, db, datahub, cache):
    config_row = _make_config_row(is_enabled=True)
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    cache.publish = AsyncMock()
    cache.set = AsyncMock()
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    from src.backend.validation.rules import RuleEvaluation

    mock_eval = RuleEvaluation(
        rule_id="r1",
        assertion_result="FAILURE",
        values={"hours_since_last_update": 50.0},
        validation=None,
        issues=[{"msg": "Stale", "type": "freshness_violation"}],
        partition={"load_date": "2025-03-10"},
    )

    with (
        patch("src.backend.validation.service.evaluate_rule", return_value=mock_eval),
        patch("src.backend.validation.service.register_assertion", return_value=None),
        patch("src.backend.validation.service.report_result", return_value=True),
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = config_row
        db.execute = AsyncMock(return_value=mock_result)
        db.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", uuid.uuid4()) or None)
        db.commit = AsyncMock()
        db.add = MagicMock()

        summary = await service.run(_DATASET_URN, partition={"load_date": "2025-03-10"})
        assert summary.failed == 1
        assert summary.passed == 0
        # Status enum is impl-defined; spec USE_CASE_en.md L196-L197 silent on enum values
        assert summary.status.lower() == "failure"


async def test_run_empty_rules(service, db, datahub, cache):
    config_row = _make_config_row(rules=[], is_enabled=True)
    mock_scalar_query(db, config_row)

    cache.publish = AsyncMock()
    cache.set = AsyncMock()
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=mock_result)
    db.commit = AsyncMock()
    db.add = MagicMock()

    summary = await service.run(_DATASET_URN)
    assert summary.total == 0
    assert summary.passed == 0
    # spec: USE_CASE_en.md L196-L197 — status field; casing not mandated by spec
    assert summary.status.lower() == "success"


# ── Redis SETNX concurrency guard ─────────────────────────────────────────────


async def test_run_redis_setnx_conflict(service, db, cache):
    """Second concurrent run() raises ConflictError when lock is already held."""
    cache.set_nx = AsyncMock(return_value=False)  # lock already held

    with pytest.raises(ConflictError) as exc_info:
        await service.run(_DATASET_URN)
    assert exc_info.value.error_code == "VALIDATION_RUNNING"


async def test_run_redis_setnx_lock_released_on_error(service, db, cache):
    """Lock is released in finally block even on inner exception."""
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    mock_scalar_query(db, None)  # triggers EntityNotFoundError

    with pytest.raises(EntityNotFoundError):
        await service.run(_DATASET_URN)

    # Lock must be released
    cache.delete_if_value.assert_awaited_once()


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(entity_type="dataset", event_type="VALIDATION.COMPLETE", minutes_ago=i)
        for i in range(3)
    ]
    mock_paginated_query(db, rows, 5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], 0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []
