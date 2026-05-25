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
    stub_redis_client: bool
    stub_llm_client: bool
    stub_pgvector_manager: bool
    stub_notification_service: bool
    updated_at: datetime | None = None


# ── Peripheral configuration ───────────────────────────────────────────────────


class DatahubPeripheralResponse(SingleResponse):
    """Response envelope for the DataHub peripheral configuration.

    ``token`` is a masked indicator only: ``""`` when unset, ``"********"``
    when set.  The plaintext token is never returned.
    """

    gms_url: str
    kafka_brokers: str
    token: str
    is_configured: bool
    updated_at: datetime | None = None


class DatahubPeripheralPatchRequest(BaseModel):
    """Partial update for the DataHub peripheral configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    ``token`` is routed to the Kubernetes Secret rather than the DB.
    An explicitly provided ``""`` clears the token; ``None`` (or omitting
    the field) means "leave the token unchanged".
    """

    gms_url: Annotated[str | None, Field(default=None, max_length=512)] = None
    kafka_brokers: Annotated[str | None, Field(default=None, max_length=512)] = None
    token: Annotated[str | None, Field(default=None, max_length=8192)] = None


class LangfusePeripheralResponse(SingleResponse):
    """Response envelope for the Langfuse peripheral configuration.

    ``secret_key`` is a masked indicator only: ``""`` when unset, ``"********"``
    when set.  The plaintext secret key is never returned.
    """

    host: str
    public_key: str
    secret_key: str
    is_configured: bool
    updated_at: datetime | None = None


class LangfusePeripheralPatchRequest(BaseModel):
    """Partial update for the Langfuse peripheral configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    ``secret_key`` is routed to the Kubernetes Secret rather than the DB.
    An explicitly provided ``""`` clears the secret key; ``None`` (or omitting
    the field) means "leave the secret key unchanged".
    """

    host: Annotated[str | None, Field(default=None, max_length=512)] = None
    public_key: Annotated[str | None, Field(default=None, max_length=512)] = None
    secret_key: Annotated[str | None, Field(default=None, max_length=8192)] = None


class PeripheralsStatusResponse(SingleResponse):
    """Summary response for GET /admin/peripherals."""

    datahub: dict
    langfuse: dict


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
    stub_redis_client: bool | None = None
    stub_llm_client: bool | None = None
    stub_pgvector_manager: bool | None = None
    stub_notification_service: bool | None = None
