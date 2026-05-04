"""Internal activity endpoints — called by Airflow HTTP operator tasks.

Each endpoint corresponds to an Airflow activity.  Business logic lives in
src/backend/; these endpoints are thin wrappers that translate DataSpokeError
to 400 (non-retryable) or 500 (retryable) HTTP responses, letting Airflow
distinguish between errors worth retrying and permanent failures.

These endpoints are NOT exposed to end users — they are called by the Airflow
orchestrator running inside the same K8s namespace, gated by X-Internal-Token.

Spec: spec/feature/BACKEND.md §DAG Catalogue + §Dependency Injection.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.auth.internal import require_internal_token
from src.api.schemas.admin import DatahubSyncRequest
from src.shared.exceptions import DataSpokeError
from src.workflows._common import (
    make_cache,
    make_datahub,
    make_db_session,
    make_llm,
    make_vector,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/activities",
    tags=[
        "internal/activities/ingestion",
        "internal/activities/validation",
        "internal/activities/metagen",
        "internal/activities/metrics",
        "internal/activities/ontogen",
        "internal/activities/datahub",
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
    """Return dataset URNs with active-custom-mode ingestion configs matching the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService

            datahub = make_datahub()
            service = IngestionService(datahub=datahub, db=db)
            return await service.list_active_for_tier(body.tier)
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


class IngestionRunRequest(BaseModel):
    dataset_urn: str
    dry_run: bool = False


@router.post("/ingestion/run")
async def ingestion_run(body: IngestionRunRequest) -> dict[str, object]:
    """Execute ingestion pipeline for a single dataset."""
    datahub = make_datahub()
    cache = make_cache()
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService

            service = IngestionService(datahub=datahub, db=db, cache=cache)
            result = await service.run(body.dataset_urn, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


@router.post("/ingestion/passive-sync")
async def ingestion_passive_sync() -> dict[str, object]:
    """Mirror DataHub run history for all passive-mode configs into the events table.

    Called hourly by the ingestion-passive-hourly DAG.
    """
    datahub = make_datahub()
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService

            service = IngestionService(datahub=datahub, db=db)
            await service.sync_passive_status()
            return {"status": "ok"}
    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]


# ── /validation ───────────────────────────────────────────────────────────────


class ValidationListActiveRequest(BaseModel):
    tier: str


@router.post("/validation/list-active")
async def validation_list_active(body: ValidationListActiveRequest) -> list[str]:
    """Return dataset URNs with is_enabled validation configs matching the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.validation.service import ValidationService

            datahub = make_datahub()
            cache = make_cache()
            service = ValidationService(datahub=datahub, db=db, cache=cache)
            return await service.list_active_for_tier(body.tier)
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


class ValidationRunRequest(BaseModel):
    dataset_urn: str


@router.post("/validation/run")
async def validation_run(body: ValidationRunRequest) -> dict[str, object]:
    """Execute validation pipeline for a single dataset."""
    datahub = make_datahub()
    cache = make_cache()
    try:
        async with make_db_session() as db:
            from src.backend.validation.service import ValidationService

            service = ValidationService(datahub=datahub, db=db, cache=cache)
            summary = await service.run(body.dataset_urn)
            return {
                "run_id": summary.run_id,
                "status": summary.status,
                "total": summary.total,
                "passed": summary.passed,
                "failed": summary.failed,
                "errored": summary.errored,
            }
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


# ── /metagen ──────────────────────────────────────────────────────────────────


class MetagenListActiveRequest(BaseModel):
    tier: str


@router.post("/metagen/list-active")
async def metagen_list_active(body: MetagenListActiveRequest) -> list[str]:
    """Return dataset URNs with is_enabled metagen configs matching the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.metagen.service import MetagenService

            datahub = make_datahub()
            llm = make_llm()
            service = MetagenService(datahub=datahub, db=db, llm=llm)
            configs = await service.list_active_for_tier(body.tier)
            return [c.dataset_urn for c in configs]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


class MetagenRunRequest(BaseModel):
    dataset_urn: str
    dry_run: bool = False


@router.post("/metagen/run")
async def metagen_run(body: MetagenRunRequest) -> dict[str, object]:
    """Execute metadata generation pipeline for a single dataset."""
    datahub = make_datahub()
    llm = make_llm()
    try:
        async with make_db_session() as db:
            from src.backend.metagen.service import MetagenService

            service = MetagenService(datahub=datahub, db=db, llm=llm)
            result = await service.run(body.dataset_urn, dry_run=body.dry_run)
            return {
                "id": result.id,
                "dataset_urn": result.dataset_urn,
                "run_id": result.run_id,
                "status": "success",
            }
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


# ── /metrics ──────────────────────────────────────────────────────────────────


class MetricsListActiveRequest(BaseModel):
    tier: str


@router.post("/metrics/list-active")
async def metrics_list_active(body: MetricsListActiveRequest) -> list[str]:
    """Return metric IDs with is_enabled=True and schedule_tier matching the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.metrics.service import MetricsService

            datahub = make_datahub()
            cache = make_cache()
            service = MetricsService(datahub=datahub, db=db, cache=cache)
            records = await service.list_active_for_tier(body.tier)
            return [r.id for r in records]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


class MetricsRunRequest(BaseModel):
    metric_id: str
    dry_run: bool = False


@router.post("/metrics/run")
async def metrics_run(body: MetricsRunRequest) -> dict[str, object]:
    """Execute metric measurement run for a single metric."""
    datahub = make_datahub()
    cache = make_cache()
    try:
        async with make_db_session() as db:
            from src.backend.metrics.service import MetricsService

            service = MetricsService(datahub=datahub, db=db, cache=cache)
            result = await service.run(body.metric_id, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


# ── /ontogen ──────────────────────────────────────────────────────────────────


class OntogenRunRequest(BaseModel):
    dry_run: bool = False
    prompt_md: str | None = None


@router.post("/ontogen/run")
async def ontogen_run(body: OntogenRunRequest) -> dict[str, object]:
    """Execute the ontogen inference pipeline.

    Called by the ontogen tier DAGs and the on-demand ontogen DAG.
    Tier-based DAGs supply no prompt_md (falls back to conf.default_run_prompt).
    """
    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    vector = make_vector()
    try:
        async with make_db_session() as db:
            from src.backend.ontogen.service import OntogenService
            from src.shared.db.session import SessionLocal
            from src.shared.graph.client import AgeGraph

            age = AgeGraph(session_factory=SessionLocal)
            service = OntogenService(
                datahub=datahub,
                db=db,
                cache=cache,
                llm=llm,
                age=age,
                vector=vector,
            )
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
    datahub = make_datahub()
    try:
        async with make_db_session() as db:
            from src.shared.db.registry import sync_with_datahub

            result = await sync_with_datahub(
                db=db,
                datahub=datahub,
                dataset_urns=body.dataset_urns,
            )
            await db.commit()
            return result
    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]
