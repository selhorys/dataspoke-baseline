"""Unit tests for OntologyService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.ontology.service import OntologyService, _status_for_confidence
from src.shared.exceptions import ConflictError, EntityNotFoundError


def _make_concept_row(
    name: str = "test_concept",
    description: str = "A test concept",
    parent_id: uuid.UUID | None = None,
    status: str = "pending",
    version: int = 1,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    row.description = description
    row.parent_id = parent_id
    row.status = status
    row.version = version
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_relationship_row(
    concept_a: uuid.UUID | None = None,
    concept_b: uuid.UUID | None = None,
    relationship_type: str = "related_to",
    confidence_score: float = 0.85,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.concept_a = concept_a or uuid.uuid4()
    row.concept_b = concept_b or uuid.uuid4()
    row.relationship_type = relationship_type
    row.confidence_score = confidence_score
    row.created_at = datetime.now(tz=UTC)
    return row


def _make_event_row(
    entity_id: str = "test-concept-id",
    event_type: str = "concept.approved",
    status: str = "success",
    minutes_ago: int = 5,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.entity_type = "ontology"
    row.entity_id = entity_id
    row.event_type = event_type
    row.status = status
    row.detail = {"source": "test"}
    row.occurred_at = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return row


@pytest.fixture
def db():
    return AsyncMock()


@pytest.fixture
def service(db):
    return OntologyService(db=db)


# ── list_concepts ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_concepts_returns_paginated(service, db):
    rows = [_make_concept_row(name=f"concept_{i}") for i in range(3)]

    count_result = MagicMock()
    count_result.scalar.return_value = 5

    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    concepts, total = await service.list_concepts(offset=0, limit=3)
    assert total == 5
    assert len(concepts) == 3


@pytest.mark.asyncio
async def test_list_concepts_empty(service, db):
    count_result = MagicMock()
    count_result.scalar.return_value = 0

    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    concepts, total = await service.list_concepts()
    assert total == 0
    assert concepts == []


# ── get_concept ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_concept_found(service, db):
    concept_row = _make_concept_row(name="customer")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = concept_row
    db.execute = AsyncMock(return_value=result_mock)

    concept = await service.get_concept(str(concept_row.id))
    assert concept.name == "customer"
    assert concept.id == str(concept_row.id)


@pytest.mark.asyncio
async def test_get_concept_not_found(service, db):
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_concept(str(uuid.uuid4()))
    assert exc_info.value.error_code == "CONCEPT_CATEGORY_NOT_FOUND"


# ── get_concept_attr ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_concept_attr(service, db):
    concept_row = _make_concept_row()
    concept_id = concept_row.id

    rel_row = _make_relationship_row(concept_a=concept_id)
    child_row = _make_concept_row(name="child_concept", parent_id=concept_id)

    # Mock for get_concept (scalar_one_or_none)
    concept_result = MagicMock()
    concept_result.scalar_one_or_none.return_value = concept_row

    # Mock for dataset map aggregate
    map_row = MagicMock()
    map_row.cnt = 3
    map_row.avg_conf = 0.82
    map_result = MagicMock()
    map_result.one.return_value = map_row

    # Mock for relationships
    rel_result = MagicMock()
    rel_result.scalars.return_value.all.return_value = [rel_row]

    # Mock for children
    children_result = MagicMock()
    children_result.scalars.return_value.all.return_value = [child_row]

    db.execute = AsyncMock(side_effect=[concept_result, map_result, rel_result, children_result])

    attr = await service.get_concept_attr(str(concept_id))
    assert attr.dataset_count == 3
    assert attr.avg_confidence == 0.82
    assert len(attr.relationships) == 1
    assert len(attr.children) == 1
    assert attr.children[0].name == "child_concept"


# ── get_concept_events ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_concept_events(service, db):
    concept_id = str(uuid.uuid4())
    rows = [_make_event_row(entity_id=concept_id, minutes_ago=i) for i in range(3)]

    count_result = MagicMock()
    count_result.scalar.return_value = 5

    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows

    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    events, total = await service.get_concept_events(concept_id, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "ontology"


# ── approve ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_pending_concept(service, db):
    concept_row = _make_concept_row(status="pending", version=1)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = concept_row
    db.execute = AsyncMock(return_value=result_mock)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    concept = await service.approve(str(concept_row.id))
    assert concept.status == "approved"
    assert concept.version == 2
    # commit called: once for approve, once for event
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_approve_already_approved_raises(service, db):
    concept_row = _make_concept_row(status="approved")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = concept_row
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(ConflictError) as exc_info:
        await service.approve(str(concept_row.id))
    assert exc_info.value.error_code == "INVALID_STATUS_TRANSITION"


# ── reject ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_pending_concept(service, db):
    concept_row = _make_concept_row(status="pending", version=1)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = concept_row
    db.execute = AsyncMock(return_value=result_mock)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    concept = await service.reject(str(concept_row.id))
    assert concept.status == "rejected"
    # Version should NOT be bumped on rejection
    assert concept.version == 1
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_reject_already_rejected_raises(service, db):
    concept_row = _make_concept_row(status="rejected")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = concept_row
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(ConflictError) as exc_info:
        await service.reject(str(concept_row.id))
    assert exc_info.value.error_code == "INVALID_STATUS_TRANSITION"


# ── confidence threshold helper ──────────────────────────────────────────────


def test_confidence_above_threshold_auto_approved():
    assert _status_for_confidence(0.7) == "approved"
    assert _status_for_confidence(0.95) == "approved"


def test_confidence_below_threshold_pending():
    assert _status_for_confidence(0.69) == "pending"
    assert _status_for_confidence(0.0) == "pending"
