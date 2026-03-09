"""Unit tests for DatasetService (mocked infrastructure)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.dataset.service import DatasetService, _parse_platform
from src.shared.exceptions import EntityNotFoundError

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


def _make_props(name: str = "public.users", description: str = "User table"):
    props = MagicMock()
    props.name = name
    props.description = description
    props.customProperties = {}
    return props


def _make_ownership(owner_urns: list[str]):
    ownership = MagicMock()
    owners = []
    for urn in owner_urns:
        o = MagicMock()
        o.owner = urn
        owners.append(o)
    ownership.owners = owners
    return ownership


def _make_tags(tag_urns: list[str]):
    tags_obj = MagicMock()
    tags = []
    for urn in tag_urns:
        t = MagicMock()
        t.tag = urn
        tags.append(t)
    tags_obj.tags = tags
    return tags_obj


def _make_schema(field_paths: list[str]):
    schema = MagicMock()
    fields = []
    for fp in field_paths:
        f = MagicMock()
        f.fieldPath = fp
        fields.append(f)
    schema.fields = fields
    return schema


def _make_event_row(
    entity_id: str = _DATASET_URN,
    event_type: str = "ingestion.completed",
    status: str = "success",
    minutes_ago: int = 5,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.entity_type = "dataset"
    row.entity_id = entity_id
    row.event_type = event_type
    row.status = status
    row.detail = {"source": "test"}
    row.occurred_at = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return row


@pytest.fixture
def datahub():
    return AsyncMock()


@pytest.fixture
def cache():
    return AsyncMock()


@pytest.fixture
def db():
    return AsyncMock()


@pytest.fixture
def service(datahub, db, cache):
    return DatasetService(datahub=datahub, db=db, cache=cache)


# ── parse_platform ────────────────────────────────────────────────────────────


def test_parse_platform_postgres():
    assert _parse_platform(_DATASET_URN) == "postgres"


def test_parse_platform_unknown():
    assert _parse_platform("urn:li:dataset:bad") == "unknown"


# ── get_summary ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_summary_returns_dataset(service, datahub):
    props = _make_props()
    ownership = _make_ownership(["urn:li:corpuser:alice@example.com"])
    tags = _make_tags(["urn:li:tag:pii"])

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        if "Ownership" in name:
            return ownership
        if "GlobalTags" in name:
            return tags
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    result = await service.get_summary(_DATASET_URN)
    assert result.urn == _DATASET_URN
    assert result.name == "public.users"
    assert result.platform == "postgres"
    assert result.description == "User table"
    assert result.owners == ["urn:li:corpuser:alice@example.com"]
    assert result.tags == ["urn:li:tag:pii"]


@pytest.mark.asyncio
async def test_get_summary_missing_optional_aspects(service, datahub):
    props = _make_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    result = await service.get_summary(_DATASET_URN)
    assert result.owners == []
    assert result.tags == []


@pytest.mark.asyncio
async def test_get_summary_not_found(service, datahub):
    datahub.get_aspect = AsyncMock(return_value=None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_summary(_DATASET_URN)
    assert exc_info.value.error_code == "DATASET_NOT_FOUND"


# ── get_attributes ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_attributes_with_schema_and_quality(service, datahub, cache):
    props = _make_props()
    ownership = _make_ownership(["urn:li:corpuser:bob@example.com"])
    tags = _make_tags(["urn:li:tag:finance"])
    schema = _make_schema(["id", "name", "email"])

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        if "Ownership" in name:
            return ownership
        if "GlobalTags" in name:
            return tags
        if "SchemaMetadata" in name:
            return schema
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    quality_json = json.dumps({"overall_score": 0.95, "dimensions": {"completeness": 0.9}})
    cache.get = AsyncMock(return_value=quality_json)

    result = await service.get_attributes(_DATASET_URN)
    assert result.column_count == 3
    assert result.fields == ["id", "name", "email"]
    assert result.quality_score is not None
    assert result.quality_score.overall_score == 0.95
    assert result.quality_score.dimensions == {"completeness": 0.9}


@pytest.mark.asyncio
async def test_get_attributes_quality_cache_miss(service, datahub, cache):
    props = _make_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)
    cache.get = AsyncMock(return_value=None)

    result = await service.get_attributes(_DATASET_URN)
    assert result.quality_score is None


@pytest.mark.asyncio
async def test_get_attributes_no_schema(service, datahub, cache):
    props = _make_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)
    cache.get = AsyncMock(return_value=None)

    result = await service.get_attributes(_DATASET_URN)
    assert result.column_count == 0
    assert result.fields == []


@pytest.mark.asyncio
async def test_get_attributes_not_found(service, datahub):
    datahub.get_aspect = AsyncMock(return_value=None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_attributes(_DATASET_URN)
    assert exc_info.value.error_code == "DATASET_NOT_FOUND"


# ── get_events ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_returns_paginated(service, db):
    rows = [_make_event_row(minutes_ago=i) for i in range(3)]

    # Mock for count query
    count_result = MagicMock()
    count_result.scalar.return_value = 5

    # Mock for rows query
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0].entity_type == "dataset"
    assert events[0].entity_id == _DATASET_URN


@pytest.mark.asyncio
async def test_get_events_empty(service, db):
    count_result = MagicMock()
    count_result.scalar.return_value = 0

    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []


@pytest.mark.asyncio
async def test_get_events_with_time_range(service, db):
    count_result = MagicMock()
    count_result.scalar.return_value = 0

    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    from_dt = datetime(2025, 1, 1, tzinfo=UTC)
    to_dt = datetime(2025, 12, 31, tzinfo=UTC)

    events, total = await service.get_events(_DATASET_URN, from_dt=from_dt, to_dt=to_dt)
    assert total == 0
    # Verify execute was called twice (count + rows)
    assert db.execute.call_count == 2


@pytest.mark.asyncio
async def test_get_events_offset_limit(service, db):
    count_result = MagicMock()
    count_result.scalar.return_value = 20

    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    events, total = await service.get_events(_DATASET_URN, offset=10, limit=5)
    assert total == 20
    assert db.execute.call_count == 2
