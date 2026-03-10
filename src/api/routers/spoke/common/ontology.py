from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_ontology_service
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ontology import (
    ConceptAttrResponse,
    ConceptListResponse,
    ConceptRelationshipResponse,
    ConceptResponse,
)
from src.backend.ontology.service import OntologyService

router = APIRouter(
    prefix="/ontology",
    tags=["common/ontology"],
    dependencies=[Depends(require_common)],
)


def _concept_response(c) -> ConceptResponse:  # noqa: ANN001
    return ConceptResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),
        name=c.name,
        description=c.description,
        parent_id=c.parent_id,
        status=c.status,
        version=c.version,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("", response_model=ConceptListResponse)
async def list_ontology_concepts(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service: OntologyService = Depends(get_ontology_service),
) -> ConceptListResponse:
    concepts, total_count = await service.list_concepts(offset=offset, limit=limit)
    return ConceptListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        concepts=[_concept_response(c) for c in concepts],
    )


@router.get("/{concept_id}", response_model=ConceptResponse)
async def get_concept(
    concept_id: str,
    service: OntologyService = Depends(get_ontology_service),
) -> ConceptResponse:
    concept = await service.get_concept(concept_id)
    return _concept_response(concept)


@router.get("/{concept_id}/attr", response_model=ConceptAttrResponse)
async def get_concept_attr(
    concept_id: str,
    service: OntologyService = Depends(get_ontology_service),
) -> ConceptAttrResponse:
    attr = await service.get_concept_attr(concept_id)
    return ConceptAttrResponse(
        concept_id=concept_id,
        dataset_count=attr.dataset_count,
        avg_confidence=attr.avg_confidence,
        relationships=[
            ConceptRelationshipResponse(
                id=r.id,
                concept_a=r.concept_a,
                concept_b=r.concept_b,
                relationship_type=r.relationship_type,
                confidence_score=r.confidence_score,
                created_at=r.created_at,
            )
            for r in attr.relationships
        ],
        children=[_concept_response(c) for c in attr.children],
    )


@router.get("/{concept_id}/event", response_model=EventListResponse)
async def get_concept_events(
    concept_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service: OntologyService = Depends(get_ontology_service),
) -> EventListResponse:
    events, total_count = await service.get_concept_events(concept_id, offset=offset, limit=limit)
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=e["id"],
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e["detail"],
                occurred_at=e["occurred_at"],
            )
            for e in events
        ],
    )


@router.post("/{concept_id}/method/approve", response_model=ConceptResponse)
async def approve_concept(
    concept_id: str,
    service: OntologyService = Depends(get_ontology_service),
) -> ConceptResponse:
    concept = await service.approve(concept_id)
    return _concept_response(concept)


@router.post("/{concept_id}/method/reject", response_model=ConceptResponse)
async def reject_concept(
    concept_id: str,
    service: OntologyService = Depends(get_ontology_service),
) -> ConceptResponse:
    concept = await service.reject(concept_id)
    return _concept_response(concept)
