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
from src.api.routers import auth as auth_router
from src.api.routers import health
from src.api.routers import hub as hub_router
from src.api.routers.internal import activities as internal_activities
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
from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
    PreconditionError,
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
    """Register Kestra flows at startup, close client on shutdown."""
    import asyncio

    from src.api.dependencies import get_kestra_client
    from src.workflows.kestra.registry import register_all_flows

    kestra = get_kestra_client()
    try:
        count = await asyncio.wait_for(register_all_flows(kestra), timeout=120)
        logger.info("Registered %d Kestra flows", count)
    except asyncio.TimeoutError:
        logger.warning("Kestra flow registration timed out — Kestra may be unavailable")
    except Exception:
        logger.warning("Failed to register Kestra flows (Kestra may not be available)", exc_info=True)

    yield

    await kestra.close()


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


async def _handle_precondition(request: Request, exc: PreconditionError) -> JSONResponse:
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
                "Ingestion config CRUD and run operations. Requires common auth (de/da/dg/admin groups).\n\n"
                "DataSpoke ingestion implements a **source-agnostic metadata extraction** pattern built on "
                "[DataHub's entity-aspect model](https://datahubproject.io/docs/what/aspect). "
                "Each dataset in DataHub is described by composable *aspects* — typed metadata facets. "
                "DataSpoke's ingestion pipeline connects to heterogeneous data sources, discovers schema "
                "metadata, and expresses the results as standard DataHub aspects via the REST Emitter API.\n\n"
                "## DataSpoke vs DataHub Native Ingestion\n\n"
                "| Concern | DataHub Native Ingestion | DataSpoke Ingestion |\n"
                "|---------|--------------------------|---------------------|\n"
                "| Trigger | CLI batch (`datahub ingest`) | HTTP API + Kestra cron |\n"
                "| Configuration | YAML recipes | JSONB config in PostgreSQL |\n"
                "| Source plugins | 200+ community connectors | Focused extractors (extensible) |\n"
                "| Output | Aspects + lineage + profiling | Core aspects (Status, Properties, Schema) |\n\n"
                "## Source Abstraction\n\n"
                "The `platform` / `locator` / `identifier` / `auth` model provides a uniform interface "
                "across data platforms. Each `platform` maps to a dedicated extractor that handles "
                "connection, schema discovery, and type mapping.\n\n"
                "| Platform | Status | Locator | Identifier |\n"
                "|----------|--------|---------|------------|\n"
                "| **postgres** | Implemented | host, port | database, schema_name, table |\n"
                "| **kafka** | Implemented | bootstrap_servers | topic, cluster |\n"
                "| **mysql** | Planned | host, port | database, schema_name, table |\n"
                "| **oracle** | Planned | host, port | database, schema_name, table |\n"
                "| **bigquery** | Planned | project_id | dataset, table |\n"
                "| **snowflake** | Planned | account_id | database, schema_name, table |\n\n"
                "## Aspect Emission\n\n"
                "A successful non-dry-run ingestion emits three aspects to DataHub per discovered dataset:\n"
                "- `StatusClass(removed=False)` — marks the entity as active\n"
                "- `DatasetPropertiesClass` — name, qualified name, description, custom properties\n"
                "- `SchemaMetadataClass` — field list with native-to-DataHub type mapping "
                "(e.g., PostgreSQL `integer` → DataHub `NUMBER`)\n\n"
                "**`dry_run` mode**: Extracts and validates source metadata without calling DataHub's "
                "REST Emitter — useful for verifying connection parameters and previewing schema."
            ),
            "externalDocs": {
                "description": "DataHub Dataset Entity — aspect catalog and REST endpoints",
                "url": "https://datahubproject.io/docs/generated/metamodel/entities/dataset",
            },
        },
        {
            "name": "common/validation",
            "description": (
                "Validation config CRUD, run, and result queries. Requires common auth.\n\n"
                "DataSpoke provides a convenience and customization layer on top of "
                "[DataHub's assertion framework](https://datahubproject.io/docs/managed-datahub/observe/assertions). "
                "Each validation config registers assertion rules per dataset; DataSpoke executes them and "
                "reports results back to DataHub as `assertionRunEvent` timeseries aspects.\n\n"
                "## Supported Rule Types\n\n"
                "| Type | Purpose | DataHub Assertion |\n"
                "|------|---------|-------------------|\n"
                "| **freshness** | Verify data was updated within a lookback window | "
                "[Freshness](https://datahubproject.io/docs/managed-datahub/observe/freshness-assertions) |\n"
                "| **volume** | Check row count stays within expected bounds | "
                "[Volume](https://datahubproject.io/docs/managed-datahub/observe/volume-assertions) |\n"
                "| **field** | Validate column-level metrics (null count, distinct count, min/max, etc.) | "
                "[Column](https://datahubproject.io/docs/managed-datahub/observe/column-assertions) |\n"
                "| **schema** | Ensure required fields exist with expected types | "
                "[Schema](https://datahubproject.io/docs/managed-datahub/observe/schema-assertions) |\n"
                "| **sql** | Run a custom SQL statement and assert on the scalar result | "
                "[Custom SQL](https://datahubproject.io/docs/managed-datahub/observe/custom-sql-assertions) |\n"
                "| **custom** | DataSpoke-original logic (e.g., `sql_timeseries` with ML-based anomaly detection) | N/A (DataSpoke extension) |\n\n"
                "All rule types support `partition` and `order` variables for targeting specific data partitions. "
                "The `custom` type with `subtype: sql_timeseries` enables trend tracking with configurable "
                "ML validation (model type, lookback window, target columns).\n\n"
                "Rule format follows the "
                "[DataHub Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec) "
                "with DataSpoke extensions (`rule_id`, `partition`, `order`, `ml_validation`)."
            ),
            "externalDocs": {
                "description": "DataHub Assertion Entity — assertionInfo and assertionRunEvent aspects",
                "url": "https://datahubproject.io/docs/generated/metamodel/entities/assertion",
            },
        },
        {
            "name": "common/gen",
            "description": "AI metadata generation config CRUD, generate, and apply. Requires common auth.",
        },
        {
            "name": "common/data",
            "description": "Dataset overview with embedded ingestion, validation, and generation sub-resources. Requires common auth.",
        },
        {
            "name": "common/ontology",
            "description": "Concept taxonomy CRUD and approval workflow. Requires common auth.",
        },
        {
            "name": "common/search",
            "description": "Vector-powered dataset search and reindex. Requires common auth.",
        },
        {
            "name": "dg/metric",
            "description": (
                "Governance metric definitions, measurement results, and scheduling. "
                "Requires DG auth (dg/admin groups).\n\n"
                "## Pure Aggregation Principle\n\n"
                "DataSpoke metrics implement the **observatory pattern**: a metric does not observe the "
                "data estate directly — it aggregates results that already exist in DataHub metadata or "
                "DataSpoke validation results. The metrics layer has no data source credentials, no SQL "
                "execution against production databases, and no network access to external systems beyond "
                "DataHub's API.\n\n"
                "## Data Governance Dimensions\n\n"
                "Built-in metric types are categorized by the data governance quality dimension they measure:\n\n"
                "| Governance Dimension | Metric Type | Data Source |\n"
                "|---------------------|-------------|-------------|\n"
                "| **Completeness** (metadata) | `poorly_documented` | "
                "DataHub `DatasetPropertiesClass.description` — counts datasets with description < 20 chars |\n"
                "| **Freshness** (timeliness) | `stale_datasets` | "
                "DataSpoke `validation_results` — counts datasets with no freshness rule or failing freshness validation |\n"
                "| *(extensible)* | Custom types | "
                "Any DataHub aspect or DataSpoke result table |\n\n"
                "New metric types are added by implementing a measurement function that reads from "
                "DataHub aspects or DataSpoke tables — never by adding direct source connections.\n\n"
                "## DataHub Relationship\n\n"
                "Metrics are **read-only consumers** of DataHub metadata. They read aspects "
                "(`DatasetPropertiesClass`, `OwnershipClass`, `globalTags`, `glossaryTerms`) via "
                "the DataHub SDK but never write aspects. Metric results are stored exclusively in "
                "DataSpoke's PostgreSQL `metric_results` table.\n\n"
                "## Measurement Query & Dataset Filter\n\n"
                "Each metric definition carries a `measurement_query` with a `type` field that selects "
                "the aggregation function. The query vocabulary is currently fixed (`poorly_documented`, "
                "`stale_datasets`); unsupported types return `422 UNSUPPORTED_METRIC_TYPE`.\n\n"
                "**`dataset_filter`**: Optional filter in `measurement_query` with `tags` (list of DataHub "
                "tag URNs) and `glossary_terms` (list of DataHub glossary term URNs). When specified, only "
                "datasets matching ANY of the listed tags or glossary terms are included. "
                "Filters are OR-ed across all dimensions."
            ),
        },
        {
            "name": "dg/overview",
            "description": "Data governance overview dashboard and lineage graph. Requires DG auth.",
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
    app.add_exception_handler(PreconditionError, _handle_precondition)  # type: ignore[arg-type]
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

    # ── Internal activity endpoints (called by Kestra, no auth) ────────────────
    app.include_router(internal_activities.router, include_in_schema=False)

    # ── Spoke/common routes ────────────────────────────────────────────────────
    app.include_router(common_ontology.router, prefix=SPOKE_COMMON)
    app.include_router(common_data.router, prefix=SPOKE_COMMON)
    app.include_router(common_data.ws_router, prefix=SPOKE_COMMON)
    app.include_router(common_ingestion.router, prefix=SPOKE_COMMON)
    app.include_router(common_validation.router, prefix=SPOKE_COMMON)
    app.include_router(common_gen.router, prefix=SPOKE_COMMON)
    app.include_router(common_search.router, prefix=SPOKE_COMMON)

    # ── Spoke/dg routes ────────────────────────────────────────────────────────
    app.include_router(dg_metrics.router, prefix=SPOKE_DG)
    app.include_router(dg_metrics.ws_router, prefix=SPOKE_DG)
    app.include_router(dg_overview.router, prefix=SPOKE_DG)

    # ── Hub pass-through routes ────────────────────────────────────────────────
    app.include_router(hub_router.router, prefix=API_PREFIX)

    return app


app = create_app()
