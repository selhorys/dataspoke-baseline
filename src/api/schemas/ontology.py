"""Concept CRUD and approve/reject models."""

from datetime import datetime

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateConceptRequest(BaseModel):
    name: str
    description: str
    parent_id: str | None = None


class PatchConceptRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_id: str | None = None
    status: str | None = None


class ConceptResponse(SingleResponse):
    id: str
    name: str
    description: str
    parent_id: str | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class ConceptListResponse(PaginatedResponse):
    concepts: list[ConceptResponse] = []


class ConceptRelationshipResponse(SingleResponse):
    id: str
    concept_a: str
    concept_b: str
    relationship_type: str
    confidence_score: float
    created_at: datetime
