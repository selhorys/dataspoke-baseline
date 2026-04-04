"""Unit tests for IngestionService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import EntityNotFoundError
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
    source_type: str = "POSTGRESQL",
    locator: dict | None = None,
    identifier: dict | None = None,
    auth: dict | None = None,
    is_active: bool = False,
    schedule_cron: str | None = "0 0 * * *",
    enrichment_sources: dict | None = None,
    custom_extractors: dict | None = None,
    kestra_flow_namespace: str | None = None,
    kestra_flow_id: str | None = None,
    status: str = "OK",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.source_type = source_type
    row.locator = locator or _LOCATOR
    row.identifier = identifier or _IDENTIFIER
    row.auth = auth if auth is not None else _AUTH
    row.is_active = is_active
    row.schedule_cron = schedule_cron
    row.enrichment_sources = enrichment_sources
    row.custom_extractors = custom_extractors
    row.kestra_flow_namespace = kestra_flow_namespace
    row.kestra_flow_id = kestra_flow_id
    row.status = status
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db):
    return IngestionService(datahub=datahub, db=db)


# ── get_config ───────────────────────────────────────────────────────────────


async def test_get_config_found(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)

    config = await service.get_config(_DATASET_URN)
    assert config is not None
    assert config.dataset_urn == _DATASET_URN
    assert config.source_type == "POSTGRESQL"
    assert config.locator == _LOCATOR
    assert config.identifier == _IDENTIFIER
    assert config.auth == _AUTH


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
        source_type="POSTGRESQL",
        locator=_LOCATOR,
        identifier=_IDENTIFIER,
        auth=_AUTH,
        is_active=False,
        schedule_cron=None,
    )
    assert db.add.called
    assert db.commit.await_count >= 1


async def test_upsert_config_updates_existing(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    new_locator = {"host": "newdb.example.com", "port": 5432}
    new_identifier = {"database": "newdb", "schema_name": "public", "table": "orders"}
    new_auth = {"username": "admin", "secret_ref": "newpw"}
    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        source_type="MYSQL",
        locator=new_locator,
        identifier=new_identifier,
        auth=new_auth,
        is_active=True,
        schedule_cron="0 6 * * *",
    )
    assert db.add.called
    assert db.commit.await_count >= 1
    assert existing_row.source_type == "MYSQL"
    assert existing_row.locator == new_locator
    assert existing_row.identifier == new_identifier
    assert existing_row.auth == new_auth
    assert existing_row.is_active is True
    assert existing_row.schedule_cron == "0 6 * * *"


async def test_upsert_config_with_optional_fields(service, db):
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    enrichment = {"confluence": {"base_url": "https://wiki.example.com"}}
    custom = {"plugin_a": {"class": "mymodule.MyExtractor"}}
    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        source_type="POSTGRESQL",
        locator=_LOCATOR,
        identifier=_IDENTIFIER,
        auth=_AUTH,
        is_active=False,
        schedule_cron=None,
        enrichment_sources=enrichment,
        custom_extractors=custom,
    )
    assert db.add.called


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_schedule(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"schedule_cron": "0 12 * * *"})
    assert existing_row.schedule_cron == "0 12 * * *"
    assert db.commit.await_count >= 1


async def test_patch_config_applies_source_type(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"source_type": "MYSQL"})
    assert existing_row.source_type == "MYSQL"


async def test_patch_config_applies_periodic_and_schedule(service, db):
    existing_row = _make_config_row(is_active=False)
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"is_active": True, "schedule_cron": "0 2 * * *"})
    assert existing_row.is_active is True
    assert existing_row.schedule_cron == "0 2 * * *"



async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule_cron": "0 12 * * *"})
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


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


# ── list_periodic_datasets ────────────────────────────────────────────────────


async def test_list_periodic_datasets_returns_urns(service, db):
    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t1,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t2,PROD)",
    ]
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = urns
    db.execute = AsyncMock(return_value=result_mock)

    datasets = await service.list_periodic_datasets("0 2 * * *")
    assert datasets == urns


async def test_list_periodic_datasets_empty(service, db):
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)

    datasets = await service.list_periodic_datasets("0 2 * * *")
    assert datasets == []


# ── run ──────────────────────────────────────────────────────────────────────


async def test_run_success(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=5, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN)

    assert result.status == "success"
    assert result.run_id
    assert result.detail["dry_run"] is False
    assert result.detail["entities_ingested"] == 5


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


async def test_run_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


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
