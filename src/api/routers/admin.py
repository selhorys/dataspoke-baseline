"""Admin endpoints — system configuration and operational tasks.

Accessible to users with the ``admin`` group claim via ``/api/v1/admin/…``.
Also mounted as ``/internal/admin/…`` for scripts and automation (requires ``X-Internal-Token`` shared-secret header via ``require_internal_token``).
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import require_admin
from src.api.auth.internal import require_internal_token
from src.api.dependencies import get_airflow_client, get_datahub, get_db
from src.api.schemas.admin import (
    DatahubPeripheralPatchRequest,
    DatahubPeripheralResponse,
    DatahubSyncRequest,
    LangfusePeripheralPatchRequest,
    LangfusePeripheralResponse,
    PeripheralsStatusResponse,
    RuntimeConfPatchRequest,
    RuntimeConfResponse,
)
from src.backend.admin.config_service import get_runtime_config, patch_runtime_config
from src.backend.admin.datahub_secret import (
    datahub_token_is_set,
    invalidate_datahub_token_cache,
    set_datahub_token,
)
from src.backend.admin.langfuse_secret import (
    langfuse_secret_key_is_set,
    invalidate_langfuse_secret_key_cache,
    set_langfuse_secret_key,
)
from src.backend.admin.llm_secret import llm_api_key_is_set, set_llm_api_key
from src.backend.admin.peripheral_service import (
    get_peripheral_config,
    invalidate_peripheral_config_cache,
    patch_peripheral_config,
)
from src.backend.ingestion.secret_resolver import SecretResolverUnavailable
from src.shared.datahub.client import DataHubClient
from src.shared.db.registry import sync_with_datahub
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


async def _verify_dags(airflow: AirflowClient) -> dict:
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
) -> dict:
    """Verify that all expected Airflow DAGs are loaded and visible."""
    return await _verify_dags(airflow)


@internal_router.post("/dags/verify")
async def internal_verify_dags(
    airflow: AirflowClient = Depends(get_airflow_client),
) -> dict:
    """Verify that all expected Airflow DAGs are loaded (internal — requires X-Internal-Token)."""
    return await _verify_dags(airflow)


@internal_router.post("/datahub/sync")
async def internal_datahub_sync(
    body: DatahubSyncRequest | None = None,
    db: AsyncSession = Depends(get_db),
    datahub: DataHubClient = Depends(get_datahub),
) -> dict:
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


def _dto_to_response(dto: object, updated_at: object) -> RuntimeConfResponse:
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


def _datahub_dto_to_response(
    dto: object | None,
    updated_at: object,
) -> DatahubPeripheralResponse:
    from src.backend.admin.peripheral_service import DatahubConfigDTO

    if dto is None or not isinstance(dto, DatahubConfigDTO):
        return DatahubPeripheralResponse(
            gms_url="",
            kafka_brokers="",
            token="",
            is_configured=False,
            updated_at=updated_at,  # type: ignore[arg-type]
        )
    token_set = datahub_token_is_set()
    return DatahubPeripheralResponse(
        gms_url=dto.gms_url,
        kafka_brokers=dto.kafka_brokers,
        token="********" if token_set else "",
        is_configured=token_set,
        updated_at=updated_at,  # type: ignore[arg-type]
    )


def _langfuse_dto_to_response(
    dto: object | None,
    updated_at: object,
) -> LangfusePeripheralResponse:
    from src.backend.admin.peripheral_service import LangfuseConfigDTO

    if dto is None or not isinstance(dto, LangfuseConfigDTO):
        return LangfusePeripheralResponse(
            host="",
            public_key="",
            secret_key="",
            is_configured=False,
            updated_at=updated_at,  # type: ignore[arg-type]
        )
    secret_set = langfuse_secret_key_is_set()
    return LangfusePeripheralResponse(
        host=dto.host,
        public_key=dto.public_key,
        secret_key="********" if secret_set else "",
        is_configured=secret_set,
        updated_at=updated_at,  # type: ignore[arg-type]
    )


async def _get_peripheral_updated_at(db: AsyncSession, name: str) -> object:
    from sqlalchemy import select

    from src.shared.db.models import PeripheralConfig

    result = await db.execute(
        select(PeripheralConfig).where(PeripheralConfig.name == name)
    )
    row = result.scalar_one_or_none()
    return row.updated_at if row else None


@router.get("/peripherals")
async def get_peripherals_status(
    db: AsyncSession = Depends(get_db),
) -> PeripheralsStatusResponse:
    """Return a brief configured/unconfigured status for each peripheral."""
    datahub_dto = await get_peripheral_config(db, "datahub")
    langfuse_dto = await get_peripheral_config(db, "langfuse")
    return PeripheralsStatusResponse(
        datahub={"is_configured": datahub_dto is not None and datahub_token_is_set()},
        langfuse={"is_configured": langfuse_dto is not None and langfuse_secret_key_is_set()},
    )


@router.get("/peripherals/datahub")
async def get_datahub_peripheral(
    db: AsyncSession = Depends(get_db),
) -> DatahubPeripheralResponse:
    """Return the current DataHub peripheral configuration."""
    dto = await get_peripheral_config(db, "datahub")
    updated_at = await _get_peripheral_updated_at(db, "datahub")
    return _datahub_dto_to_response(dto, updated_at)


async def _apply_datahub_patch_and_respond(
    body: DatahubPeripheralPatchRequest,
    db: AsyncSession,
) -> DatahubPeripheralResponse:
    """Shared handler for admin and internal PATCH /peripherals/datahub endpoints.

    ``token`` is routed to the Kubernetes Secret, never to the DB.
    The Secret write happens first; if it fails (SecretResolverUnavailable →
    StorageUnavailableError → 503) the DB patch is skipped.
    """
    from src.shared.exceptions import StorageUnavailableError

    all_updates = body.model_dump(exclude_unset=True)

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

    db_updates = {k: v for k, v in all_updates.items() if v is not None}
    if db_updates:
        dto = await patch_peripheral_config(db, "datahub", **db_updates)
    else:
        invalidate_peripheral_config_cache("datahub")
        dto = await get_peripheral_config(db, "datahub")

    updated_at = await _get_peripheral_updated_at(db, "datahub")
    return _datahub_dto_to_response(dto, updated_at)


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
    """Apply a partial update to the DataHub peripheral configuration (internal — requires X-Internal-Token).

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
    """Apply a partial update to the Langfuse peripheral configuration (internal — requires X-Internal-Token).

    Intended for install scripts and dev-env seeding.
    """
    return await _apply_langfuse_patch_and_respond(body, db)
