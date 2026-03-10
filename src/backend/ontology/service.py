"""Ontology service — concept CRUD, approve/reject workflow, and events."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import ONTOLOGY_CONFIDENCE_THRESHOLD
from src.shared.db.models import ConceptCategory, ConceptRelationship, DatasetConceptMap, Event
from src.shared.exceptions import ConflictError, EntityNotFoundError


class ConceptRecord:
    """Value object mirroring the ORM ConceptCategory."""

    __slots__ = (
        "id",
        "name",
        "description",
        "parent_id",
        "status",
        "version",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        parent_id: str | None,
        status: str,
        version: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.parent_id = parent_id
        self.status = status
        self.version = version
        self.created_at = created_at
        self.updated_at = updated_at


class ConceptRelationshipRecord:
    """Value object mirroring the ORM ConceptRelationship."""

    __slots__ = (
        "id",
        "concept_a",
        "concept_b",
        "relationship_type",
        "confidence_score",
        "created_at",
    )

    def __init__(
        self,
        id: str,
        concept_a: str,
        concept_b: str,
        relationship_type: str,
        confidence_score: float,
        created_at: datetime,
    ) -> None:
        self.id = id
        self.concept_a = concept_a
        self.concept_b = concept_b
        self.relationship_type = relationship_type
        self.confidence_score = confidence_score
        self.created_at = created_at


class ConceptAttr:
    """Aggregated attributes for a concept."""

    __slots__ = ("dataset_count", "avg_confidence", "relationships", "children")

    def __init__(
        self,
        dataset_count: int,
        avg_confidence: float,
        relationships: list[ConceptRelationshipRecord],
        children: list[ConceptRecord],
    ) -> None:
        self.dataset_count = dataset_count
        self.avg_confidence = avg_confidence
        self.relationships = relationships
        self.children = children


def _concept_from_row(row: ConceptCategory) -> ConceptRecord:
    return ConceptRecord(
        id=str(row.id),
        name=row.name,
        description=row.description,
        parent_id=str(row.parent_id) if row.parent_id else None,
        status=row.status,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _relationship_from_row(row: ConceptRelationship) -> ConceptRelationshipRecord:
    return ConceptRelationshipRecord(
        id=str(row.id),
        concept_a=str(row.concept_a),
        concept_b=str(row.concept_b),
        relationship_type=row.relationship_type,
        confidence_score=row.confidence_score,
        created_at=row.created_at,
    )


def _status_for_confidence(score: float) -> str:
    """Return auto-approval status based on confidence threshold."""
    if score >= ONTOLOGY_CONFIDENCE_THRESHOLD:
        return "approved"
    return "pending"


class OntologyService:
    """Concept CRUD, approve/reject workflow, and event recording."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Concept CRUD ─────────────────────────────────────────────────────

    async def list_concepts(
        self,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ConceptRecord], int]:
        base = select(ConceptCategory)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(ConceptCategory.name).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_concept_from_row(r) for r in rows], total_count

    async def get_concept(self, concept_id: str) -> ConceptRecord:
        result = await self._db.execute(
            select(ConceptCategory).where(ConceptCategory.id == uuid.UUID(concept_id))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("concept_category", concept_id)
        return _concept_from_row(row)

    async def get_concept_attr(self, concept_id: str) -> ConceptAttr:
        # Verify concept exists
        await self.get_concept(concept_id)
        uid = uuid.UUID(concept_id)

        # Dataset mappings: count + avg confidence
        map_q = select(
            func.count().label("cnt"),
            func.coalesce(func.avg(DatasetConceptMap.confidence_score), 0.0).label("avg_conf"),
        ).where(DatasetConceptMap.concept_id == uid)
        map_result = await self._db.execute(map_q)
        map_row = map_result.one()
        dataset_count = map_row.cnt
        avg_confidence = float(map_row.avg_conf)

        # Relationships where this concept is either side
        rel_q = select(ConceptRelationship).where(
            (ConceptRelationship.concept_a == uid) | (ConceptRelationship.concept_b == uid)
        )
        rel_result = await self._db.execute(rel_q)
        rel_rows = rel_result.scalars().all()

        # Children
        children_q = select(ConceptCategory).where(ConceptCategory.parent_id == uid)
        children_result = await self._db.execute(children_q)
        children_rows = children_result.scalars().all()

        return ConceptAttr(
            dataset_count=dataset_count,
            avg_confidence=avg_confidence,
            relationships=[_relationship_from_row(r) for r in rel_rows],
            children=[_concept_from_row(c) for c in children_rows],
        )

    # ── Events ───────────────────────────────────────────────────────────

    async def get_concept_events(
        self,
        concept_id: str,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "ontology",
            Event.entity_id == concept_id,
        )

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(Event.occurred_at.desc()).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        events = [
            {
                "id": str(row.id),
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "event_type": row.event_type,
                "status": row.status,
                "detail": row.detail,
                "occurred_at": row.occurred_at,
            }
            for row in rows
        ]
        return events, total_count

    # ── Approve / Reject ─────────────────────────────────────────────────

    async def approve(self, concept_id: str) -> ConceptRecord:
        result = await self._db.execute(
            select(ConceptCategory).where(ConceptCategory.id == uuid.UUID(concept_id))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("concept_category", concept_id)

        if row.status != "pending":
            raise ConflictError(
                "INVALID_STATUS_TRANSITION",
                f"Cannot approve concept with status '{row.status}', expected 'pending'",
            )

        row.status = "approved"
        row.version += 1
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            concept_id, "concept.approved", "success", {"new_version": row.version}
        )

        return _concept_from_row(row)

    async def reject(self, concept_id: str) -> ConceptRecord:
        result = await self._db.execute(
            select(ConceptCategory).where(ConceptCategory.id == uuid.UUID(concept_id))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("concept_category", concept_id)

        if row.status != "pending":
            raise ConflictError(
                "INVALID_STATUS_TRANSITION",
                f"Cannot reject concept with status '{row.status}', expected 'pending'",
            )

        row.status = "rejected"
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(concept_id, "concept.rejected", "success", {})

        return _concept_from_row(row)

    # ── Internal ─────────────────────────────────────────────────────────

    async def _record_event(
        self,
        concept_id: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = Event(
            entity_type="ontology",
            entity_id=concept_id,
            event_type=event_type,
            status=status,
            detail=detail,
            occurred_at=datetime.now(tz=UTC),
        )
        self._db.add(event)
        await self._db.commit()
