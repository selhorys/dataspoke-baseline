"""Shared admin request schemas.

Used by both the admin router and the internal activities router to ensure
consistent validation (URN pattern + list-length cap) on DataHub sync requests.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from src.api.schemas.common import (
    SAFE_DISPLAY_URL_MAX_LENGTH,
    SAFE_DISPLAY_URL_PATTERN,
    SAFE_PROJECT_ID_MAX_LENGTH,
    SAFE_PROJECT_ID_PATTERN,
    DatasetUrn,
    PaginatedResponse,
    SingleResponse,
)
from src.shared.datahub.kafka_security import check_kafka_security


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
    stub_redis_client: bool
    stub_llm_client: bool
    stub_pgvector_manager: bool
    stub_notification_service: bool
    auth_datahub_corp_group: str
    updated_at: datetime | None = None


# ── Peripheral configuration ───────────────────────────────────────────────────


class PeripheralHealthModel(BaseModel):
    """A peripheral's last self-reported connection state.

    ``unknown`` covers both "never reported" and "no reporter deployed"; the API
    does not distinguish them.
    """

    status: Literal["unknown", "ok", "error"]
    last_error: str | None = None
    last_ok_at: datetime | None = None
    updated_at: datetime | None = None


class DatahubPeripheralResponse(SingleResponse):
    """Response envelope for the DataHub peripheral configuration.

    ``token`` and ``kafka_sasl_password`` are masked indicators only: ``""``
    when unset, ``"********"`` when set.  The plaintext values are never
    returned.  ``is_configured`` keys on ``token`` alone — the Kafka credential
    is optional and never participates in it.

    ``kafka_sasl_password_version`` is API-owned bookkeeping incremented on every
    password write; it is read-only and not accepted on PATCH.

    DataSpoke reaches DataHub over two independent transports, each with its own
    health row: ``health`` is the event consumer's report of the Kafka event
    stream, ``api_health`` the sync sweep's report of the GMS metadata API.
    Neither is a verdict on DataHub overall.
    """

    gms_url: str
    frontend_url: str
    kafka_brokers: str
    kafka_security_protocol: str
    kafka_sasl_mechanism: str
    kafka_sasl_username: str
    kafka_sasl_password: str
    kafka_sasl_password_version: int
    kafka_aws_region: str
    token: str
    service_corpuser_urn: str
    default_env: str
    is_configured: bool
    health: PeripheralHealthModel
    api_health: PeripheralHealthModel
    updated_at: datetime | None = None


class DatahubPeripheralPatchRequest(BaseModel):
    """Partial update for the DataHub peripheral configuration.

    All fields are optional — only supplied (non-null) fields are applied.
    ``token`` and ``kafka_sasl_password`` are routed to the Kubernetes Secret
    rather than the DB.  An explicitly provided ``""`` clears a secret; ``None``
    (or omitting the field) means "leave it unchanged".

    ``service_corpuser_urn`` and ``default_env`` are non-secret connection
    settings stored in the DB; they drive the emitted DataHub actor URN and the
    ingestion fabric/env default respectively.

    ``gms_url`` and ``frontend_url`` share the same http(s) shape constraint.
    ``frontend_url`` needs it because it is interpolated into an anchor ``href``;
    ``gms_url`` needs it because the pattern bars **userinfo**, and a transport
    exception quoting a URL that carried an embedded credential would persist it
    into ``peripheral_health.last_error`` and the API's logs.

    Known limitation: the shared pattern's authority admits a hostname plus an
    optional numeric port, so a bracketed **IPv6 literal** (``http://[::1]:8080``)
    is rejected. For ``frontend_url`` that only costs a UI deep-link; for
    ``gms_url`` it means an IPv6-only GMS cannot be configured through this route.
    Widening the authority is a change to ``SAFE_DISPLAY_URL_PATTERN``, which
    ``src/frontend/lib/safe-url.ts`` mirrors character-for-character under
    ``tests/fixtures/safe-url-cases.json``, so both engines must move together.

    ``frontend_url`` is the browser-facing DataHub UI URL — distinct from
    ``gms_url``, which addresses the GMS service.  It is served to any
    authenticated role via ``/spoke/common/peripheral-links`` and interpolated
    into an anchor ``href``, so it is constrained to a safe http(s) form.

    The ``kafka_*`` fields configure the event consumer's connection to a secured
    Kafka.  They are cross-validated against the *merged* stored configuration by
    ``validate_datahub_kafka_security`` at the router, not field-by-field here,
    because a partial PATCH is only meaningful against the settings it lands on.
    ``kafka_sasl_password_version`` is not accepted — the API owns the counter.
    """

    gms_url: Annotated[
        str | None,
        Field(
            default=None,
            max_length=SAFE_DISPLAY_URL_MAX_LENGTH,
            pattern=SAFE_DISPLAY_URL_PATTERN,
        ),
    ] = None
    frontend_url: Annotated[
        str | None,
        Field(
            default=None,
            max_length=SAFE_DISPLAY_URL_MAX_LENGTH,
            pattern=SAFE_DISPLAY_URL_PATTERN,
        ),
    ] = None
    kafka_brokers: Annotated[str | None, Field(default=None, max_length=512)] = None
    kafka_security_protocol: Annotated[str | None, Field(default=None, max_length=32)] = None
    kafka_sasl_mechanism: Annotated[str | None, Field(default=None, max_length=32)] = None
    kafka_sasl_username: Annotated[str | None, Field(default=None, max_length=256)] = None
    kafka_sasl_password: Annotated[str | None, Field(default=None, max_length=8192)] = None
    kafka_aws_region: Annotated[
        str | None,
        Field(default=None, max_length=64, pattern=r"^$|^[a-z0-9-]+$"),
    ] = None
    token: Annotated[str | None, Field(default=None, max_length=8192)] = None
    service_corpuser_urn: Annotated[
        str | None,
        Field(default=None, max_length=512, pattern=r"^$|^urn:li:corpuser:[^\s,()]+$"),
    ] = None
    default_env: Annotated[
        str | None,
        Field(default=None, max_length=64, pattern=r"^$|^[A-Za-z][A-Za-z0-9_]*$"),
    ] = None


def validate_datahub_kafka_security(
    *,
    security_protocol: str,
    sasl_mechanism: str,
    sasl_username: str,
    aws_region: str,
    brokers: str,
    submitted_sasl_password: str | None,
) -> None:
    """Enforce the Kafka security rules of spec/API.md §DataHub Kafka security.

    A thin HTTP wrapper over ``check_kafka_security``: the rules themselves live
    in ``src/shared/datahub/kafka_security.py`` so the event consumer re-asserts
    exactly the same predicate against the stored row before building a client.

    Arguments describe the **effective** tuple — stored settings with the PATCH
    body merged over them — so a partial update is judged by the configuration it
    produces.  ``submitted_sasl_password`` is the body's value (``None`` when
    absent).

    Raises:
        PreconditionFailedError: ``422 INVALID_PARAMETER`` naming the offending
            field in ``detail.field``.
    """
    from src.shared.exceptions import PreconditionFailedError

    violation = check_kafka_security(
        security_protocol=security_protocol,
        sasl_mechanism=sasl_mechanism,
        sasl_username=sasl_username,
        aws_region=aws_region,
        brokers=brokers,
        submitted_sasl_password=submitted_sasl_password,
    )
    if violation is not None:
        raise PreconditionFailedError(
            "INVALID_PARAMETER", violation.message, detail={"field": violation.field}
        )



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

    ``host`` and ``project_id`` are served to any authenticated role via
    ``/spoke/common/peripheral-links``, where the host becomes an anchor
    ``href`` and the project id is interpolated into a deep-link path, so both
    are constrained rather than length-capped alone.
    """

    host: Annotated[
        str | None,
        Field(
            default=None,
            max_length=SAFE_DISPLAY_URL_MAX_LENGTH,
            pattern=SAFE_DISPLAY_URL_PATTERN,
        ),
    ] = None
    public_key: Annotated[str | None, Field(default=None, max_length=512)] = None
    secret_key: Annotated[str | None, Field(default=None, max_length=8192)] = None
    project_id: Annotated[
        str | None,
        Field(
            default=None,
            max_length=SAFE_PROJECT_ID_MAX_LENGTH,
            pattern=SAFE_PROJECT_ID_PATTERN,
        ),
    ] = None
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
    has_password: bool
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
