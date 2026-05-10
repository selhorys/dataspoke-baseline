"""Domain Pydantic models for Ontology Generation (UC3).

These are internal domain objects shared between the backend service layer and
the shared client layer. API request/response schemas live in
``src/api/schemas/ontogen.py`` (to be written in Pass 3).

Column shapes match spec/feature/BACKEND_SCHEMA.md exactly.
"""

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Slug pattern: lowercase letters, digits, hyphens, underscores only.
# Must match the _SLUG_RE used in src/shared/graph/client.py and the DB CHECK
# constraints so the API/DB/graph layers agree on a single valid shape.
_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


def _validate_slug(value: str, field_name: str) -> str:
    """Raise ValueError if *value* contains __ or violates the slug regex."""
    if "__" in value:
        raise ValueError(f"{field_name} must not contain '__' (reserved as triple-ID separator)")
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"{field_name} must match ^[a-z0-9_-]+$ (got {value!r})"
        )
    return value


class OntogenConf(BaseModel):
    """Singleton Ontology Generation configuration row (``ontogen_config``)."""

    id: int = 1
    is_enabled: bool = False
    schedule_tier: str | None = None
    dataset_filter: dict[str, Any] = Field(default_factory=dict)
    default_run_prompt: str | None = None
    updated_at: datetime


class OntogenSeed(BaseModel):
    """Human-authored Markdown seed document (``ontogen_seeds``)."""

    id: str  # UUID as string
    body_md: str
    status: str = "active"  # "active" | "retired"
    created_at: datetime
    updated_at: datetime


class OntogenNode(BaseModel):
    """Subject / object of the ontology triple model (``ontogen_nodes``)."""

    id: str  # slug, no __ allowed
    name: str
    description: str
    confidence_score: float
    status: str = "pending_review"  # "approved" | "pending_review" | "rejected"
    evidence: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return _validate_slug(v, "id")


class OntogenEdge(BaseModel):
    """Predicate / relationship type (``ontogen_edges``)."""

    id: str  # slug, no __ allowed
    label: str
    semantics: str | None = None
    confidence_score: float
    status: str = "pending_review"  # "approved" | "pending_review" | "rejected"
    evidence: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return _validate_slug(v, "id")


class OntogenTriple(BaseModel):
    """(subject_node, edge, object_node) fact (``ontogen_triples``).

    The ``id`` field is always ``{subject_node_id}__{edge_id}__{object_node_id}``.
    """

    id: str
    subject_node_id: str
    edge_id: str
    object_node_id: str
    confidence_score: float
    status: str = "pending_review"  # "approved" | "pending_review" | "rejected"
    evidence: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("subject_node_id", "edge_id", "object_node_id")
    @classmethod
    def validate_component_ids(cls, v: str, info: Any) -> str:
        return _validate_slug(v, info.field_name)

    @field_validator("id")
    @classmethod
    def validate_id_no_leading_double_underscore(cls, v: str) -> str:
        # id is a composite slug — it MUST contain exactly two __ separators.
        # Detailed composite check is done in the model_validator below.
        # Here we only verify it is non-empty and contains no leading/trailing whitespace.
        if not v.strip():
            raise ValueError("id must not be empty")
        return v

    @model_validator(mode="after")
    def validate_composite_id(self) -> "OntogenTriple":
        expected = f"{self.subject_node_id}__{self.edge_id}__{self.object_node_id}"
        if self.id != expected:
            raise ValueError(
                f"id must equal '{{subject_node_id}}__{{edge_id}}__{{object_node_id}}' "
                f"(expected {expected!r}, got {self.id!r})"
            )
        return self


class DatasetNodeMap(BaseModel):
    """Mapping between a dataset URN and an ontology node (``dataset_node_map``)."""

    dataset_urn: str
    node_id: str
    confidence_score: float
    status: str = "pending"  # "approved" | "pending"
    is_primary: bool = False
    created_at: datetime
