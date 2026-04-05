"""Concept CRUD and approve/reject models."""

from datetime import datetime

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import ConceptStatus, RelationshipType


class CreateConceptRequest(BaseModel):
    name: str = Field(description="Unique human-readable name for the concept, e.g. 'Customer Lifetime Value'")
    description: str = Field(description="Detailed description of what this concept means in the business domain")
    parent_id: str | None = Field(default=None, description="Identifier of the parent concept for hierarchical ontology structures")


class PatchConceptRequest(BaseModel):
    name: str | None = Field(default=None, description="Updated concept name")
    description: str | None = Field(default=None, description="Updated concept description")
    parent_id: str | None = Field(default=None, description="Updated parent concept identifier")
    status: ConceptStatus | None = Field(default=None, description="Updated concept status: 'pending', 'approved', or 'rejected'")


class ConceptResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the concept")
    name: str = Field(description="Human-readable concept name")
    description: str = Field(description="Description of the concept in the business domain")
    parent_id: str | None = Field(description="Identifier of the parent concept, null if this is a root concept")
    status: ConceptStatus = Field(description="Review status of the concept: 'pending' (awaiting approval), 'approved', or 'rejected'")
    version: int = Field(description="Version number incremented on each update")
    created_at: datetime = Field(description="UTC timestamp when the concept was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class ConceptListResponse(PaginatedResponse):
    concepts: list[ConceptResponse] = Field(default=[], description="Page of concept records")


class ConceptRelationshipResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the relationship")
    concept_a: str = Field(description="Identifier of the first concept in the relationship")
    concept_b: str = Field(description="Identifier of the second concept in the relationship")
    relationship_type: RelationshipType = Field(description="Semantic relationship between the two concepts: 'related_to', 'part_of', 'depends_on', or 'overlaps_with'")
    confidence_score: float = Field(description="Confidence score (0.0–1.0) for the inferred relationship")
    created_at: datetime = Field(description="UTC timestamp when the relationship was created")


class ConceptAttrResponse(SingleResponse):
    concept_id: str = Field(description="Identifier of the concept these attributes belong to")
    dataset_count: int = Field(description="Number of datasets linked to this concept")
    avg_confidence: float = Field(description="Average confidence score across all dataset linkages")
    relationships: list[ConceptRelationshipResponse] = Field(default=[], description="List of relationships this concept has with other concepts")
    children: list[ConceptResponse] = Field(default=[], description="Direct child concepts in the ontology hierarchy")
