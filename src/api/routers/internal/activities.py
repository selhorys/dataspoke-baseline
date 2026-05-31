"""Internal activity endpoints — called by Airflow HTTP operator tasks.

Each endpoint corresponds to an Airflow activity.  Business logic lives in
src/backend/; these endpoints are thin wrappers that translate DataSpokeError
to 400 (non-retryable) or 500 (retryable) HTTP responses, letting Airflow
distinguish between errors worth retrying and permanent failures.

These endpoints are NOT exposed to end users — they are called by the Airflow
orchestrator running inside the same K8s namespace, gated by X-Internal-Token.

Activities:
  /ingestion/list-active  — list source IDs with ACTIVE_CUSTOM_MANAGED configs for a tier
  /ingestion/run          — execute ingestion pipeline for a single source
  /ingestion/sync         — reconcile all ingestion sources against DataHub (hourly sweep)
  /metagen/run            — execute global metagen inference pipeline (singleton)
  /metrics/list-active    — list metric IDs with is_enabled=True for a tier
  /metrics/run            — execute metric measurement for a single metric
  /ontogen/run            — execute the ontogen inference pipeline (singleton)
  /datahub/sync           — reconcile dataset_registry against live DataHub URN set
  /auth/role-sync         — reconcile DataHub-side role assignments against users.role

Spec: spec/feature/BACKEND.md §DAG Catalogue + §Dependency Injection.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.auth.internal import require_internal_token
from src.api.schemas.admin import DatahubSyncRequest
from src.shared.exceptions import ConflictError, DataSpokeError
from src.workflows._common import (
    make_datahub,
    make_db_session,
    make_llm_client,
    make_pgvector_manager,
    make_redis_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/activities",
    tags=[
        "internal/activities/ingestion",
        "internal/activities/metagen",
        "internal/activities/metrics",
        "internal/activities/ontogen",
        "internal/activities/datahub",
        "internal/activities/auth",
    ],
    dependencies=[Depends(require_internal_token)],
)


def _error_response(exc: Exception, non_retryable: bool = True) -> JSONResponse:
    """Map DataSpokeError → 400 (non-retryable) or 500 (retryable)."""
    error_code = getattr(exc, "error_code", "INTERNAL_ERROR")
    status_code = 400 if non_retryable else 500
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "message": str(exc), "non_retryable": non_retryable},
    )


# ── /ingestion ────────────────────────────────────────────────────────────────


class IngestionListActiveRequest(BaseModel):
    tier: str  # "hourly" | "daily" | "weekly"


@router.post("/ingestion/list-active")
async def ingestion_list_active(body: IngestionListActiveRequest) -> list[str]:
    """Return source IDs with ACTIVE_CUSTOM_MANAGED ingestion configs for the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService

            datahub = await make_datahub(db)
            service = IngestionService(datahub=datahub, db=db)
            records = await service.list_active_sources_for_tier(body.tier)
            return [r.id for r in records]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


class IngestionRunRequest(BaseModel):
    source_id: str
    dry_run: bool = False


@router.post("/ingestion/run")
async def ingestion_run(body: IngestionRunRequest) -> dict[str, object]:
    """Execute ingestion pipeline for a single ACTIVE_CUSTOM_MANAGED source."""
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.ingestion.service import IngestionService

            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            datahub = await make_datahub(db)
            service = IngestionService(datahub=datahub, db=db, cache=cache)
            result = await service.run(source_id=body.source_id, dry_run=body.dry_run)
            return {
                "run_id": result.run_id,
                "status": result.status,
                "entities_ingested": result.entities_ingested,
                "dry_run": result.dry_run,
                "errors": result.errors,
                "warnings": result.warnings,
            }
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


@router.post("/ingestion/sync")
async def ingestion_sync() -> dict[str, object]:
    """Reconcile all ingestion sources against DataHub.

    Called hourly by the ingestion-sync-hourly DAG. Runs the five-step sync
    pipeline (source defs, mapping, observed enrichment, run events, unmanaged
    bucket) and returns a summary dict.

    Retryable: DataHub transient failures surface as 500 so Airflow retries.

    Returns:
        {sources_synced, sources_removed, datasets_mapped, pipeline_links, events_mirrored}
    """
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService
            from src.shared.exceptions import DataHubUnavailableError

            datahub = await make_datahub(db)
            service = IngestionService(datahub=datahub, db=db)
            summary = await service.sync()
            return summary
    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]


# ── /metagen ──────────────────────────────────────────────────────────────────


class MetagenRunRequest(BaseModel):
    # Internal variant; public counterpart is src/api/schemas/metagen.MetagenRunRequest (no `tier`).
    tier: str | None = None
    dataset_urns: list[str] | None = None
    dry_run: bool = False


@router.post("/metagen/run")
async def metagen_run(body: MetagenRunRequest) -> dict[str, object]:
    """Execute the metagen pipeline (singleton).

    Called by the three metagen tier DAGs. Each tier DAG supplies ``tier``;
    the activity short-circuits when ``tier`` does not match
    ``metagen_config.schedule_tier`` so only the one DAG matching the conf
    actually runs. Manual API calls (``POST /spoke/metagen/method/run``)
    call MetagenService.run() directly in-process.

    Spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection.
    """
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.metagen.service import MetagenService

            datahub = await make_datahub(db)
            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            vector = make_pgvector_manager(stub=rc.stub_pgvector_manager)
            llm = make_llm_client(stub=rc.stub_llm_client, provider=rc.llm_provider, model=rc.llm_model)
            service = MetagenService(datahub=datahub, db=db, cache=cache, llm=llm, vector=vector)

            if body.tier is not None:
                conf = await service.get_global_conf()
                conf_tier = conf.schedule_tier if conf is not None else None
                if body.tier != conf_tier:
                    return {
                        "status": "skipped",
                        "reason": "tier_mismatch",
                        "dag_tier": body.tier,
                        "conf_tier": conf_tier,
                    }

            result = await service.run(
                dataset_urns=body.dataset_urns,
                dry_run=body.dry_run,
            )
            return result.model_dump()
    except DataSpokeError as exc:
        non_retryable = exc.error_code != "METAGEN_RUNNING" if hasattr(exc, "error_code") else True
        return _error_response(exc, non_retryable=non_retryable)  # type: ignore[return-value]


# ── /metrics ──────────────────────────────────────────────────────────────────


class MetricsListActiveRequest(BaseModel):
    tier: str


@router.post("/metrics/list-active")
async def metrics_list_active(body: MetricsListActiveRequest) -> list[str]:
    """Return metric IDs with is_enabled=True and schedule_tier matching the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.metrics.service import MetricsService

            rc = await get_runtime_config(db)
            datahub = await make_datahub(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            service = MetricsService(datahub=datahub, db=db, cache=cache)
            records = await service.list_active_for_tier(body.tier)
            return [r.id for r in records]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


_METRIC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$"


class MetricsRunRequest(BaseModel):
    metric_id: str = Field(
        pattern=_METRIC_ID_PATTERN,
        max_length=64,
    )
    dry_run: bool = False


@router.post("/metrics/run")
async def metrics_run(body: MetricsRunRequest) -> dict[str, object]:
    """Execute metric measurement run for a single metric."""
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.metrics.service import MetricsService

            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            datahub = await make_datahub(db)
            service = MetricsService(datahub=datahub, db=db, cache=cache)
            result = await service.run(body.metric_id, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except ConflictError as exc:
        if exc.error_code == "METRIC_RUNNING":
            # Return HTTP 200 with structured error payload so the DAG task stays
            # green; the calling route inspects dag_run.conf to translate to 409.
            return {
                "run_id": "",
                "status": "error",
                "detail": {"error_code": "METRIC_RUNNING", "message": str(exc)},
            }
        return _error_response(exc)  # type: ignore[return-value]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


# ── /ontogen ──────────────────────────────────────────────────────────────────


class OntogenRunRequest(BaseModel):
    dry_run: bool = False
    prompt_md: str | None = None
    tier: str | None = None  # set by periodic tier DAGs; None for on-demand DAG


@router.post("/ontogen/run")
async def ontogen_run(body: OntogenRunRequest) -> dict[str, object]:
    """Execute the ontogen inference pipeline.

    Called by the three ontogen tier DAGs. Each tier DAG supplies ``tier``;
    the activity short-circuits when ``tier`` does not match
    ``ontogen_config.schedule_tier`` so only the one DAG matching the conf
    actually runs. Manual API calls (``POST /spoke/ontogen/method/run``)
    call OntogenService.run() directly in-process.

    Spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection.
    """
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.ontogen.service import OntogenService

            datahub = await make_datahub(db)
            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            vector = make_pgvector_manager(stub=rc.stub_pgvector_manager)
            llm = make_llm_client(stub=rc.stub_llm_client, provider=rc.llm_provider, model=rc.llm_model)
            service = OntogenService(
                datahub=datahub,
                db=db,
                cache=cache,
                llm=llm,
                vector=vector,
            )

            if body.tier is not None:
                conf = await service.get_conf()
                if body.tier != conf.schedule_tier:
                    return {
                        "status": "skipped",
                        "reason": "tier_mismatch",
                        "dag_tier": body.tier,
                        "conf_tier": conf.schedule_tier,
                    }

            summary = await service.run(prompt_md=body.prompt_md, dry_run=body.dry_run)
            return {
                "status": summary.status,
                "dry_run": summary.dry_run,
                "unresolved_urns": summary.unresolved_urns,
                "counts": summary.counts,
            }
    except DataSpokeError as exc:
        # ONTOGEN_RUNNING (409) → retryable = True (Airflow will retry)
        non_retryable = exc.error_code != "ONTOGEN_RUNNING" if hasattr(exc, "error_code") else True
        return _error_response(exc, non_retryable=non_retryable)  # type: ignore[return-value]


# ── /datahub ──────────────────────────────────────────────────────────────────


@router.post("/datahub/sync")
async def datahub_sync(body: DatahubSyncRequest) -> dict[str, object]:
    """Reconcile dataset_registry.datahub_registered against the live DataHub URN set.

    Called daily by the datahub-sync-daily DAG (unparameterized = full sweep).
    Accepts an optional dataset_urns list for a targeted sweep.
    """
    try:
        async with make_db_session() as db:
            from src.shared.db.registry import sync_with_datahub

            datahub = await make_datahub(db)
            result = await sync_with_datahub(
                db=db,
                datahub=datahub,
                dataset_urns=body.dataset_urns,
            )
            await db.commit()
            return result
    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]


# ── /auth ─────────────────────────────────────────────────────────────────────


@router.post("/auth/role-sync")
async def auth_role_sync() -> dict[str, object]:
    """Reconcile DataHub-side role assignments against DataSpoke users.role (SSOT).

    Called daily by the auth-role-sync-daily DAG.

    Algorithm:
      1. SELECT all rows from users ordered by id.
         (Baseline uses a simple full-scan; for large deployments the optimisation
         path is scrollAcrossEntities on the marker corpGroup instead.)
      2. For each user, read the DataHub-side role via the RoleMembership aspect
         (atomic single-role per DataHub RoleService).
      3. On divergence (DataHub role differs from users.role, or no role assigned),
         re-assert users.role to DataHub via batchAssignRole. DataSpoke wins.
      4. Emit an AUTH.ROLE_SYNC_FIXED event row per fixed user.
      5. Per-user DataHubUnavailableError is logged and counted — the batch
         continues; the whole transaction commits at the end.

    Note: every row in users is in-scope because DataSpoke is the SSOT for
    which corpusers are managed. The marker-group filter is implicit — every
    row in users was mirrored into the marker group at registration time.

    Returns {checked, fixed, errors}.

    Spec: spec/feature/AUTH.md §Role Drift Reconciliation,
          spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation.
    """
    from sqlalchemy import select

    from src.backend.datahub import users as dh_users
    from src.shared.db.models import Event, User
    from src.shared.exceptions import DataHubUnavailableError

    checked = 0
    fixed = 0
    errors = 0
    event_rows: list[Event] = []

    try:
        async with make_db_session() as db:
            datahub = await make_datahub(db)

            result = await db.execute(select(User).order_by(User.id))
            users = result.scalars().all()

            for user in users:
                checked += 1
                urn = f"urn:li:corpuser:{user.email}"

                try:
                    observed_role = await dh_users.read_role(datahub, urn)
                except DataHubUnavailableError:
                    logger.warning(
                        "auth_role_sync: DataHub unavailable for user",
                        extra={"user_id": str(user.id), "corpuser_urn": urn},
                    )
                    errors += 1
                    continue

                if observed_role != user.role:
                    try:
                        await dh_users.propagate_role(datahub, urn, user.role)
                    except DataHubUnavailableError:
                        logger.warning(
                            "auth_role_sync: DataHub unavailable propagating role for user",
                            extra={"user_id": str(user.id), "corpuser_urn": urn},
                        )
                        errors += 1
                        continue

                    fixed += 1
                    event_rows.append(
                        Event(
                            entity_type="user",
                            entity_id=str(user.id),
                            event_type="AUTH.ROLE_SYNC_FIXED",
                            status="OK",
                            detail={
                                "dataspoke_role_authoritative": user.role,
                                "datahub_role_observed": observed_role,
                            },
                        )
                    )

            # Batch all event inserts in a single transaction commit.
            if event_rows:
                db.add_all(event_rows)
            await db.commit()

    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]

    return {"checked": checked, "fixed": fixed, "errors": errors}
