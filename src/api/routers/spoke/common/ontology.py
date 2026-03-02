from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth.dependencies import require_common

router = APIRouter(
    prefix="/ontology",
    tags=["common/ontology"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("")
async def list_ontology_concepts() -> None:
    raise _501


@router.get("/{concept_id}")
async def get_concept(concept_id: str) -> None:
    raise _501


@router.get("/{concept_id}/attr")
async def get_concept_attr(concept_id: str) -> None:
    raise _501


@router.get("/{concept_id}/event")
async def get_concept_events(concept_id: str) -> None:
    raise _501


@router.post("/{concept_id}/method/approve")
async def approve_concept(concept_id: str) -> None:
    raise _501


@router.post("/{concept_id}/method/reject")
async def reject_concept(concept_id: str) -> None:
    raise _501
