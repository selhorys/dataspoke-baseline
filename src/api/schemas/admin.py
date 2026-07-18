"""Shared admin request schemas.

Used by both the admin router and the internal activities router to ensure
consistent validation (URN pattern + list-length cap) on DataHub sync requests.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse

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


# ── DAG schedule control ───────────────────────────────────────────────────────

# The controllable DAG groups (operational schedule control via Airflow).
DagGroup = Literal[
    "datahub_sync",
    "auth_role_sync",
    "ingestion_active",
    "ontogen",
    "metagen",
    "metrics",
]


class DagDetail(BaseModel):
    """Paused state of a single member DAG within a group."""

    dag_id: str
    paused: bool


class DagGroupStatus(BaseModel):
    """Schedule (paused) status of one controllable DAG group.

    ``paused`` is true only when all member DAGs are paused; ``mixed`` is true
    when members disagree (some paused, some not).
    """

    group: DagGroup
    paused: bool
    mixed: bool
    dags: list[DagDetail]


class DagGroupsResponse(SingleResponse):
    """Response for GET /admin/dags — a fixed status object, not a record collection."""

    groups: list[DagGroupStatus]


class DagGroupPatchRequest(BaseModel):
    """Request body for PATCH /admin/dags/{group}.

    Sets ``is_paused`` on every member DAG of the group to ``paused``.
    """

    paused: bool


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
    auth_datahub_corp_group: str
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
    service_corpuser_urn: str
    default_env: str
    is_configured: bool
    updated_at: datetime | None = None


class DatahubPeripheralPatchRequest(BaseModel):
    """Partial update for the DataHub peripheral configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    ``token`` is routed to the Kubernetes Secret rather than the DB.
    An explicitly provided ``""`` clears the token; ``None`` (or omitting
    the field) means "leave the token unchanged".

    ``service_corpuser_urn`` and ``default_env`` are non-secret connection
    settings stored in the DB; they drive the emitted DataHub actor URN and the
    ingestion fabric/env default respectively.
    """

    gms_url: Annotated[str | None, Field(default=None, max_length=512)] = None
    kafka_brokers: Annotated[str | None, Field(default=None, max_length=512)] = None
    token: Annotated[str | None, Field(default=None, max_length=8192)] = None
    service_corpuser_urn: Annotated[
        str | None,
        Field(default=None, max_length=512, pattern=r"^$|^urn:li:corpuser:[^\s,()]+$"),
    ] = None
    default_env: Annotated[
        str | None,
        Field(default=None, max_length=64, pattern=r"^$|^[A-Za-z][A-Za-z0-9_]*$"),
    ] = None


class LangfusePeripheralResponse(SingleResponse):
    """Response envelope for the Langfuse peripheral configuration.

    ``secret_key`` is a masked indicator only: ``""`` when unset, ``"********"``
    when set.  The plaintext secret key is never returned.
    """

    host: str
    public_key: str
    secret_key: str
    project_id: str
    environment_tag: str
    is_configured: bool
    updated_at: datetime | None = None


class LangfusePeripheralPatchRequest(BaseModel):
    """Partial update for the Langfuse peripheral configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    ``secret_key`` is routed to the Kubernetes Secret rather than the DB.
    An explicitly provided ``""`` clears the secret key; ``None`` (or omitting
    the field) means "leave the secret key unchanged".

    ``project_id`` and ``environment_tag`` are non-secret connection settings
    stored in the DB; ``environment_tag`` drives the Langfuse trace environment
    and ``project_id`` is surfaced as trace metadata.
    """

    host: Annotated[str | None, Field(default=None, max_length=512)] = None
    public_key: Annotated[str | None, Field(default=None, max_length=512)] = None
    secret_key: Annotated[str | None, Field(default=None, max_length=8192)] = None
    project_id: Annotated[str | None, Field(default=None, max_length=256)] = None
    environment_tag: Annotated[str | None, Field(default=None, max_length=64)] = None


class PeripheralsStatusResponse(SingleResponse):
    """Summary response for GET /admin/peripherals."""

    datahub: dict[str, Any]
    langfuse: dict[str, Any]
    smtp: dict[str, Any] = Field(default_factory=dict)


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
    metagen_confidence_threshold: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = (
        None
    )
    metagen_ontology_rag_node_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    metagen_ontology_rag_edge_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    metagen_ontology_rag_triple_k: Annotated[int | None, Field(default=None, ge=0, le=20)] = None
    validation_score_n_intervals: Annotated[int | None, Field(default=None, ge=1)] = None
    stub_redis_client: bool | None = None
    stub_llm_client: bool | None = None
    stub_pgvector_manager: bool | None = None
    stub_notification_service: bool | None = None
    auth_datahub_corp_group: str | None = None


# ── User management ────────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    """User representation for admin endpoints."""

    id: uuid.UUID
    email: str
    name: str
    has_google: bool
    role: str
    created_at: datetime
    updated_at: datetime


class UsersListResponse(PaginatedResponse):
    users: list[UserResponse]


class UserPatchRequest(BaseModel):
    name: str = Field(max_length=128)


class UserRolePatchRequest(BaseModel):
    role: Literal["Admin", "Editor", "Reader"]


# ── SMTP peripheral ────────────────────────────────────────────────────────────


class SmtpPeripheralResponse(SingleResponse):
    """Response envelope for the SMTP peripheral configuration.

    ``password`` is masked: ``""`` when unset, ``"********"`` when set.
    """

    host: str
    port: int
    username: str
    from_address: str
    use_tls: bool
    password: str
    is_configured: bool
    updated_at: datetime | None = None


class SmtpPeripheralPatchRequest(BaseModel):
    """Partial update for the SMTP peripheral configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    ``password`` is routed to the Kubernetes Secret rather than the DB.
    An explicitly provided ``""`` clears the password; ``None`` (or omitting
    the field) means "leave the password unchanged".
    """

    host: Annotated[str | None, Field(default=None, max_length=512)] = None
    port: Annotated[int | None, Field(default=None, ge=1, le=65535)] = None
    username: Annotated[str | None, Field(default=None, max_length=512)] = None
    from_address: Annotated[str | None, Field(default=None, max_length=512)] = None
    use_tls: bool | None = None
    password: Annotated[str | None, Field(default=None, max_length=8192)] = None


# ── Bootstrap ──────────────────────────────────────────────────────────────────


class BootstrapResponse(BaseModel):
    created: bool
    user_id: str | None = None
    email: str | None = None
