from datetime import datetime

from pydantic import BaseModel


class Concept(BaseModel):
    id: str
    name: str
    description: str
    parent_id: str | None = None
    status: str = "pending"  # "approved", "pending", "rejected"
    version: int = 1
    created_at: datetime
    updated_at: datetime


class ConceptRelationship(BaseModel):
    id: str
    concept_a: str
    concept_b: str
    relationship_type: str  # "related_to", "part_of", "depends_on", "overlaps_with"
    confidence_score: float
    created_at: datetime
