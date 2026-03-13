"""Unit tests for DatasetService (mocked infrastructure)."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.backend.dataset.service import DatasetService, _parse_platform
from src.shared.exceptions import EntityNotFoundError
from tests.unit.backend.conftest import (
    make_datahub_ownership,
    make_datahub_props,
    make_datahub_schema,
    make_datahub_tags,
    make_event_row,
    mock_paginated_query,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


@pytest.fixture
def service(datahub, db, cache):
    return DatasetService(datahub=datahub, db=db, cache=cache)


# ── parse_platform ────────────────────────────────────────────────────────────


def test_parse_platform_postgres():
    assert _parse_platform(_DATASET_URN) == "postgres"


def test_parse_platform_unknown():
    assert _parse_platform("urn:li:dataset:bad") == "unknown"


# ── get_summary ───────────────────────────────────────────────────────────────


async def test_get_summary_returns_dataset(service, datahub):
    props = make_datahub_props()
    ownership = make_datahub_ownership(["urn:li:corpuser:alice@example.com"])
    tags = make_datahub_tags(["urn:li:tag:pii"])

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


async def test_get_summary_missing_optional_aspects(service, datahub):
    props = make_datahub_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    result = await service.get_summary(_DATASET_URN)
    assert result.owners == []
    assert result.tags == []


async def test_get_summary_not_found(service, datahub):
    datahub.get_aspect = AsyncMock(return_value=None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_summary(_DATASET_URN)
    assert exc_info.value.error_code == "DATASET_NOT_FOUND"


# ── get_attributes ────────────────────────────────────────────────────────────


async def test_get_attributes_with_schema_and_quality(service, datahub, cache):
    props = make_datahub_props()
    ownership = make_datahub_ownership(["urn:li:corpuser:bob@example.com"])
    tags = make_datahub_tags(["urn:li:tag:finance"])
    schema = make_datahub_schema(["id", "name", "email"])

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


async def test_get_attributes_quality_cache_miss(service, datahub, cache):
    props = make_datahub_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)
    cache.get = AsyncMock(return_value=None)

    result = await service.get_attributes(_DATASET_URN)
    assert result.quality_score is None


async def test_get_attributes_no_schema(service, datahub, cache):
    props = make_datahub_props()

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


async def test_get_attributes_not_found(service, datahub):
    datahub.get_aspect = AsyncMock(return_value=None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_attributes(_DATASET_URN)
    assert exc_info.value.error_code == "DATASET_NOT_FOUND"


# ── get_events ────────────────────────────────────────────────────────────────


async def test_get_events_returns_paginated(service, db):
    rows = [
        make_event_row(entity_type="dataset", entity_id=_DATASET_URN, minutes_ago=i)
        for i in range(3)
    ]
    mock_paginated_query(db, rows, 5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0].entity_type == "dataset"
    assert events[0].entity_id == _DATASET_URN


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], 0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []


async def test_get_events_with_time_range(service, db):
    mock_paginated_query(db, [], 0)

    from_dt = datetime(2025, 1, 1, tzinfo=UTC)
    to_dt = datetime(2025, 12, 31, tzinfo=UTC)

    events, total = await service.get_events(_DATASET_URN, from_dt=from_dt, to_dt=to_dt)
    assert total == 0
    # Verify execute was called twice (count + rows)
    assert db.execute.call_count == 2


async def test_get_events_offset_limit(service, db):
    mock_paginated_query(db, [], 20)

    events, total = await service.get_events(_DATASET_URN, offset=10, limit=5)
    assert total == 20
    assert db.execute.call_count == 2
