"""Admin endpoints — system configuration and operational tasks.

Accessible to users with Admin role via ``/api/v1/admin/…``.
Also mounted as ``/internal/admin/…`` for scripts and automation (requires the
``X-Internal-Token`` shared-secret header via ``require_internal_token``).
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import require_admin
from src.api.auth.internal import require_internal_token
from src.api.dependencies import get_airflow_client, get_datahub, get_db
from src.api.schemas.admin import (
    BootstrapResponse,
    DagGroupPatchRequest,
    DagGroupsResponse,
    DagGroupStatus,
    DatahubPeripheralPatchRequest,
    DatahubPeripheralResponse,
    DatahubSyncRequest,
    LangfusePeripheralPatchRequest,
    LangfusePeripheralResponse,
    PeripheralHealthModel,
    PeripheralsStatusResponse,
    RuntimeConfPatchRequest,
    RuntimeConfResponse,
    SmtpPeripheralPatchRequest,
    SmtpPeripheralResponse,
    UserPatchRequest,
    UserResponse,
    UserRolePatchRequest,
    UsersListResponse,
    validate_datahub_kafka_security,
)
from src.api.schemas.auth import ApiTokenItem, ApiTokenListResponse
from src.api.schemas.common import parse_sort
from src.backend.admin.config_service import get_runtime_config, patch_runtime_config
from src.backend.admin.dag_control_service import get_dag_groups, set_group_paused
from src.backend.admin.datahub_secret import (
    datahub_kafka_sasl_password_is_set,
    datahub_token_is_set,
    invalidate_datahub_kafka_sasl_password_cache,
    set_datahub_kafka_sasl_password,
    set_datahub_token,
)
from src.backend.admin.langfuse_secret import (
    langfuse_secret_key_is_set,
    set_langfuse_secret_key,
)
from src.backend.admin.llm_secret import llm_api_key_is_set, set_llm_api_key
from src.backend.admin.peripheral_health import (
    PeripheralHealthDTO,
    get_peripheral_health,
)
from src.backend.admin.peripheral_service import (
    get_peripheral_config,
    invalidate_peripheral_config_cache,
    patch_peripheral_config,
)
from src.backend.admin.smtp_secret import (
    invalidate_smtp_password_cache,
    set_smtp_password,
    smtp_password_is_set,
)
from src.backend.auth import api_tokens, users
from src.backend.datahub import users as dh_users
from src.shared.datahub.client import DataHubClient
from src.shared.db.registry import sync_with_datahub
from src.shared.exceptions import ConflictError, StorageUnavailableError
from src.shared.secrets import SecretResolverUnavailable
from src.workflows.airflow.client import AirflowClient
from src.workflows.registry import ALL_DAG_IDS as _EXPECTED_DAGS

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

internal_router = APIRouter(
    prefix="/internal/admin",
    tags=["internal/admin"],
    dependencies=[Depends(require_internal_token)],
)


async def _verify_dags(airflow: AirflowClient) -> dict[str, Any]:
    dags = await airflow.list_dags()
    loaded_ids = {d.get("dag_id") for d in dags}
    missing = sorted(_EXPECTED_DAGS - loaded_ids)
    found = sorted(_EXPECTED_DAGS & loaded_ids)
    logger.info(
        "DAG verification: found %d/%d expected DAGs, missing=%s",
        len(found),
        len(_EXPECTED_DAGS),
        missing,
    )
    return {
        "found": found,
        "missing": missing,
        "total_expected": len(_EXPECTED_DAGS),
    }


@router.post("/dags/verify")
async def verify_dags(
    airflow: AirflowClient = Depends(get_airflow_client),
) -> dict[str, Any]:
    """Verify that all expected Airflow DAGs are loaded and visible."""
    return await _verify_dags(airflow)


@internal_router.post("/dags/verify")
async def internal_verify_dags(
    airflow: AirflowClient = Depends(get_airflow_client),
) -> dict[str, Any]:
    """Verify that all expected Airflow DAGs are loaded (internal — requires X-Internal-Token)."""
    return await _verify_dags(airflow)


# ── DAG schedule control ───────────────────────────────────────────────────────


@router.get("/dags")
async def get_dags_schedule(
    airflow: AirflowClient = Depends(get_airflow_client),
) -> DagGroupsResponse:
    """Return the schedule (paused) state of all controllable DAG groups.

    Operational schedule control — distinct from ``/admin/peripherals``
    (connections) and ``/admin/conf`` (behavioral tunables). Airflow is the SSOT
    for paused state. Returns ``503 AIRFLOW_UNAVAILABLE`` when Airflow is
    unreachable.
    """
    groups = await get_dag_groups(airflow)
    return DagGroupsResponse(groups=groups)


@router.patch("/dags/{group}")
async def patch_dag_group_schedule(
    group: str,
    body: DagGroupPatchRequest,
    airflow: AirflowClient = Depends(get_airflow_client),
) -> DagGroupStatus:
    """Pause or unpause every member DAG of a controllable group.

    Sets ``is_paused`` on each member DAG to ``body.paused`` and returns the
    recomputed group status. An unknown ``group`` returns ``404
    DAG_GROUP_NOT_FOUND``; Airflow unreachable returns ``503 AIRFLOW_UNAVAILABLE``.
    """
    return await set_group_paused(airflow, group, body.paused)


@internal_router.post("/datahub/sync")
async def internal_datahub_sync(
    body: DatahubSyncRequest | None = None,
    db: AsyncSession = Depends(get_db),
    datahub: DataHubClient = Depends(get_datahub),
) -> dict[str, Any]:
    """Reconcile dataset_registry.datahub_registered against DataHub.

    Internal-only — requires ``X-Internal-Token`` header.
    Body is optional. When omitted or ``dataset_urns`` is null, all registry rows are
    checked. When ``dataset_urns`` is provided, only those URNs are reconciled.
    """
    dataset_urns = body.dataset_urns if body else None
    result = await sync_with_datahub(db, datahub, dataset_urns=dataset_urns)
    await db.commit()
    logger.info(
        "datahub_sync completed: checked=%d flipped_true=%d flipped_false=%d "
        "unchanged=%d not_found=%d",
        result["checked"],
        result["flipped_true"],
        result["flipped_false"],
        result["unchanged"],
        result["not_found"],
    )
    return result


# ── Runtime configuration ──────────────────────────────────────────────────────


def _dto_to_response(dto: object, updated_at: datetime | None) -> RuntimeConfResponse:
    """Convert a RuntimeConfigDTO to the API response schema.

    ``llm_api_key`` is always masked: ``"********"`` when set, ``""`` when unset.
    The plaintext key is never included in the response.
    """
    from src.backend.admin.config_service import RuntimeConfigDTO

    assert isinstance(dto, RuntimeConfigDTO)
    return RuntimeConfResponse(
        llm_provider=dto.llm_provider,
        llm_model=dto.llm_model,
        llm_api_key="********" if llm_api_key_is_set() else "",
        ontogen_llm_max_iterations=dto.ontogen_llm_max_iterations,
        ontogen_debate_max_turns=dto.ontogen_debate_max_turns,
        ontogen_debate_rag_k=dto.ontogen_debate_rag_k,
        ontogen_debate_reviewer_model=dto.ontogen_debate_reviewer_model,
        metagen_llm_max_iterations=dto.metagen_llm_max_iterations,
        metagen_debate_max_turns=dto.metagen_debate_max_turns,
        metagen_debate_rag_k=dto.metagen_debate_rag_k,
        metagen_debate_reviewer_model=dto.metagen_debate_reviewer_model,
        metagen_confidence_threshold=dto.metagen_confidence_threshold,
        metagen_ontology_rag_node_k=dto.metagen_ontology_rag_node_k,
        metagen_ontology_rag_edge_k=dto.metagen_ontology_rag_edge_k,
        metagen_ontology_rag_triple_k=dto.metagen_ontology_rag_triple_k,
        validation_score_n_intervals=dto.validation_score_n_intervals,
        stub_redis_client=dto.stub_redis_client,
        stub_llm_client=dto.stub_llm_client,
        stub_pgvector_manager=dto.stub_pgvector_manager,
        stub_notification_service=dto.stub_notification_service,
        auth_datahub_corp_group=dto.auth_datahub_corp_group,
        updated_at=updated_at,
    )


async def _get_conf_with_updated_at(db: AsyncSession) -> RuntimeConfResponse:
    from sqlalchemy import select

    from src.shared.db.models import RuntimeConfig

    dto = await get_runtime_config(db)
    result = await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))
    row = result.scalar_one_or_none()
    updated_at = row.updated_at if row else None
    return _dto_to_response(dto, updated_at)


@router.get("/conf")
async def get_conf(
    db: AsyncSession = Depends(get_db),
) -> RuntimeConfResponse:
    """Return the current singleton runtime configuration."""
    return await _get_conf_with_updated_at(db)


async def _apply_patch_and_respond(
    body: RuntimeConfPatchRequest,
    db: AsyncSession,
) -> RuntimeConfResponse:
    """Shared handler for admin and internal PATCH /conf endpoints.

    ``llm_api_key`` is routed to the Kubernetes Secret, never to the DB.
    The Secret write happens first; if it fails (SecretResolverUnavailable →
    StorageUnavailableError → 503) the DB patch is skipped.  An explicit ``""``
    clears the key; omitting the field entirely leaves it unchanged.
    """
    from sqlalchemy import select

    from src.shared.db.models import RuntimeConfig
    from src.shared.exceptions import StorageUnavailableError

    # Use exclude_unset=True WITHOUT exclude_none so explicit "" is preserved.
    all_updates = body.model_dump(exclude_unset=True)

    # Route llm_api_key to the Secret, not the DB.
    if "llm_api_key" in all_updates:
        key_value: str | None = all_updates.pop("llm_api_key")
        if key_value is None:
            key_value = ""
        try:
            set_llm_api_key(key_value)
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(
                "Kubernetes Secret unavailable; LLM API key could not be stored"
            ) from exc

    # Build DB-targeted updates: exclude None values (those mean "leave unchanged").
    db_updates = {k: v for k, v in all_updates.items() if v is not None}
    dto = await patch_runtime_config(db, **db_updates)
    result = await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))
    row = result.scalar_one_or_none()
    updated_at = row.updated_at if row else None
    return _dto_to_response(dto, updated_at)


@router.patch("/conf")
async def patch_conf(
    body: RuntimeConfPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> RuntimeConfResponse:
    """Apply a partial update to the singleton runtime configuration."""
    return await _apply_patch_and_respond(body, db)


@internal_router.patch("/conf")
async def internal_patch_conf(
    body: RuntimeConfPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> RuntimeConfResponse:
    """Apply a partial update to the runtime configuration (internal — requires X-Internal-Token).

    Intended for install scripts and dev-env seeding.
    """
    return await _apply_patch_and_respond(body, db)


# ── Peripheral configuration ───────────────────────────────────────────────────

# Read-back defaults for non-secret DataHub settings when the row is unset.
_DEFAULT_SERVICE_CORPUSER_URN = "urn:li:corpuser:dataspoke"
_DEFAULT_INGESTION_ENV = "DEV"


def _health_to_model(health: "PeripheralHealthDTO") -> PeripheralHealthModel:
    return PeripheralHealthModel(
        status=health.status,  # type: ignore[arg-type]  # constrained by HEALTH_STATUSES at the service.
        last_error=health.last_error,
        last_ok_at=health.last_ok_at,
        updated_at=health.updated_at,
    )


def _datahub_dto_to_response(
    dto: object | None,
    updated_at: datetime | None,
    health: "PeripheralHealthDTO",
) -> DatahubPeripheralResponse:
    from src.backend.admin.peripheral_service import DatahubConfigDTO

    if dto is None or not isinstance(dto, DatahubConfigDTO):
        return DatahubPeripheralResponse(
            gms_url="",
            frontend_url="",
            kafka_brokers="",
            kafka_security_protocol="PLAINTEXT",
            kafka_sasl_mechanism="",
            kafka_sasl_username="",
            kafka_sasl_password="",
            kafka_sasl_password_version=0,
            kafka_aws_region="",
            token="",
            service_corpuser_urn=_DEFAULT_SERVICE_CORPUSER_URN,
            default_env=_DEFAULT_INGESTION_ENV,
            is_configured=False,
            health=_health_to_model(health),
            updated_at=updated_at,
        )
    token_set = datahub_token_is_set()
    return DatahubPeripheralResponse(
        gms_url=dto.gms_url,
        frontend_url=dto.frontend_url,
        kafka_brokers=dto.kafka_brokers,
        kafka_security_protocol=dto.kafka_security_protocol or "PLAINTEXT",
        kafka_sasl_mechanism=dto.kafka_sasl_mechanism,
        kafka_sasl_username=dto.kafka_sasl_username,
        kafka_sasl_password="********" if datahub_kafka_sasl_password_is_set() else "",
        kafka_sasl_password_version=dto.kafka_sasl_password_version,
        kafka_aws_region=dto.kafka_aws_region,
        token="********" if token_set else "",
        service_corpuser_urn=dto.service_corpuser_urn or _DEFAULT_SERVICE_CORPUSER_URN,
        default_env=dto.default_env or _DEFAULT_INGESTION_ENV,
        # The Kafka credential is optional and never participates in is_configured.
        is_configured=token_set,
        health=_health_to_model(health),
        updated_at=updated_at,
    )


def _langfuse_dto_to_response(
    dto: object | None,
    updated_at: datetime | None,
) -> LangfusePeripheralResponse:
    from src.backend.admin.peripheral_service import LangfuseConfigDTO

    if dto is None or not isinstance(dto, LangfuseConfigDTO):
        return LangfusePeripheralResponse(
            host="",
            public_key="",
            secret_key="",
            project_id="",
            environment_tag="",
            is_configured=False,
            updated_at=updated_at,
        )
    secret_set = langfuse_secret_key_is_set()
    return LangfusePeripheralResponse(
        host=dto.host,
        public_key=dto.public_key,
        secret_key="********" if secret_set else "",
        project_id=dto.project_id,
        environment_tag=dto.environment_tag,
        is_configured=secret_set,
        updated_at=updated_at,
    )


async def _get_peripheral_updated_at(db: AsyncSession, name: str) -> datetime | None:
    from sqlalchemy import select

    from src.shared.db.models import PeripheralConfig

    result = await db.execute(select(PeripheralConfig).where(PeripheralConfig.name == name))
    row = result.scalar_one_or_none()
    return row.updated_at if row else None


@router.get("/peripherals")
async def get_peripherals_status(
    db: AsyncSession = Depends(get_db),
) -> PeripheralsStatusResponse:
    """Return a brief configured/unconfigured status for each peripheral."""
    datahub_dto = await get_peripheral_config(db, "datahub")
    langfuse_dto = await get_peripheral_config(db, "langfuse")
    smtp_dto = await get_peripheral_config(db, "smtp")
    smtp_configured = (
        smtp_dto is not None
        and bool(getattr(smtp_dto, "host", ""))
        and bool(getattr(smtp_dto, "from_address", ""))
        and smtp_password_is_set()
    )
    return PeripheralsStatusResponse(
        datahub={"is_configured": datahub_dto is not None and datahub_token_is_set()},
        langfuse={"is_configured": langfuse_dto is not None and langfuse_secret_key_is_set()},
        smtp={"is_configured": smtp_configured},
    )


@router.get("/peripherals/datahub")
async def get_datahub_peripheral(
    db: AsyncSession = Depends(get_db),
) -> DatahubPeripheralResponse:
    """Return the current DataHub peripheral configuration."""
    dto = await get_peripheral_config(db, "datahub")
    updated_at = await _get_peripheral_updated_at(db, "datahub")
    health = await get_peripheral_health(db, "datahub")
    return _datahub_dto_to_response(dto, updated_at, health)


def _kafka_password_is_set_uncached() -> bool:
    """Read the Kafka SASL password's presence past the process cache.

    ``set_datahub_kafka_sasl_password`` invalidates only the writing process's
    cache, so on a multi-replica API a password stored on one replica is invisible
    to another for up to the 60-second TTL.  Deciding whether to clear a stored
    credential on that stale view would skip the clear and leave a live password
    in the Secret that nothing reads.
    """
    invalidate_datahub_kafka_sasl_password_cache()
    return datahub_kafka_sasl_password_is_set()


def _effective_kafka_field(
    updates: dict[str, Any],
    current: object | None,
    key: str,
    default: str = "",
) -> str:
    """Return the value *key* takes once the patch body lands on the stored config."""
    if key in updates and updates[key] is not None:
        return str(updates[key])
    return str(getattr(current, key, default) or default)


async def _apply_datahub_patch_and_respond(
    body: DatahubPeripheralPatchRequest,
    db: AsyncSession,
) -> DatahubPeripheralResponse:
    """Shared handler for admin and internal PATCH /peripherals/datahub endpoints.

    ``token`` and ``kafka_sasl_password`` are routed to the Kubernetes Secret,
    never to the DB.  Secret writes happen first; if one fails
    (SecretResolverUnavailable → StorageUnavailableError → 503) the DB patch is
    skipped.  Any write of the Kafka password — setting it, clearing it, or
    dropping it because the mechanism no longer reads it — bumps
    ``kafka_sasl_password_version``, which is what makes a Secret-only rotation
    visible to the consumer's DB-plane poll loop.  The increment itself is
    performed inside the DB transaction by ``patch_peripheral_config``.
    """
    from src.shared.exceptions import StorageUnavailableError

    all_updates = body.model_dump(exclude_unset=True)

    # Validate the Kafka tuple the patch produces, before anything is written.
    # The read must bypass the process cache: a stale entry on this replica would
    # judge the patch against a configuration another replica has already changed.
    invalidate_peripheral_config_cache("datahub")
    current = await get_peripheral_config(db, "datahub")
    effective_mechanism = _effective_kafka_field(all_updates, current, "kafka_sasl_mechanism")
    validate_datahub_kafka_security(
        security_protocol=_effective_kafka_field(
            all_updates, current, "kafka_security_protocol", "PLAINTEXT"
        ),
        sasl_mechanism=effective_mechanism,
        sasl_username=_effective_kafka_field(all_updates, current, "kafka_sasl_username"),
        aws_region=_effective_kafka_field(all_updates, current, "kafka_aws_region"),
        brokers=_effective_kafka_field(all_updates, current, "kafka_brokers"),
        submitted_sasl_password=all_updates.get("kafka_sasl_password"),
    )

    if "token" in all_updates:
        token_value: str | None = all_updates.pop("token")
        if token_value is None:
            token_value = ""
        try:
            set_datahub_token(token_value)
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(
                "Kubernetes Secret unavailable; DataHub token could not be stored"
            ) from exc

    password_written = False
    password_value: str | None = None
    if "kafka_sasl_password" in all_updates:
        password_value = all_updates.pop("kafka_sasl_password") or ""
    elif effective_mechanism == "AWS_MSK_IAM" and _kafka_password_is_set_uncached():
        # A stored password under AWS_MSK_IAM is state that has lost its purpose:
        # the mechanism authenticates with the pod's IAM identity and never reads
        # it. Submitting one is rejected; leaving one behind would keep a live
        # credential in the Secret that nothing uses and that GET would keep
        # reporting as "********". Clear it instead.
        password_value = ""

    if password_value is not None:
        try:
            set_datahub_kafka_sasl_password(password_value)
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(
                "Kubernetes Secret unavailable; DataHub Kafka SASL password could not be stored"
            ) from exc
        password_written = True

    db_updates: dict[str, Any] = {k: v for k, v in all_updates.items() if v is not None}

    if db_updates or password_written:
        try:
            dto = await patch_peripheral_config(
                db,
                "datahub",
                bump_kafka_sasl_password_version=password_written,
                **db_updates,
            )
        except Exception:
            if password_written:
                # The Secret holds the new credential but the counter did not move,
                # so the consumer will not notice the rotation. Name it explicitly:
                # the operator otherwise sees a failed request and assumes nothing
                # changed.
                logger.warning(
                    "datahub_kafka_password_rotation_half_applied",
                    extra={
                        "detail": (
                            "kafka_sasl_password was written to dataspoke-datahub-secret "
                            "but kafka_sasl_password_version was not incremented; "
                            "re-issue the PATCH so the event consumer picks up the rotation"
                        )
                    },
                )
            raise
    else:
        invalidate_peripheral_config_cache("datahub")
        dto = await get_peripheral_config(db, "datahub")

    updated_at = await _get_peripheral_updated_at(db, "datahub")
    health = await get_peripheral_health(db, "datahub")
    return _datahub_dto_to_response(dto, updated_at, health)


@router.patch("/peripherals/datahub")
async def patch_datahub_peripheral(
    body: DatahubPeripheralPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> DatahubPeripheralResponse:
    """Apply a partial update to the DataHub peripheral configuration."""
    return await _apply_datahub_patch_and_respond(body, db)


@internal_router.patch("/peripherals/datahub")
async def internal_patch_datahub_peripheral(
    body: DatahubPeripheralPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> DatahubPeripheralResponse:
    """Apply a partial update to the DataHub peripheral configuration.

    Internal — requires the ``X-Internal-Token`` header.
    Intended for install scripts and dev-env seeding.
    """
    return await _apply_datahub_patch_and_respond(body, db)


@router.get("/peripherals/langfuse")
async def get_langfuse_peripheral(
    db: AsyncSession = Depends(get_db),
) -> LangfusePeripheralResponse:
    """Return the current Langfuse peripheral configuration."""
    dto = await get_peripheral_config(db, "langfuse")
    updated_at = await _get_peripheral_updated_at(db, "langfuse")
    return _langfuse_dto_to_response(dto, updated_at)


async def _apply_langfuse_patch_and_respond(
    body: LangfusePeripheralPatchRequest,
    db: AsyncSession,
) -> LangfusePeripheralResponse:
    """Shared handler for admin and internal PATCH /peripherals/langfuse endpoints.

    ``secret_key`` is routed to the Kubernetes Secret, never to the DB.
    The Secret write happens first; if it fails (SecretResolverUnavailable →
    StorageUnavailableError → 503) the DB patch is skipped.
    """
    from src.shared.exceptions import StorageUnavailableError

    all_updates = body.model_dump(exclude_unset=True)

    if "secret_key" in all_updates:
        secret_value: str | None = all_updates.pop("secret_key")
        if secret_value is None:
            secret_value = ""
        try:
            set_langfuse_secret_key(secret_value)
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(
                "Kubernetes Secret unavailable; Langfuse secret key could not be stored"
            ) from exc

    db_updates = {k: v for k, v in all_updates.items() if v is not None}
    if db_updates:
        dto = await patch_peripheral_config(db, "langfuse", **db_updates)
    else:
        invalidate_peripheral_config_cache("langfuse")
        dto = await get_peripheral_config(db, "langfuse")

    updated_at = await _get_peripheral_updated_at(db, "langfuse")
    return _langfuse_dto_to_response(dto, updated_at)


@router.patch("/peripherals/langfuse")
async def patch_langfuse_peripheral(
    body: LangfusePeripheralPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> LangfusePeripheralResponse:
    """Apply a partial update to the Langfuse peripheral configuration."""
    return await _apply_langfuse_patch_and_respond(body, db)


@internal_router.patch("/peripherals/langfuse")
async def internal_patch_langfuse_peripheral(
    body: LangfusePeripheralPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> LangfusePeripheralResponse:
    """Apply a partial update to the Langfuse peripheral configuration.

    Internal — requires the ``X-Internal-Token`` header.
    Intended for install scripts and dev-env seeding.
    """
    return await _apply_langfuse_patch_and_respond(body, db)


# ── SMTP peripheral ────────────────────────────────────────────────────────────


def _smtp_dto_to_response(
    dto: object | None,
    updated_at: datetime | None,
) -> SmtpPeripheralResponse:
    from src.backend.admin.peripheral_service import SmtpConfigDTO

    password_set = smtp_password_is_set()
    if dto is None or not isinstance(dto, SmtpConfigDTO):
        return SmtpPeripheralResponse(
            host="",
            port=0,
            username="",
            from_address="",
            use_tls=False,
            password="" if not password_set else "********",
            is_configured=False,
            updated_at=updated_at,
        )
    is_configured = bool(dto.host and dto.from_address and password_set)
    return SmtpPeripheralResponse(
        host=dto.host,
        port=dto.port,
        username=dto.username,
        from_address=dto.from_address,
        use_tls=dto.use_tls,
        password="********" if password_set else "",
        is_configured=is_configured,
        updated_at=updated_at,
    )


@router.get("/peripherals/smtp")
async def get_smtp_peripheral(
    db: AsyncSession = Depends(get_db),
) -> SmtpPeripheralResponse:
    """Return the current SMTP peripheral configuration."""
    dto = await get_peripheral_config(db, "smtp")
    updated_at = await _get_peripheral_updated_at(db, "smtp")
    return _smtp_dto_to_response(dto, updated_at)


async def _apply_smtp_patch_and_respond(
    body: SmtpPeripheralPatchRequest,
    db: AsyncSession,
) -> SmtpPeripheralResponse:
    """Shared handler for admin and internal PATCH /peripherals/smtp endpoints.

    ``password`` is routed to the Kubernetes Secret, never to the DB.
    """
    all_updates = body.model_dump(exclude_unset=True)

    if "password" in all_updates:
        password_value: str | None = all_updates.pop("password")
        if password_value is None:
            password_value = ""
        try:
            set_smtp_password(password_value)
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(
                "Kubernetes Secret unavailable; SMTP password could not be stored"
            ) from exc

    db_updates = {k: v for k, v in all_updates.items() if v is not None}
    if db_updates:
        dto = await patch_peripheral_config(db, "smtp", **db_updates)
    else:
        invalidate_smtp_password_cache()
        dto = await get_peripheral_config(db, "smtp")

    updated_at = await _get_peripheral_updated_at(db, "smtp")
    return _smtp_dto_to_response(dto, updated_at)


@router.patch("/peripherals/smtp")
async def patch_smtp_peripheral(
    body: SmtpPeripheralPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> SmtpPeripheralResponse:
    """Apply a partial update to the SMTP peripheral configuration."""
    return await _apply_smtp_patch_and_respond(body, db)


@internal_router.patch("/peripherals/smtp")
async def internal_patch_smtp_peripheral(
    body: SmtpPeripheralPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> SmtpPeripheralResponse:
    """Apply a partial update to the SMTP peripheral configuration.

    Internal — requires the ``X-Internal-Token`` header.
    Intended for install scripts and dev-env seeding.
    """
    return await _apply_smtp_patch_and_respond(body, db)


# ── User management ────────────────────────────────────────────────────────────


def _user_to_response(user: object) -> UserResponse:
    from src.shared.db.models import User

    assert isinstance(user, User)
    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        has_google=user.google_sub is not None,
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _token_to_item(t: object) -> ApiTokenItem:
    from src.shared.db.models import ApiToken

    assert isinstance(t, ApiToken)
    return ApiTokenItem(
        id=t.id,
        name=t.name,
        role_snapshot=t.role_snapshot,
        created_at=t.created_at,
        last_used_at=t.last_used_at,
        expires_at=t.expires_at,
    )


@router.get("/users")
async def get_users(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> UsersListResponse:
    """List all DataSpoke users (paginated; sortable by created_at, email)."""
    from src.shared.db.models import User

    order_by = parse_sort(
        sort,
        {
            "created_at": User.created_at,
            "updated_at": User.updated_at,
            "email": User.email,
        },
        None,
    )
    user_list, total = await users.list_users(db, limit=limit, offset=offset, order_by=order_by)
    return UsersListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        users=[_user_to_response(u) for u in user_list],
    )


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatchRequest,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Update a user's display name.

    The display name is DataSpoke-local; the DataHub-side profile is owned by
    DataHub's OIDC JIT provisioning, which refreshes it from the Google claims
    on each DataHub login.
    """
    user = await users.update_name(db, user_id, body.name)
    await db.commit()
    return _user_to_response(user)


@router.patch("/users/{user_id}/role")
async def patch_user_role(
    user_id: uuid.UUID,
    body: UserRolePatchRequest,
    db: AsyncSession = Depends(get_db),
    datahub: DataHubClient = Depends(get_datahub),
) -> dict[str, Any]:
    """Update a user's role and project it onto DataHub.

    The projection is gated on the user having a verified Google identity
    (``google_sub``). ``corpuser_urn`` addresses whoever DataHub's OIDC JIT
    provisioned at that email, and a password-registered row's email is
    unverified — so projecting for an unbound row would let someone who
    registered another person's address steer that person's DataHub role.
    A password-only account simply carries no DataHub projection; the user
    establishes the binding by signing into DataSpoke with Google once.
    """
    user = await users.update_role(db, user_id, body.role)
    if user.google_sub is not None:
        # Projection is non-fatal on failure — the nightly DAG reconciles.
        try:
            await dh_users.propagate_role(datahub, dh_users.corpuser_urn(user.email), body.role)
        except Exception:
            logger.warning(
                "datahub_role_propagation_failed",
                extra={"user_id": str(user_id), "role": body.role},
                exc_info=True,
            )
    await db.commit()
    return {"role": user.role}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    datahub: DataHubClient = Depends(get_datahub),
) -> None:
    """Hard-delete a user and their DataHub corpuser."""
    user = await users.get_by_id(db, user_id)
    if user is None:
        from src.shared.exceptions import EntityNotFoundError

        raise EntityNotFoundError("user", str(user_id))
    email = user.email
    await users.hard_delete(db, user_id)
    # DataHub delete is best-effort — orphan cleaned up manually.
    try:
        await dh_users.hard_delete_corpuser(datahub, dh_users.corpuser_urn(email))
    except Exception:
        logger.warning(
            "datahub_corpuser_delete_failed",
            extra={"user_id": str(user_id), "email": email},
            exc_info=True,
        )
    await db.commit()


@router.get("/users/{user_id}/api-tokens")
async def get_user_api_tokens(
    user_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiTokenListResponse:
    """List all API tokens for a user (admin view — includes revoked).

    Paginated; sortable by created_at (default: created_at descending).
    """
    token_list = await api_tokens.list_all_for_user(db, user_id)
    token_list = api_tokens.sort_tokens(token_list, sort)
    total = len(token_list)
    page = token_list[offset : offset + limit]
    return ApiTokenListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        tokens=[_token_to_item(t) for t in page],
    )


@router.delete("/users/{user_id}/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_api_token(
    user_id: uuid.UUID,
    token_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a user's API token (admin incident response — no ownership check)."""
    await api_tokens.revoke(db, token_id=token_id)
    await db.commit()


# ── Bootstrap ──────────────────────────────────────────────────────────────────


@internal_router.post("/bootstrap")
async def internal_bootstrap(
    db: AsyncSession = Depends(get_db),
) -> BootstrapResponse:
    """Seed the built-in dataspoke@dataspoke.local/dataspoke admin if no Admin user exists.

    Idempotent: if any Admin user already exists, returns created=False without
    touching anything.

    Writes only the local users row, so it requires no peripheral configuration
    and succeeds on a fresh install before DataHub is wired. The bootstrap
    address has no Google identity behind it, so the row carries no google_sub
    and reconciliation reports it as skipped_unbound — the binding gate runs
    ahead of the existence probe.

    Protected by X-Internal-Token — only Helm post-install scripts should call this.
    """
    from sqlalchemy import select

    from src.shared.db.models import User

    # Check for any existing Admin.
    result = await db.execute(select(User).where(User.role == "Admin").limit(1))
    existing_admin = result.scalar_one_or_none()
    if existing_admin is not None:
        return BootstrapResponse(created=False, user_id=None, email=None)

    # Create the bootstrap admin.
    try:
        user = await users.create_user(
            db,
            email="dataspoke@dataspoke.local",
            name="DataSpoke Admin",
            password="dataspoke",
            role="Admin",
        )
    except ConflictError as exc:
        if exc.error_code == "EMAIL_ALREADY_REGISTERED":
            # A concurrent caller already created the admin — re-check and return no-op.
            result2 = await db.execute(select(User).where(User.role == "Admin").limit(1))
            if result2.scalar_one_or_none() is not None:
                return BootstrapResponse(created=False, user_id=None, email=None)
        raise

    await db.commit()
    logger.info("bootstrap_admin_created", extra={"user_id": str(user.id)})
    logger.warning(
        "bootstrap_admin_seeded_with_default_password",
        extra={"hint": "rotate via PATCH /auth/me before going to production"},
    )
    return BootstrapResponse(created=True, user_id=str(user.id), email="dataspoke@dataspoke.local")
