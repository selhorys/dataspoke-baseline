"""Unit tests for ValidationService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

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
    rules: dict | None = None,
    schedule: str | None = "0 0 * * *",
    sla_target: dict | None = None,
    status: str = "draft",
    owner: str = "alice@example.com",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.rules = rules or {"freshness": {"max_age_hours": 24}}
    row.schedule = schedule
    row.sla_target = sla_target
    row.status = status
    row.owner = owner
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    dataset_urn: str = _DATASET_URN,
    quality_score: float = 75.0,
    minutes_ago: int = 5,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.quality_score = quality_score
    row.dimensions = {"completeness": 80.0, "freshness": 70.0}
    row.issues = [{"dimension": "freshness", "score": 70.0}]
    row.anomalies = []
    row.recommendations = ["Check freshness"]
    row.alternatives = []
    row.run_id = uuid.uuid4()
    row.measured_at = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return row


@pytest.fixture
def service(datahub, db, cache, llm, qdrant):
    return ValidationService(datahub=datahub, db=db, cache=cache, llm=llm, qdrant=qdrant)


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
        rules={"freshness": {"max_age_hours": 24}},
        schedule=None,
        sla_target=None,
        owner="alice@example.com",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


async def test_upsert_config_updates_existing(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        rules={"completeness": {"min_ratio": 0.9}},
        schedule="0 6 * * *",
        sla_target={"freshness_hours": 12},
        owner="bob@example.com",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    assert existing_row.rules == {"completeness": {"min_ratio": 0.9}}
    assert existing_row.owner == "bob@example.com"


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_partial(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"schedule": "0 12 * * *"})
    assert existing_row.schedule == "0 12 * * *"
    db.commit.assert_awaited_once()


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule": "0 12 * * *"})
    assert exc_info.value.error_code == "VALIDATION_CONFIG_NOT_FOUND"


# ── delete_config ────────────────────────────────────────────────────────────


async def test_delete_config_success(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)

    await service.delete_config(_DATASET_URN)
    db.delete.assert_awaited_once_with(existing_row)
    db.commit.assert_awaited_once()


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

    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    cache.publish = AsyncMock()

    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])
    datahub.get_downstream_lineage = AsyncMock(return_value=[])

    result = await service.run(_DATASET_URN)
    assert result.status == "success"
    assert result.run_id
    assert result.detail["dry_run"] is False


async def test_run_dry_run(service, db, datahub, cache):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()

    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])

    result = await service.run(_DATASET_URN, dry_run=True)
    assert result.detail["dry_run"] is True
    # dry_run should not persist result or publish
    cache.publish.assert_not_awaited()


async def test_run_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    assert exc_info.value.error_code == "VALIDATION_CONFIG_NOT_FOUND"


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(entity_type="validation", event_type="validation.completed", minutes_ago=i)
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
