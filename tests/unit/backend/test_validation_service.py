"""Unit tests for ValidationService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.exceptions import EntityNotFoundError
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
    schedule_cron: str | None = None,
    is_active: bool = False,
    owner: str = "alice@example.com",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.rules = rules if rules is not None else [
        {"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"}
    ]
    row.schedule_cron = schedule_cron
    row.is_active = is_active
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
        schedule_cron=None,
        is_active=False,
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
        schedule_cron="0 6 * * *",
        is_active=True,
        owner="bob@example.com",
    )
    assert db.add.called
    assert db.commit.await_count >= 1
    assert existing_row.rules == new_rules
    assert existing_row.owner == "bob@example.com"


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_partial(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"schedule_cron": "0 12 * * *"})
    assert existing_row.schedule_cron == "0 12 * * *"
    assert db.commit.await_count >= 1


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule_cron": "0 12 * * *"})
    assert exc_info.value.error_code == "VALIDATION_CONFIG_NOT_FOUND"


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
    assert exc_info.value.error_code == "VALIDATION_CONFIG_NOT_FOUND"


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
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    cache.publish = AsyncMock()
    cache.set = AsyncMock()

    # Patch evaluate_rule and DataHub assertion methods
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
        patch("src.backend.validation.service.report_result", return_value=None),
    ):
        # db.execute needs to return result_row after add+commit+refresh
        result_row = _make_result_row()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = config_row
        db.execute = AsyncMock(return_value=mock_result)
        db.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", uuid.uuid4()) or None)
        db.commit = AsyncMock()
        db.add = MagicMock()

        # For get_config call
        mock_result2 = MagicMock()
        mock_result2.scalar_one_or_none.return_value = config_row
        db.execute = AsyncMock(return_value=mock_result2)

        summary = await service.run(_DATASET_URN)
        assert summary.run_id
        assert summary.total == 1
        assert summary.passed == 1
        assert summary.failed == 0
        assert summary.errored == 0
        assert summary.status == "success"


async def test_run_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    assert exc_info.value.error_code == "VALIDATION_CONFIG_NOT_FOUND"


async def test_run_with_partition(service, db, datahub, cache):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    cache.publish = AsyncMock()
    cache.set = AsyncMock()

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
        patch("src.backend.validation.service.report_result", return_value=None),
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
        assert summary.status == "failure"


async def test_run_empty_rules(service, db, datahub, cache):
    config_row = _make_config_row(rules=[])
    mock_scalar_query(db, config_row)

    cache.publish = AsyncMock()
    cache.set = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=mock_result)
    db.commit = AsyncMock()
    db.add = MagicMock()

    summary = await service.run(_DATASET_URN)
    assert summary.total == 0
    assert summary.passed == 0
    assert summary.status == "success"


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(entity_type="validation", event_type="VALIDATION.COMPLETE", minutes_ago=i)
        for i in range(3)
    ]
    mock_paginated_query(db, rows, 5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "validation"


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], 0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []
