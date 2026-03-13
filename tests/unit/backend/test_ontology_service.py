"""Unit tests for OntologyService (mocked infrastructure)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.ontology.service import OntologyService, _status_for_confidence
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import (
    make_concept_row,
    make_event_row,
    make_relationship_row,
    mock_paginated_query,
    mock_scalar_query,
)


@pytest.fixture
def service(db):
    return OntologyService(db=db)


# ── list_concepts ────────────────────────────────────────────────────────────


async def test_list_concepts_returns_paginated(service, db):
    rows = [make_concept_row(name=f"concept_{i}") for i in range(3)]
    mock_paginated_query(db, rows, total_count=5)

    concepts, total = await service.list_concepts(offset=0, limit=3)
    assert total == 5
    assert len(concepts) == 3


async def test_list_concepts_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    concepts, total = await service.list_concepts()
    assert total == 0
    assert concepts == []


# ── get_concept ──────────────────────────────────────────────────────────────


async def test_get_concept_found(service, db):
    concept_row = make_concept_row(name="customer")
    mock_scalar_query(db, concept_row)

    concept = await service.get_concept(str(concept_row.id))
    assert concept.name == "customer"
    assert concept.id == str(concept_row.id)


async def test_get_concept_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_concept(str(uuid.uuid4()))
    assert exc_info.value.error_code == "CONCEPT_CATEGORY_NOT_FOUND"


# ── get_concept_attr ─────────────────────────────────────────────────────────


async def test_get_concept_attr(service, db):
    concept_row = make_concept_row()
    concept_id = concept_row.id

    rel_row = make_relationship_row(concept_a=concept_id)
    child_row = make_concept_row(name="child_concept", parent_id=concept_id)

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


async def test_get_concept_events(service, db):
    concept_id = str(uuid.uuid4())
    rows = [
        make_event_row(
            entity_type="ontology",
            event_type="concept.approved",
            entity_id=concept_id,
            minutes_ago=i,
        )
        for i in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_concept_events(concept_id, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "ontology"


# ── approve ──────────────────────────────────────────────────────────────────


async def test_approve_pending_concept(service, db):
    concept_row = make_concept_row(status="pending", version=1)
    mock_scalar_query(db, concept_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    concept = await service.approve(str(concept_row.id))
    assert concept.status == "approved"
    assert concept.version == 2
    # commit called: once for approve, once for event
    assert db.commit.await_count == 2


async def test_approve_already_approved_raises(service, db):
    concept_row = make_concept_row(status="approved")
    mock_scalar_query(db, concept_row)

    with pytest.raises(ConflictError) as exc_info:
        await service.approve(str(concept_row.id))
    assert exc_info.value.error_code == "INVALID_STATUS_TRANSITION"


# ── reject ───────────────────────────────────────────────────────────────────


async def test_reject_pending_concept(service, db):
    concept_row = make_concept_row(status="pending", version=1)
    mock_scalar_query(db, concept_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    concept = await service.reject(str(concept_row.id))
    assert concept.status == "rejected"
    # Version should NOT be bumped on rejection
    assert concept.version == 1
    assert db.commit.await_count == 2


async def test_reject_already_rejected_raises(service, db):
    concept_row = make_concept_row(status="rejected")
    mock_scalar_query(db, concept_row)

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
