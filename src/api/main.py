"""DataSpoke API — FastAPI application factory."""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
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
from src.shared.exceptions import (
    AuthenticationError,
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    NotImplementedAPIError,
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
    app.state.llm = LLMClient(
        settings.llm_provider,
        settings.llm_api_key,
        settings.llm_model,
        langfuse_host=settings.langfuse_host,
        langfuse_public_key=settings.langfuse_public_key,
        langfuse_secret_key=settings.langfuse_secret_key,
    )

    logger.info(
        "lifespan_startup_complete",
        extra={"clients": ["airflow", "datahub", "redis", "vector", "llm"]},
    )

    # Seed factory-default metric definitions (idempotent)
    try:
        from src.backend.metrics.bootstrap import seed_factory_defaults

        async with SessionLocal() as seed_session:
            await seed_factory_defaults(seed_session)
    except Exception:
        logger.warning("metrics_bootstrap_seed_failed", exc_info=True)

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


def _resp_time() -> str:
    """ISO 8601 UTC timestamp with millisecond precision (matches spec §Date/Time)."""
    now = datetime.now(tz=UTC)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S.')}{now.microsecond // 1000:03d}Z"


def _error_json(
    request: Request,
    status: int,
    error_code: str,
    message: str,
    headers: dict[str, str] | None = None,
    detail: dict[str, object] | None = None,
) -> JSONResponse:
    trace_id = request.headers.get(_TRACE_HEADER, "")
    body: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "trace_id": trace_id,
        "resp_time": _resp_time(),
    }
    if detail:
        body["detail"] = detail
    return JSONResponse(
        status_code=status,
        content=body,
        headers=headers,
    )


async def _handle_rate_limit(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return the standard error envelope on 429 plus SlowAPI's rate-limit headers.

    SlowAPI's default handler emits a plain text body. We wrap it so clients see
    the standard {error_code, message, trace_id, resp_time} envelope while keeping
    SlowAPI's X-RateLimit-* and Retry-After headers via _inject_headers.
    """
    response = _error_json(
        request,
        429,
        "RATE_LIMIT_EXCEEDED",
        f"Rate limit exceeded: {exc.detail}" if getattr(exc, "detail", None) else "Rate limit exceeded.",
    )
    view_rate_limit = getattr(request.state, "view_rate_limit", None)
    if view_rate_limit is not None:
        response = request.app.state.limiter._inject_headers(response, view_rate_limit)
    return response


async def _handle_not_found(request: Request, exc: EntityNotFoundError) -> JSONResponse:
    return _error_json(request, 404, exc.error_code, str(exc))


async def _handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
    return _error_json(request, 409, exc.error_code, str(exc))


async def _handle_precondition(request: Request, exc: PreconditionFailedError) -> JSONResponse:
    return _error_json(request, 422, exc.error_code, str(exc), detail=exc.detail or None)


async def _handle_invalid_dataset_urn(
    request: Request, exc: InvalidDatasetUrnError
) -> JSONResponse:
    return _error_json(request, 422, exc.error_code, str(exc))


async def _handle_authentication(request: Request, exc: AuthenticationError) -> JSONResponse:
    return _error_json(request, 401, exc.error_code, str(exc))


async def _handle_validation(request: Request, exc: PydanticValidationError) -> JSONResponse:
    return _error_json(request, 422, "INVALID_PARAMETER", str(exc))


async def _handle_datahub(request: Request, exc: DataHubUnavailableError) -> JSONResponse:
    logger.warning(
        "datahub_unavailable",
        extra={"detail": str(exc), "path": request.url.path},
    )
    return _error_json(
        request,
        502,
        exc.error_code,
        "DataHub temporarily unavailable; please retry",
    )


async def _handle_storage(request: Request, exc: StorageUnavailableError) -> JSONResponse:
    return _error_json(request, 503, exc.error_code, str(exc))


async def _handle_not_implemented(request: Request, exc: NotImplementedAPIError) -> JSONResponse:
    return _error_json(request, 501, exc.error_code, str(exc))


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
                "Passive validation result store. DataSpoke runs no rule logic — "
                "external pipelines compute results and POST them, and DataSpoke "
                "stores the configuration plus the result timeseries and emits the "
                "matching DataHub assertion aspects on the pipeline's behalf. "
                "Requires common auth.\n\n"
                "One slot per dataset. Configuration is `description` + declared "
                "`variables[]` (each name matches `[a-z][a-z0-9_]{0,99}`, 1–200 "
                "entries). Each `POST .../attr/validation/result` carries "
                "`{data_time, score, variables}`. The historical GET serves the "
                "result timeseries as a baseline cache.\n\n"
                "**Where to look:** `spec/feature/VALIDATION.md` (philosophy, scope, "
                "API surface, configuration / result shapes, DataHub aspect mapping)."
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
                "Requires DG auth (dg/admin groups). Built-in metric types: "
                "ingestion-freshness, validation-score, doc-health. "
                "See spec/feature/BACKEND.md §Metrics Service for measurer semantics, "
                "dataset_filter shape, and breakdown format."
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
    app.add_exception_handler(RateLimitExceeded, _handle_rate_limit)  # type: ignore[arg-type]

    # ── Exception handlers (specific → generic) ───────────────────────────────
    app.add_exception_handler(NotImplementedAPIError, _handle_not_implemented)  # type: ignore[arg-type]
    app.add_exception_handler(EntityNotFoundError, _handle_not_found)  # type: ignore[arg-type]
    app.add_exception_handler(ConflictError, _handle_conflict)  # type: ignore[arg-type]
    app.add_exception_handler(PreconditionFailedError, _handle_precondition)  # type: ignore[arg-type]
    app.add_exception_handler(InvalidDatasetUrnError, _handle_invalid_dataset_urn)  # type: ignore[arg-type]
    app.add_exception_handler(AuthenticationError, _handle_authentication)  # type: ignore[arg-type]
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

    # ── Hub pass-through routes ────────────────────────────────────────────────
    app.include_router(hub_router.router, prefix=API_PREFIX)

    # ── Admin routes (admin group only) ───────────────────────────────────────
    app.include_router(admin_router.router, prefix=API_PREFIX)

    return app


app = create_app()
