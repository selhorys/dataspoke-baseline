"""Unit tests for IngestionService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import EntityNotFoundError
from tests.unit.backend.conftest import make_event_row, mock_paginated_query, mock_scalar_query

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    sources: dict | None = None,
    deep_spec_enabled: bool = False,
    schedule: str | None = "0 0 * * *",
    status: str = "draft",
    owner: str = "alice@example.com",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.sources = sources or {"sql_log": {"queries": ["SELECT 1"]}}
    row.deep_spec_enabled = deep_spec_enabled
    row.schedule = schedule
    row.status = status
    row.owner = owner
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db, llm):
    return IngestionService(datahub=datahub, db=db, llm=llm)


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
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        sources={"sql_log": {"queries": ["SELECT 1"]}},
        deep_spec_enabled=False,
        schedule=None,
        owner="alice@example.com",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


async def test_upsert_config_updates_existing(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        sources={"github": {"owner": "o", "repo": "r", "token": "t"}},
        deep_spec_enabled=True,
        schedule="0 6 * * *",
        owner="bob@example.com",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    assert existing_row.sources == {"github": {"owner": "o", "repo": "r", "token": "t"}}
    assert existing_row.owner == "bob@example.com"


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_partial(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    await service.patch_config(_DATASET_URN, {"schedule": "0 12 * * *"})
    assert existing_row.schedule == "0 12 * * *"
    db.commit.assert_awaited_once()


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule": "0 12 * * *"})
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


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


# ── run ──────────────────────────────────────────────────────────────────────


async def test_run_success(service, db, datahub):
    config_row = _make_config_row(
        sources={"sql_log": {"queries": ["SELECT * FROM orders.order_header"]}}
    )
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    result = await service.run(_DATASET_URN)
    assert result.status in ("success", "partial")
    assert result.run_id
    assert result.detail["dry_run"] is False


async def test_run_dry_run(service, db, datahub):
    config_row = _make_config_row(
        sources={"sql_log": {"queries": ["SELECT * FROM catalog.title_master"]}}
    )
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    result = await service.run(_DATASET_URN, dry_run=True)
    assert result.detail["dry_run"] is True
    datahub.emit_aspect.assert_not_awaited()


async def test_run_with_llm_enrichment(service, db, datahub, llm):
    config_row = _make_config_row(
        deep_spec_enabled=True,
        sources={"sql_log": {"queries": ["SELECT * FROM catalog.title_master"]}},
    )
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    llm.complete_json = AsyncMock(
        return_value={"description": "Enriched desc", "tags": ["important"]}
    )

    result = await service.run(_DATASET_URN)
    assert result.status in ("success", "partial")
    llm.complete_json.assert_awaited_once()


async def test_run_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    assert exc_info.value.error_code == "INGESTION_CONFIG_NOT_FOUND"


async def test_run_extractor_partial_failure(service, db, datahub):
    config_row = _make_config_row(
        sources={
            "sql_log": {"queries": ["SELECT * FROM orders.order_header"]},
            "unknown_type": {"foo": "bar"},
        }
    )
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    result = await service.run(_DATASET_URN)
    assert result.status == "partial"
    assert "extractor_errors" in result.detail


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(
            entity_type="ingestion",
            event_type="ingestion.completed",
            entity_id=_DATASET_URN,
            minutes_ago=i,
        )
        for i in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "ingestion"


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []
