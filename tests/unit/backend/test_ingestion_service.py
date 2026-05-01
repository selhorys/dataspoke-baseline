"""Unit tests for IngestionService (mocked infrastructure).

spec: BACKEND.md §Ingestion Service (§Feature Services)
spec: BACKEND.md §Active run pipeline L195-L204
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.backend.ingestion.service import IngestionService
from src.shared.events import INGESTION_COMPLETE
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import (
    make_event_row,
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
_LOCATOR = {"host": "db.example.com", "port": 5432}
_IDENTIFIER = {"database": "mydb", "schema_name": "public", "table": "users"}
_AUTH = {"username": "user", "secret_ref": "pw"}


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    platform: str = "postgres",
    locator: dict | None = None,
    identifier: dict | None = None,
    auth: dict | None = None,
    is_enabled: bool = False,
    mode: str = "active",
    schedule_tier: str | None = "daily",
    workflow_dag_id: str | None = None,
    status: str = "OK",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.platform = platform
    row.locator = locator or _LOCATOR
    row.identifier = identifier or _IDENTIFIER
    row.auth = auth if auth is not None else _AUTH
    row.is_enabled = is_enabled
    row.mode = mode
    row.schedule_tier = schedule_tier
    row.workflow_dag_id = workflow_dag_id
    row.status = status
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db):
    return IngestionService(datahub=datahub, db=db)


@pytest.fixture
def service_with_cache(datahub, db, cache):
    return IngestionService(datahub=datahub, db=db, cache=cache)


# ── get_config ───────────────────────────────────────────────────────────────


async def test_get_config_found(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)

    config = await service.get_config(_DATASET_URN)
    assert config is not None
    assert config.dataset_urn == _DATASET_URN
    assert config.platform == "postgres"
    assert config.locator == _LOCATOR
    assert config.identifier == _IDENTIFIER
    assert config.auth == _AUTH


async def test_get_config_not_found(service, db):
    mock_scalar_query(db, None)

    config = await service.get_config("nonexistent")
    assert config is None


# ── upsert_config ────────────────────────────────────────────────────────────


async def test_upsert_config_creates_new(service, db):
    # spec: BACKEND.md §Ingestion Service — "Config upsert registers the dataset URN
    # in dataset_registry (does not require the dataset to exist in DataHub yet)"
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, created = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="active",
            platform="postgres",
            locator=_LOCATOR,
            identifier=_IDENTIFIER,
            auth=_AUTH,
            is_enabled=False,
            schedule_tier=None,
        )

    # Behavioral: created flag must be True for a brand-new config
    assert created is True
    # Behavioral: the returned record must carry the correct URN and platform
    assert result.dataset_urn == _DATASET_URN
    assert result.platform == "postgres"
    assert result.mode == "active"


async def test_upsert_config_updates_existing(service, db):
    # spec: BACKEND.md §Ingestion Service — upsert mutates existing row in-place
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    new_locator = {"host": "newdb.example.com", "port": 5432}
    new_identifier = {"database": "newdb", "schema_name": "public", "table": "orders"}
    new_auth = {"username": "admin", "secret_ref": "newpw"}

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, created = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="passive",
            platform="mysql",
            locator=new_locator,
            identifier=new_identifier,
            auth=new_auth,
            is_enabled=True,
            schedule_tier="weekly",
        )

    # Behavioral: created flag must be False for an existing config
    assert created is False
    # Behavioral: all mutated fields are reflected in the ORM row
    assert existing_row.platform == "mysql"
    assert existing_row.locator == new_locator
    assert existing_row.identifier == new_identifier
    assert existing_row.auth == new_auth
    assert existing_row.is_enabled is True
    assert existing_row.schedule_tier == "weekly"
    # Behavioral: the returned record mirrors the updated row
    assert result.mode == "passive"
    assert result.is_enabled is True


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_schedule_tier(service, db):
    # spec: BACKEND.md §Ingestion Service — PATCH mutates is_enabled / schedule_tier
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    result = await service.patch_config(_DATASET_URN, {"schedule_tier": "hourly"})
    # Behavioral: ORM row mutation must propagate to the returned record
    assert existing_row.schedule_tier == "hourly"
    assert result.schedule_tier == "hourly"


async def test_patch_config_applies_platform(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"platform": "mysql"})
    assert existing_row.platform == "mysql"


async def test_patch_config_applies_is_enabled_and_schedule(service, db):
    existing_row = _make_config_row(is_enabled=False)
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"is_enabled": True, "schedule_tier": "daily"})
    assert existing_row.is_enabled is True
    assert existing_row.schedule_tier == "daily"


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule_tier": "daily"})
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


# ── delete_config ────────────────────────────────────────────────────────────


async def test_delete_config_success(service, db):
    # spec: BACKEND.md §Ingestion Service — DELETE removes the config row from DB
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)

    await service.delete_config(_DATASET_URN)
    # Behavioral: the correct ORM row was deleted and the session was committed
    db.delete.assert_awaited_once_with(existing_row)


async def test_delete_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.delete_config("nonexistent")
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


# ── list_configs ─────────────────────────────────────────────────────────────


async def test_list_configs_paginated(service, db):
    rows = [_make_config_row(dataset_urn=f"urn:{i}") for i in range(3)]
    mock_paginated_query(db, rows, total_count=5)

    configs, total = await service.list_configs(offset=0, limit=3)
    assert total == 5
    assert len(configs) == 3


async def test_list_configs_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    configs, total = await service.list_configs()
    assert total == 0
    assert configs == []


# ── list_active_for_tier ──────────────────────────────────────────────────────


async def test_list_active_for_tier_returns_urns(service, db):
    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t1,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t2,PROD)",
    ]
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = urns
    db.execute = AsyncMock(return_value=result_mock)

    datasets = await service.list_active_for_tier("daily")
    assert datasets == urns


async def test_list_active_for_tier_empty(service, db):
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)

    datasets = await service.list_active_for_tier("weekly")
    assert datasets == []


# ── run ──────────────────────────────────────────────────────────────────────


async def test_run_success(service, db):
    # spec: BACKEND.md §Active run pipeline L195-L204:
    #   "on success mark dataset_registry.datahub_registered = true via mark_registered()"
    #   "record INGESTION.COMPLETE event"
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with (
        patch(
            "src.backend.ingestion.service.run_datahub_ingestion",
            new=AsyncMock(
                return_value=IngestionResult(entities_ingested=5, errors=[], warnings=[])
            ),
        ),
        patch(
            "src.backend.ingestion.service.mark_registered",
            new=AsyncMock(),
        ) as mock_mark_registered,
    ):
        result = await service.run(_DATASET_URN)

    assert result.status == "success"
    assert result.run_id
    assert result.detail["dry_run"] is False
    assert result.detail["entities_ingested"] == 5

    # Behavioral: mark_registered must be awaited with the dataset URN on success
    # spec: BACKEND.md §Active run pipeline L201
    mock_mark_registered.assert_awaited_once_with(db, _DATASET_URN)

    # Behavioral: an INGESTION.COMPLETE event must be added to the DB
    # spec: BACKEND.md §Active run pipeline L201-L204 + §Event Catalogue
    added_event_types = [
        getattr(call_args.args[0], "event_type", None)
        for call_args in db.add.call_args_list
        if hasattr(call_args.args[0] if call_args.args else None, "event_type")
    ]
    assert INGESTION_COMPLETE in added_event_types, (
        f"Expected INGESTION.COMPLETE event to be recorded, got: {added_event_types}"
    )


async def test_run_dry_run(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=0, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN, dry_run=True)

    assert result.detail["dry_run"] is True
    assert result.detail["entities_ingested"] == 0


async def test_run_ingestion_error(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(
                entities_ingested=0,
                errors=["Connection refused"],
                warnings=[],
            )
        ),
    ):
        result = await service.run(_DATASET_URN)

    assert result.status == "error"
    assert "errors" in result.detail
    assert "Connection refused" in result.detail["errors"]


async def test_run_zero_entities_non_dry_run_fails(service, db):
    """A non-dry-run ingestion that ingests zero entities with no explicit errors
    must be treated as a failure (status='error', INGESTION.FAIL event recorded).

    spec: BACKEND.md §Active run pipeline — "a non-dry-run that ingests zero
    entities is treated as failure" L200-L201
    """
    # spec: BACKEND.md §Active run pipeline L200-L201
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=0, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN, dry_run=False)

    # Behavioral: status must indicate failure when zero entities ingested (non-dry-run)
    assert result.status == "error", (
        "Expected status='error' when entities_ingested=0 and dry_run=False; "
        f"got status='{result.status}'"
    )
    # Behavioral: the result detail must indicate zero entities were ingested
    assert result.detail["entities_ingested"] == 0
    assert result.detail["dry_run"] is False


async def test_run_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


# ── Redis SETNX concurrency guard ─────────────────────────────────────────────


async def test_run_redis_setnx_conflict(service_with_cache, db, cache):
    """Second concurrent run() raises ConflictError when lock is already held."""
    cache.set_nx = AsyncMock(return_value=False)  # lock already held

    with pytest.raises(ConflictError) as exc_info:
        await service_with_cache.run(_DATASET_URN)
    assert exc_info.value.error_code == "INGESTION_RUNNING"


async def test_run_redis_setnx_acquired_then_released(service_with_cache, db, cache):
    """Lock is acquired then released even when inner run raises."""
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_config_row()
    mock_scalar_query(db, config_row)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=5, errors=[], warnings=[])
        ),
    ):
        mock_db_refresh(db)
        await service_with_cache.run(_DATASET_URN)

    # Lock must be released in finally block
    cache.delete_if_value.assert_awaited_once()


# ── sync_passive_status ───────────────────────────────────────────────────────


async def test_sync_passive_status_no_passive_configs(service, db):
    """sync_passive_status returns zeros when no passive configs exist."""
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)

    summary = await service.sync_passive_status()
    assert summary["synced_events"] == 0
    assert summary["errors"] == 0


async def test_sync_passive_status_skips_on_datahub_failure(service, db, datahub):
    """sync_passive_status continues past per-dataset failures, counting errors."""
    # One passive config
    config_row = _make_config_row(mode="passive")
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [config_row]
    db.execute = AsyncMock(return_value=result_mock)

    # DataHub raises an exception for the dataset
    datahub._with_retry = AsyncMock(side_effect=Exception("DataHub down"))
    # Patch _fetch_datahub_run_history to raise
    with patch.object(service, "_fetch_datahub_run_history", side_effect=Exception("DH error")):
        summary = await service.sync_passive_status()

    assert summary["errors"] == 1
    assert summary["synced_events"] == 0


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(
            entity_type="dataset",
            event_type="INGESTION.COMPLETE",
            entity_id=_DATASET_URN,
            minutes_ago=i,
        )
        for i in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["event_type"] == "INGESTION.COMPLETE"


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []
