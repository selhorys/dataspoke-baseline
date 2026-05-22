"""Shared admin request schemas.

Used by both the admin router and the internal activities router to ensure
consistent validation (URN pattern + list-length cap) on DataHub sync requests.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from src.api.schemas.common import SingleResponse

# A single DataHub dataset URN with format and length constraints.
DatasetUrn = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        pattern=r"^urn:li:dataset:\(.+\)$",
    ),
]


class DatahubSyncRequest(BaseModel):
    """Request body for DataHub sync operations.

    ``dataset_urns=None`` (or omitted) triggers a full-sweep reconciliation.
    When provided, only the listed URNs are reconciled.  Capped at 10 000
    entries to prevent runaway requests.
    """

    dataset_urns: Annotated[list[DatasetUrn], Field(max_length=10_000)] | None = None


# ── Runtime configuration ──────────────────────────────────────────────────────


class RuntimeConfResponse(SingleResponse):
    """Response envelope for the singleton runtime configuration.

    ``llm_api_key`` is a masked indicator only: ``""`` when unset, ``"********"``
    when set.  The plaintext key is never returned.
    """

    llm_provider: str
    llm_model: str
    llm_api_key: str
    ontogen_llm_max_iterations: int
    ontogen_debate_max_turns: int
    ontogen_debate_rag_k: int
    ontogen_debate_reviewer_model: str | None
    metagen_llm_max_iterations: int
    metagen_debate_max_turns: int
    metagen_debate_rag_k: int
    metagen_debate_reviewer_model: str | None
    metagen_confidence_threshold: float
    metagen_ontology_rag_node_k: int
    metagen_ontology_rag_edge_k: int
    metagen_ontology_rag_triple_k: int
    validation_score_n_intervals: int
    updated_at: datetime | None = None


class RuntimeConfPatchRequest(BaseModel):
    """Partial update for the singleton runtime configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    Bound violations raise HTTP 422 (FastAPI/Pydantic constraint enforcement).

    ``llm_api_key`` is routed to the Kubernetes Secret rather than the DB.
    An explicitly provided ``""`` clears the key; ``None`` (or omitting the
    field) means "leave the key unchanged".
    """

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: Annotated[str | None, Field(default=None, max_length=8192)] = None
    ontogen_llm_max_iterations: Annotated[int | None, Field(default=None, ge=1, le=20)] = None
    ontogen_debate_max_turns: Annotated[int | None, Field(default=None, ge=2, le=10)] = None
    ontogen_debate_rag_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    ontogen_debate_reviewer_model: str | None = None
    metagen_llm_max_iterations: Annotated[int | None, Field(default=None, ge=1, le=20)] = None
    metagen_debate_max_turns: Annotated[int | None, Field(default=None, ge=2, le=10)] = None
    metagen_debate_rag_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    metagen_debate_reviewer_model: str | None = None
    metagen_confidence_threshold: Annotated[
        float | None, Field(default=None, ge=0.0, le=1.0)
    ] = None
    metagen_ontology_rag_node_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    metagen_ontology_rag_edge_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    metagen_ontology_rag_triple_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    validation_score_n_intervals: Annotated[int | None, Field(default=None, ge=1)] = None
