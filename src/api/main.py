"""DataSpoke API — FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.api.config import settings
from src.api.middleware.logging import RequestLoggingMiddleware
from src.api.middleware.rate_limit import limiter
from src.api.routers import admin as admin_router
from src.api.routers import auth as auth_router
from src.api.routers import health
from src.api.routers import hub as hub_router
from src.api.routers.internal import activities as internal_activities
from src.api.routers.spoke.common import (
    data as common_data,
)
from src.api.routers.spoke.common import (
    ingestion as common_ingestion,
)
from src.api.routers.spoke.common import (
    metagen as common_metagen,
)
from src.api.routers.spoke.common import (
    ontogen as common_ontogen,
)
from src.api.routers.spoke.common import (
    validation as common_validation,
)
from src.api.routers.spoke.dg import metrics as dg_metrics
from src.api.routers.spoke.dg import overview as dg_overview
from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
    PreconditionFailedError,
    StorageUnavailableError,
)

logger = logging.getLogger(__name__)

_TRACE_HEADER = "X-Trace-Id"

API_PREFIX = "/api/v1"
SPOKE_COMMON = f"{API_PREFIX}/spoke/common"
SPOKE_DG = f"{API_PREFIX}/spoke/dg"
HUB = f"{API_PREFIX}/hub"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — construct and close shared infrastructure clients."""
    from src.shared.cache.client import RedisClient
    from src.shared.datahub.client import DataHubClient
    from src.shared.db.session import SessionLocal
    from src.shared.llm.client import LLMClient
    from src.shared.vector.client import PgVectorManager
    from src.workflows.airflow.client import AirflowClient

    if settings.enable_stub_auth and settings.admin_password == "admin":
        logger.warning(
            "stub_auth_enabled_with_default_password",
            extra={"risk": "Stub auth enabled with default admin password — not for production."},
        )

    app.state.airflow = AirflowClient(
        base_url=settings.airflow_url,
        username=settings.airflow_user,
        password=settings.airflow_password,
    )
    app.state.datahub = DataHubClient(settings.datahub_gms_url, settings.datahub_token)
    app.state.redis = RedisClient(settings.redis_host, settings.redis_port, settings.redis_password)
    app.state.vector = PgVectorManager(session_factory=SessionLocal)
    app.state.llm = LLMClient(settings.llm_provider, settings.llm_api_key, settings.llm_model)

    logger.info(
        "lifespan_startup_complete",
        extra={"clients": ["airflow", "datahub", "redis", "vector", "llm"]},
    )
    try:
        yield
    finally:
        for name in ("airflow", "redis"):
            client = getattr(app.state, name, None)
            if client is None:
                continue
            try:
                await client.close()
            except Exception:
                logger.warning(
                    "lifespan_shutdown_close_failed",
                    extra={"client": name},
                    exc_info=True,
                )
        # DataHubClient, PgVectorManager, LLMClient have no close() — rely on GC.
        logger.info("lifespan_shutdown_complete")


def _error_json(request: Request, status: int, error_code: str, message: str) -> JSONResponse:
    trace_id = request.headers.get(_TRACE_HEADER, "")
    return JSONResponse(
        status_code=status,
        content={"error_code": error_code, "message": message, "trace_id": trace_id},
    )


async def _handle_not_found(request: Request, exc: EntityNotFoundError) -> JSONResponse:
    return _error_json(request, 404, exc.error_code, str(exc))


async def _handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
    return _error_json(request, 409, exc.error_code, str(exc))


async def _handle_precondition(request: Request, exc: PreconditionFailedError) -> JSONResponse:
    return _error_json(request, 422, exc.error_code, str(exc))


async def _handle_validation(request: Request, exc: PydanticValidationError) -> JSONResponse:
    return _error_json(request, 422, "INVALID_PARAMETER", str(exc))


async def _handle_datahub(request: Request, exc: DataHubUnavailableError) -> JSONResponse:
    return _error_json(request, 502, exc.error_code, str(exc))


async def _handle_storage(request: Request, exc: StorageUnavailableError) -> JSONResponse:
    return _error_json(request, 503, exc.error_code, str(exc))


async def _handle_dataspoke_generic(request: Request, exc: DataSpokeError) -> JSONResponse:
    return _error_json(request, 500, exc.error_code, str(exc))


def create_app() -> FastAPI:
    openapi_tags = [
        {
            "name": "common/ingestion",
            "description": (
                "Ingestion config CRUD and run operations. Requires common auth "
                "(de/da/dg/admin groups). See spec/feature/BACKEND.md §Ingestion Service "
                "for the active/passive split, extractor model, and aspect emission details."
            ),
            "externalDocs": {
                "description": "DataHub Dataset Entity — aspect catalog and REST endpoints",
                "url": "https://datahubproject.io/docs/generated/metamodel/entities/dataset",
            },
        },
        {
            "name": "common/validation",
            "description": (
                "Validation config CRUD, run, and result queries. Requires common auth. "
                "See spec/feature/BACKEND.md §Validation Service for rule types, "
                "partition semantics, and DataHub assertion mapping."
            ),
            "externalDocs": {
                "description": (
                    "DataHub Assertion Entity — assertionInfo and assertionRunEvent aspects"
                ),
                "url": "https://datahubproject.io/docs/generated/metamodel/entities/assertion",
            },
        },
        {
            "name": "common/metagen",
            "description": (
                "AI metadata generation config CRUD, run, and result review. "
                "Requires common auth."
            ),
        },
        {
            "name": "common/data",
            "description": (
                "Dataset overview with embedded ingestion, validation, and metagen "
                "sub-resources. Requires common auth."
            ),
        },
        {
            "name": "common/ontogen",
            "description": (
                "Ontology generation config, seed management, run, and node/edge/triple "
                "graph view. Requires common auth."
            ),
        },
        {
            "name": "dg/metric",
            "description": (
                "Governance metric definitions, measurement results, and scheduling. "
                "Requires DG auth (dg/admin groups). See spec/feature/BACKEND.md §Metrics Service "
                "for aggregation semantics, measurement_query shape (aggregation key), "
                "dataset_filter, and breakdown format."
            ),
        },
        {
            "name": "dg/overview",
            "description": (
                "Data governance overview dashboard and lineage graph. Requires DG auth."
            ),
        },
        {
            "name": "hub",
            "description": "Pass-through proxy to DataHub GMS GraphQL. Requires common auth.",
        },
        {
            "name": "auth",
            "description": "JWT token management. No authentication required.",
        },
        {
            "name": "system",
            "description": "Health and readiness checks. No authentication required.",
        },
    ]

    app = FastAPI(
        title="DataSpoke API",
        version="0.1.0",
        description="Sidecar extension to DataHub — DataSpoke API server.",
        docs_url=None,
        redoc_url="/redoc",
        lifespan=lifespan,
        openapi_tags=openapi_tags,
    )

    # ── State (needed by slowapi) ──────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # ── Exception handlers (specific → generic) ───────────────────────────────
    app.add_exception_handler(EntityNotFoundError, _handle_not_found)  # type: ignore[arg-type]
    app.add_exception_handler(ConflictError, _handle_conflict)  # type: ignore[arg-type]
    app.add_exception_handler(PreconditionFailedError, _handle_precondition)  # type: ignore[arg-type]
    app.add_exception_handler(PydanticValidationError, _handle_validation)  # type: ignore[arg-type]
    app.add_exception_handler(DataHubUnavailableError, _handle_datahub)  # type: ignore[arg-type]
    app.add_exception_handler(StorageUnavailableError, _handle_storage)  # type: ignore[arg-type]
    app.add_exception_handler(DataSpokeError, _handle_dataspoke_generic)  # type: ignore[arg-type]

    # ── Middleware (applied bottom-up; order matches spec/feature/API.md) ──────
    # 5. Rate limiting
    app.add_middleware(SlowAPIMiddleware)
    # 2. Request logging (also adds trace ID header)
    app.add_middleware(RequestLoggingMiddleware)
    # 1. CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # NOTE: Group enforcement and JWT validation are handled by FastAPI
    # route-level Depends (require_common, require_dg, etc.) rather than
    # blanket Starlette middleware, which keeps the auth logic testable and
    # allows the public /health, /ready, and /auth/* routes to bypass it
    # without a separate exclusion list.

    # ── System routes (no auth) ────────────────────────────────────────────────
    app.include_router(health.router)

    # ── Auth routes (no auth required) ────────────────────────────────────────
    app.include_router(auth_router.router, prefix=API_PREFIX)

    # ── Internal endpoints (called by Airflow / scripts, no auth) ────────────────
    app.include_router(internal_activities.router, include_in_schema=False)
    app.include_router(admin_router.internal_router, include_in_schema=False)

    # ── Spoke/common routes ────────────────────────────────────────────────────
    app.include_router(common_ontogen.router, prefix=SPOKE_COMMON)
    app.include_router(common_data.router, prefix=SPOKE_COMMON)
    app.include_router(common_ingestion.router, prefix=SPOKE_COMMON)
    app.include_router(common_validation.router, prefix=SPOKE_COMMON)
    app.include_router(common_metagen.router, prefix=SPOKE_COMMON)

    # ── Spoke/dg routes ────────────────────────────────────────────────────────
    app.include_router(dg_metrics.router, prefix=SPOKE_DG)
    app.include_router(dg_overview.router, prefix=SPOKE_DG)

    # ── Hub pass-through routes ────────────────────────────────────────────────
    app.include_router(hub_router.router, prefix=API_PREFIX)

    # ── Admin routes (admin group only) ───────────────────────────────────────
    app.include_router(admin_router.router, prefix=API_PREFIX)

    return app


app = create_app()
