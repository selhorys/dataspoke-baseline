"""DataSpoke API — FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.api.config import settings
from src.api.middleware.logging import RequestLoggingMiddleware
from src.api.middleware.rate_limit import limiter
from src.api.routers import auth as auth_router
from src.api.routers import health
from src.api.routers import hub as hub_router
from src.api.routers.spoke.common import (
    data as common_data,
)
from src.api.routers.spoke.common import (
    gen as common_gen,
)
from src.api.routers.spoke.common import (
    ingestion as common_ingestion,
)
from src.api.routers.spoke.common import (
    ontology as common_ontology,
)
from src.api.routers.spoke.common import (
    search as common_search,
)
from src.api.routers.spoke.common import (
    validation as common_validation,
)
from src.api.routers.spoke.dg import metrics as dg_metrics
from src.api.routers.spoke.dg import overview as dg_overview

API_PREFIX = "/api/v1"
SPOKE_COMMON = f"{API_PREFIX}/spoke/common"
SPOKE_DG = f"{API_PREFIX}/spoke/dg"
HUB = f"{API_PREFIX}/hub"


def create_app() -> FastAPI:
    app = FastAPI(
        title="DataSpoke API",
        version="0.1.0",
        description="Sidecar extension to DataHub — DataSpoke API server.",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── State (needed by slowapi) ──────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # ── Middleware (applied bottom-up; order matches spec/feature/API.md) ──────
    # 5. Rate limiting
    app.add_middleware(SlowAPIMiddleware)
    # 2. Request logging (also adds trace ID header)
    app.add_middleware(RequestLoggingMiddleware)
    # 1. CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
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

    # ── Spoke/common routes ────────────────────────────────────────────────────
    app.include_router(common_ontology.router, prefix=SPOKE_COMMON)
    app.include_router(common_data.router, prefix=SPOKE_COMMON)
    app.include_router(common_ingestion.router, prefix=SPOKE_COMMON)
    app.include_router(common_validation.router, prefix=SPOKE_COMMON)
    app.include_router(common_gen.router, prefix=SPOKE_COMMON)
    app.include_router(common_search.router, prefix=SPOKE_COMMON)

    # ── Spoke/dg routes ────────────────────────────────────────────────────────
    app.include_router(dg_metrics.router, prefix=SPOKE_DG)
    app.include_router(dg_overview.router, prefix=SPOKE_DG)

    # ── Hub pass-through routes ────────────────────────────────────────────────
    app.include_router(hub_router.router, prefix=API_PREFIX)

    return app


app = create_app()
