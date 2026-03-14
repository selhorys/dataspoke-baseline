# DataSpoke Backend

> This document specifies the backend service layer — feature services, shared
> libraries, Temporal workflows, and infrastructure integration patterns that sit
> behind the API layer.
> Data contracts (PostgreSQL schema, Qdrant collections) in
> [BACKEND_SCHEMA](BACKEND_SCHEMA.md).
>
> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Architecture context in [ARCHITECTURE](../ARCHITECTURE.md).
> API routes that delegate to these services in [API](API.md).
> DataHub SDK patterns in [DATAHUB_INTEGRATION](../DATAHUB_INTEGRATION.md).
> Testing conventions in [TESTING](../TESTING.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Source Layout](#source-layout)
3. [Layered Architecture](#layered-architecture)
4. [Shared Services (`src/shared/`)](#shared-services-srcshared)
5. [Feature Services (`src/backend/`)](#feature-services-srcbackend)
6. [Temporal Workflows (`src/workflows/`)](#temporal-workflows-srcworkflows)
7. [Kafka Consumers](#kafka-consumers)
8. [WebSocket Feed Mechanism](#websocket-feed-mechanism)
9. [Dependency Injection](#dependency-injection)
10. [Error Handling](#error-handling)
11. [Configuration](#configuration)

Data contracts (PostgreSQL schema, Qdrant collections) are specified in
[BACKEND_SCHEMA](BACKEND_SCHEMA.md).

---

## Overview

The backend layer is the computational core of DataSpoke. It contains all
business logic, infrastructure integrations, and orchestration workflows. The
API layer (FastAPI) delegates to backend services; backend services never import
from `src/api/`.

```
src/api/routers/          ← HTTP routing, Pydantic validation, auth
       │
       ▼  function calls
src/backend/              ← Feature service implementations
       │
       ├──► src/shared/   ← DataHub client, DB sessions, LLM client, Qdrant client
       │
       └──► src/workflows/  ← Temporal workflow/activity definitions
```

**Key rule**: Business logic lives in `src/backend/`, not in API route handlers.
Route handlers validate input, call a service function, and format the response.
This keeps services testable independently of HTTP concerns.

---

## Source Layout

```
src/
├── api/                  # FastAPI routers, schemas, middleware (spec: API.md)
├── backend/              # Feature service implementations
│   ├── __init__.py
│   ├── dataset/          # Dataset Resource (base /data/{urn} endpoints)
│   │   ├── __init__.py
│   │   └── service.py    # DatasetService — summary, attributes, cross-domain events
│   ├── ingestion/        # Deep Technical Spec Ingestion (UC1)
│   │   ├── __init__.py
│   │   ├── service.py    # IngestionService — config CRUD, run orchestration
│   │   └── extractors.py # Source-specific extractors (Confluence, GitHub, etc.)
│   ├── validation/       # Online Data Validator (UC2, UC3)
│   │   ├── __init__.py
│   │   ├── service.py    # ValidationService — config CRUD, run orchestration
│   │   ├── scoring.py    # Quality score computation
│   │   ├── anomaly.py    # Time-series anomaly detection (Prophet, Isolation Forest)
│   │   └── sla.py        # SLA checking, threshold learning, pre-breach alerting
│   ├── generation/       # Automated Doc Generation (UC4)
│   │   ├── __init__.py
│   │   ├── service.py    # GenerationService — config CRUD, generate, apply
│   │   └── analyzer.py   # Source code analysis, similar-table diffing
│   ├── search/           # NL Search + Text-to-SQL context (UC5, UC7)
│   │   ├── __init__.py
│   │   ├── service.py    # SearchService — query parsing, hybrid search
│   │   └── embedding.py  # Embedding generation and Qdrant sync
│   ├── ontology/         # Ontology/Taxonomy Builder (UC4, UC8)
│   │   ├── __init__.py
│   │   └── service.py    # OntologyService — concept CRUD, approve/reject
│   ├── metrics/          # Enterprise Metrics Dashboard (UC6)
│   │   ├── __init__.py
│   │   ├── service.py    # MetricsService — metric CRUD, measurement runs
│   │   └── aggregator.py # Health score aggregation, department mapping
│   └── overview/         # Multi-Perspective Data Overview (UC8)
│       ├── __init__.py
│       └── service.py    # OverviewService — graph layout, medallion coverage
├── shared/               # Cross-cutting shared libraries
│   ├── __init__.py
│   ├── config.py         # Resilience settings, constants
│   ├── exceptions.py     # DataSpokeError hierarchy (error codes for API mapping)
│   ├── datahub/          # DataHub client wrapper
│   │   ├── __init__.py
│   │   ├── client.py     # DataHubClient — read/write wrapper with retry
│   │   └── events.py     # Kafka consumer base, MCL deserialization
│   ├── db/               # PostgreSQL integration
│   │   ├── __init__.py
│   │   ├── session.py    # SQLAlchemy async session factory
│   │   └── models.py     # SQLAlchemy ORM models (all tables)
│   ├── vector/           # Qdrant integration
│   │   ├── __init__.py
│   │   └── client.py     # QdrantClient wrapper with collection management
│   ├── llm/              # LLM integration
│   │   ├── __init__.py
│   │   └── client.py     # LLM client abstraction (LangChain-based)
│   ├── cache/            # Redis integration
│   │   ├── __init__.py
│   │   └── client.py     # Redis client wrapper
│   ├── notifications/    # Notification engine
│   │   ├── __init__.py
│   │   └── service.py    # NotificationService — email, in-app alerts
│   └── models/           # Shared Pydantic domain models (not API schemas)
│       ├── __init__.py
│       ├── dataset.py    # DatasetSummary, DatasetAttributes
│       ├── quality.py    # QualityScore, QualityIssue, AnomalyResult
│       ├── ontology.py   # Concept, ConceptRelationship
│       └── events.py     # EventRecord (base for all event types)
└── workflows/            # Temporal workflow definitions
    ├── __init__.py
    ├── worker.py         # Temporal worker entry point
    ├── ingestion.py      # Ingestion workflow + activities
    ├── validation.py     # Validation workflow + activities
    ├── sla_monitor.py    # SLA monitoring scheduled workflow + activities (UC3)
    ├── generation.py     # Doc generation workflow + activities
    ├── embedding_sync.py # Embedding maintenance workflow + activities
    ├── metrics.py        # Metrics collection workflow + activities
    └── ontology.py       # Ontology rebuild workflow + activities
migrations/               # Alembic database migrations (repo root)
```

---

## Layered Architecture

### Request Flow

A typical API request flows through four layers:

```
1. Router         → Parse HTTP, validate input (Pydantic), enforce auth
2. Service        → Orchestrate business logic, call shared clients
3. Shared Client  → DataHub SDK, PostgreSQL, Qdrant, Redis, LLM
4. Infrastructure → External systems (DataHub GMS, PostgreSQL, etc.)
```

### Layer Rules

| Layer | May import from | Must not import from |
|-------|----------------|---------------------|
| `src/api/` | `src/backend/`, `src/shared/` | — |
| `src/backend/` | `src/shared/` | `src/api/` |
| `src/workflows/` | `src/backend/`, `src/shared/` | `src/api/` |
| `src/shared/` | — | `src/api/`, `src/backend/`, `src/workflows/` |

### Service Pattern

Every feature service follows the same structural pattern:

```python
# src/backend/<feature>/service.py

from src.shared.datahub.client import DataHubClient
from src.shared.db.session import get_session
from src.shared.cache.client import RedisClient

class FeatureService:
    """Stateless service — dependencies injected via constructor."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache

    async def get_config(self, dataset_urn: str) -> ConfigModel:
        ...

    async def run(self, dataset_urn: str, dry_run: bool = False) -> RunResult:
        ...
```

Services are **stateless** — all state lives in PostgreSQL, Redis, or DataHub.
This allows any API instance or Temporal worker to instantiate a service and
call its methods.

---

## Shared Services (`src/shared/`)

### DataHub Client Wrapper (`src/shared/datahub/client.py`)

Thin wrapper around `acryl-datahub` SDK providing connection management, retry
logic, and convenience methods. All DataHub interaction in the codebase flows
through this wrapper.

```python
class DataHubClient:
    """Unified DataHub read/write client with retry and circuit breaker."""

    def __init__(self, gms_url: str, token: str) -> None:
        self._graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=token))
        self._emitter = DatahubRestEmitter(gms_server=gms_url, token=token)

    # ── Read ──────────────────────────────────────────────────────────────
    async def get_aspect(self, urn: str, aspect_class: type[T]) -> T | None: ...
    async def get_timeseries(self, urn: str, aspect_class: type[T], limit: int = 30) -> list[T]: ...
    async def get_downstream_lineage(self, urn: str) -> list[str]: ...
    async def get_upstream_lineage(self, urn: str) -> list[str]: ...
    async def enumerate_datasets(self, platform: str | None = None) -> list[str]: ...

    # ── Write ─────────────────────────────────────────────────────────────
    async def emit_aspect(self, urn: str, aspect: Any) -> None: ...

    # ── Health ────────────────────────────────────────────────────────────
    async def check_connectivity(self) -> bool: ...
```

**Retry policy**: Exponential backoff, max 3 attempts, 500ms base delay.
**Circuit breaker**: Opens after 5 consecutive failures, resets after 60s probe.

These follow the conventions defined in
[DATAHUB_INTEGRATION §Error Handling](../DATAHUB_INTEGRATION.md#error-handling--resilience).

### PostgreSQL Session Factory (`src/shared/db/session.py`)

Uses SQLAlchemy 2.0 async with `asyncpg` driver.

```python
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

engine = create_async_engine(
    f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}",
    pool_size=10,
    max_overflow=5,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
```

### Qdrant Client (`src/shared/vector/client.py`)

Wraps the `qdrant-client` library for collection management, upsert, and search.

```python
class QdrantManager:
    """Manages Qdrant collections and provides typed search/upsert."""

    def __init__(self, host: str, port: int, api_key: str) -> None: ...

    async def ensure_collection(self, name: str, vector_size: int) -> None: ...
    async def upsert(self, collection: str, points: list[PointStruct]) -> None: ...
    async def search(self, collection: str, vector: list[float], limit: int = 20,
                     filters: dict | None = None) -> list[ScoredPoint]: ...
    async def delete(self, collection: str, ids: list[str]) -> None: ...
```

### LLM Client (`src/shared/llm/client.py`)

Provider-agnostic LLM client using LangChain. The `DATASPOKE_LLM_PROVIDER`,
`DATASPOKE_LLM_API_KEY`, and `DATASPOKE_LLM_MODEL` environment variables
determine the backend.

```python
class LLMClient:
    """Provider-agnostic LLM client."""

    def __init__(self, provider: str, api_key: str, model: str) -> None: ...

    async def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        """Single completion. Returns raw text."""
        ...

    async def complete_json(self, prompt: str, system: str = "",
                            schema: type[BaseModel] | None = None) -> dict:
        """Completion with JSON output. Optionally validate against a Pydantic schema."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Generate a vector embedding for the given text."""
        ...
```

### Redis Client (`src/shared/cache/client.py`)

Async Redis wrapper for caching, rate limiting, and pub/sub.

```python
class RedisClient:
    """Async Redis wrapper."""

    def __init__(self, host: str, port: int, password: str) -> None: ...

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int = 300) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def publish(self, channel: str, message: str) -> None: ...
    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        """Subscribe to a Redis pub/sub channel. Yields messages as they arrive."""
        ...
```

**Cache key conventions**:

| Pattern | TTL | Purpose |
|---------|-----|---------|
| `validation:{dataset_urn}:result` | 60s | Validation result cache for AI agent loops |
| `quality:{dataset_urn}:score` | 300s | Quality score cache |
| `search:{query_hash}` | 120s | Search result cache |
| `rate_limit:{user_id}` | 60s | Rate limiting counter |

### Notification Service (`src/shared/notifications/service.py`)

Sends outbound notifications to dataset owners and governance teams. Used by
the Metrics Service (UC6) for action items and SLA alerts, and by the Validation
Service (UC3) for pre-breach warnings.

```python
class NotificationService:
    """Sends notifications via configured channels."""

    async def send_email(self, to: list[str], subject: str, body_html: str) -> None:
        """Send an email notification. Uses SMTP or an external email API
        (configured via DATASPOKE_SMTP_* or DATASPOKE_EMAIL_PROVIDER env vars)."""
        ...

    async def send_action_items(self, owner_email: str, items: list[ActionItem]) -> None:
        """Send a formatted email with prioritized action items, estimated fix
        time, and projected score impact. Used by MetricsService after each
        measurement run detects new issues."""
        ...

    async def send_sla_alert(self, recipients: list[str], alert: SLAAlert) -> None:
        """Send a pre-breach SLA warning with root cause analysis, predicted
        breach time, and recommended actions. Used by SLAMonitorWorkflow."""
        ...

    async def send_alarm(self, recipients: list[str], metric_id: str,
                         value: float, threshold: float) -> None:
        """Send a metric threshold breach alarm. Used by MetricsCollectionWorkflow."""
        ...
```

**ActionItem model** (used in `send_action_items`):

```python
class ActionItem(BaseModel):
    dataset_urn: str
    issue_type: str               # "missing_owner", "no_description", "stale", etc.
    priority: str                 # "critical", "high", "medium"
    description: str              # Human-readable action description
    estimated_fix_minutes: int    # Estimated time to resolve
    projected_score_impact: float # Points gained if resolved
    due_date: datetime | None     # Suggested due date
```

**Configuration** (environment variables):

| Variable | Purpose | Default |
|----------|---------|---------|
| `DATASPOKE_SMTP_HOST` | SMTP server host | `""` (disabled) |
| `DATASPOKE_SMTP_PORT` | SMTP server port | `587` |
| `DATASPOKE_SMTP_USER` | SMTP username | `""` |
| `DATASPOKE_SMTP_PASSWORD` | SMTP password | `""` |
| `DATASPOKE_NOTIFICATION_FROM` | Sender email address | `dataspoke@example.com` |
| `DATASPOKE_NOTIFICATION_ENABLED` | Master toggle for all notifications | `false` |

When `DATASPOKE_NOTIFICATION_ENABLED` is `false`, notification calls are no-ops
(logged but not sent). This is the default for development.

### Shared Domain Models (`src/shared/models/`)

Pydantic models shared across backend services. These are **not** API
request/response schemas (those live in `src/api/schemas/`); they are internal
domain objects passed between services.

```python
# src/shared/models/quality.py
class QualityScore(BaseModel):
    dataset_urn: str
    overall_score: float          # 0–100
    dimensions: dict[str, float]  # e.g. {"completeness": 85, "freshness": 70, ...}
    computed_at: datetime

class QualityIssue(BaseModel):
    issue_type: str               # "freshness", "completeness", "schema_drift", etc.
    severity: str                 # "critical", "warning", "info"
    detail: str
    field_path: str | None = None

class AnomalyResult(BaseModel):
    metric_name: str
    is_anomaly: bool
    expected_value: float
    actual_value: float
    confidence: float
    detected_at: datetime
```

---

## Feature Services (`src/backend/`)

### Dataset Service (`src/backend/dataset/`)

**Covers**: Base dataset resource endpoints (`GET /data/{urn}`, `GET /data/{urn}/attr`,
`GET /data/{urn}/event`)

**Responsibilities**:
- Read dataset summary and attributes from DataHub (pass-through with formatting)
- Aggregate cross-domain event history from the unified `events` table

This is a thin read-through service. It does not own any PostgreSQL configuration
tables — it reads from DataHub for dataset identity/attributes and from the
shared `events` table for cross-domain event history.

**Service interface**:

```python
class DatasetService:
    async def get_summary(self, dataset_urn: str) -> DatasetSummary:
        """Read dataset identity from DataHub: name, platform, owner, tags.
        Raises EntityNotFoundError(DATASET_NOT_FOUND) if URN does not exist."""
        ...

    async def get_attributes(self, dataset_urn: str) -> DatasetAttributes:
        """Read dataset attributes from DataHub: schema summary, ownership,
        tags, description, quality score (from cache or computed on demand)."""
        ...

    async def get_events(self, dataset_urn: str, offset: int, limit: int,
                         from_dt: datetime | None = None,
                         to_dt: datetime | None = None) -> tuple[list[EventRecord], int]:
        """Query unified events table for all event types (ingestion, validation,
        generation) related to this dataset."""
        ...
```

**DataHub aspects read**:

| Method | Aspects | Purpose |
|--------|---------|---------|
| `get_summary` | `datasetProperties` | Name, description, custom properties |
| `get_summary` | `ownership` | Dataset owner(s) |
| `get_summary` | `globalTags` | Classification tags |
| `get_attributes` | `schemaMetadata` | Column count, field list summary |
| `get_attributes` | `datasetProperties`, `ownership`, `globalTags` | Same as summary + schema |
| `get_attributes` | Quality score (from Redis cache) | Latest cached quality score |

### Ingestion Service (`src/backend/ingestion/`)

**Covers**: UC1 (Deep Technical Spec Ingestion)

**Responsibilities**:
- CRUD for ingestion configurations (PostgreSQL: `ingestion_configs`)
- Trigger ingestion runs via Temporal workflow
- Source-specific extractors (Confluence, GitHub, Excel, SQL logs)
- Field mapping and enrichment using LLM

**Service interface**:

```python
class IngestionService:
    async def get_config(self, dataset_urn: str) -> IngestionConfig | None: ...
    async def upsert_config(self, dataset_urn: str, config: IngestionConfigInput) -> IngestionConfig: ...
    async def patch_config(self, dataset_urn: str, patch: dict) -> IngestionConfig: ...
    async def delete_config(self, dataset_urn: str) -> None: ...
    async def list_configs(self, offset: int, limit: int, filters: dict) -> tuple[list[IngestionConfig], int]: ...
    async def run(self, dataset_urn: str, config_id: str | None, dry_run: bool) -> IngestionRun: ...
    async def get_events(self, dataset_urn: str, offset: int, limit: int) -> tuple[list[EventRecord], int]: ...
```

**Run pipeline** (executed as Temporal workflow):

1. Load config from PostgreSQL
2. Invoke extractor(s) for configured sources
3. Transform extracted data → DataHub aspects
4. LLM-enrich descriptions and tags (if `deep_spec_enabled`)
5. Validate transformed aspects (schema checks, deduplication)
6. Write aspects to DataHub via `DataHubClient.emit_aspect()` (skip if `dry_run`)
7. Record run event (success/failure) in PostgreSQL

**Extractors** (`src/backend/ingestion/extractors.py`):

| Extractor | Input Source | Output |
|-----------|-------------|--------|
| `ConfluenceExtractor` | Confluence API | Business descriptions, data dictionary |
| `GitHubExtractor` | GitHub API (repos, PRs) | Code references, README context |
| `ExcelExtractor` | Uploaded Excel/CSV | Column mapping, business glossary |
| `SqlLogExtractor` | SQL query logs / PL/SQL | Lineage edges, transformation logic |

Each extractor implements:

```python
class BaseExtractor(ABC):
    @abstractmethod
    async def extract(self, source_config: dict) -> list[ExtractedMetadata]: ...
```

### Validation Service (`src/backend/validation/`)

**Covers**: UC2 (Online Data Validator), UC3 (Predictive SLA)

**Responsibilities**:
- CRUD for validation configurations (PostgreSQL: `validation_configs`)
- Quality score computation from DataHub aspects
- Time-series anomaly detection (Prophet, Isolation Forest)
- Upstream lineage traversal for root cause analysis
- Downstream impact analysis (identify affected consumers/dashboards)
- SLA prediction, threshold learning, and pre-breach alerting
- Qdrant similarity search for alternative dataset recommendations

**Service interface**:

```python
class ValidationService:
    async def get_config(self, dataset_urn: str) -> ValidationConfig | None: ...
    async def upsert_config(self, dataset_urn: str, config: ValidationConfigInput) -> ValidationConfig: ...
    async def patch_config(self, dataset_urn: str, patch: dict) -> ValidationConfig: ...
    async def delete_config(self, dataset_urn: str) -> None: ...
    async def list_configs(self, offset: int, limit: int, filters: dict) -> tuple[list[ValidationConfig], int]: ...
    async def get_results(self, dataset_urn: str, from_dt: datetime | None, to_dt: datetime | None,
                          offset: int, limit: int) -> tuple[list[ValidationResult], int]: ...
    async def run(self, dataset_urn: str, config_id: str | None, dry_run: bool) -> ValidationRun: ...
    async def get_events(self, dataset_urn: str, offset: int, limit: int) -> tuple[list[EventRecord], int]: ...
```

**Quality Score Computation** (`src/backend/validation/scoring.py`):

Aggregates multiple DataHub aspects into a 0–100 composite score per dataset.

| Dimension | Weight | Source Aspect | Scoring Logic |
|-----------|--------|---------------|---------------|
| Completeness | 25% | `datasetProperties`, `schemaMetadata` | % of fields with non-empty descriptions |
| Freshness | 25% | `operation` (timeseries) | Days since last successful operation vs SLA |
| Schema stability | 15% | `schemaMetadata` (history) | Schema change frequency over 30 days |
| Data quality | 20% | `datasetProfile` (timeseries) | Null ratio, row count trend stability |
| Ownership & tags | 15% | `ownership`, `globalTags` | Has owner + min 1 classifying tag |

```python
async def compute_quality_score(datahub: DataHubClient, dataset_urn: str) -> QualityScore:
    """Compute composite quality score from DataHub aspects."""
    ...
```

The quality score engine is used by three consumers:
- Validation service (per-entity health assessment)
- Metrics service (department-level aggregation)
- Overview service (graph node coloring)

**Anomaly Detection** (`src/backend/validation/anomaly.py`):

Operates on timeseries data from `datasetProfile` and `operation` aspects.

| Method | Use Case | Library |
|--------|----------|---------|
| Prophet | Seasonal pattern detection (row counts, query volumes) | `prophet` |
| Isolation Forest | Multivariate outlier detection (null ratios, column distributions) | `scikit-learn` |

```python
async def detect_anomalies(
    profiles: list[DatasetProfileClass],
    method: str = "prophet",  # "prophet" | "isolation_forest"
) -> list[AnomalyResult]:
    ...
```

**Validation Run Pipeline** (Temporal workflow):

1. Fetch entity context (DataHub aspects + cached quality score)
2. Compute quality score (calls `compute_quality_score`)
3. Run anomaly detection on timeseries profiles
4. Traverse upstream lineage for root-cause analysis (if issues found)
5. Traverse downstream lineage to assess impact (affected consumers/dashboards)
6. Search Qdrant for similar healthy datasets as alternatives
7. Assemble validation result with issues, recommendations, alternatives
8. Publish progress to Redis pub/sub channel (`ws:validation:{dataset_urn}`)
9. Cache result in Redis (60s TTL for AI agent loops)
10. Persist result in PostgreSQL (`validation_results`)
11. Record event (success/failure) in PostgreSQL

**Predictive SLA Monitoring** (`src/backend/validation/sla.py`):

UC3 extends the validation service with scheduled monitoring and predictive
alerting. Datasets with `sla_target` configured in their `validation_configs`
are monitored by the `SLAMonitorWorkflow`.

```python
async def check_sla(
    datahub: DataHubClient,
    dataset_urn: str,
    sla_target: SLATarget,
    history: list[DatasetProfileClass],
) -> SLACheckResult:
    """Check current dataset state against SLA targets.

    1. Compare current row count / freshness against SLA thresholds
    2. Detect deviation from learned baseline (day-of-week, hour-of-day)
    3. Predict time-to-breach using Prophet trend extrapolation
    4. If pre-breach: traverse upstream lineage for root cause identification
    5. If pre-breach: traverse downstream lineage for impact assessment
    """
    ...

async def learn_thresholds(
    history: list[DatasetProfileClass],
    lookback_days: int = 28,
) -> LearnedBaseline:
    """Learn day-of-week and hour-of-day baselines from historical profiles.

    Uses 4-week rolling window. Adjusts thresholds per day-of-week to reduce
    false positives (e.g., Monday morning batch delays are normal).
    Returns per-day expected values with ±σ bands.
    """
    ...
```

**SLA Target** schema (stored in `validation_configs.sla_target` JSONB):

```python
class SLATarget(BaseModel):
    freshness_hours: int          # Max hours since last operation
    min_quality_score: float      # Minimum acceptable quality score
    deadline_utc: str | None      # Daily deadline (e.g. "09:00") — pre-breach alerting
    alert_before_minutes: int = 120  # Alert N minutes before predicted breach
    auto_adjust_thresholds: bool = True  # Enable day-of-week threshold learning
```

### Generation Service (`src/backend/generation/`)

**Covers**: UC4 (Automated Doc Generation)

**Responsibilities**:
- CRUD for generation configurations (PostgreSQL: `generation_configs`)
- LLM-powered metadata generation (descriptions, tags, deprecation notes)
- Source code analysis for code-referenced datasets
- Similar-table diffing (Qdrant similarity + LLM comparison)
- Apply generated results to DataHub (with approval gate)

**Service interface**:

```python
class GenerationService:
    async def get_config(self, dataset_urn: str) -> GenerationConfig | None: ...
    async def upsert_config(self, dataset_urn: str, config: GenerationConfigInput) -> GenerationConfig: ...
    async def patch_config(self, dataset_urn: str, patch: dict) -> GenerationConfig: ...
    async def delete_config(self, dataset_urn: str) -> None: ...
    async def list_configs(self, offset: int, limit: int, filters: dict) -> tuple[list[GenerationConfig], int]: ...
    async def get_results(self, dataset_urn: str, from_dt: datetime | None, to_dt: datetime | None,
                          offset: int, limit: int) -> tuple[list[GenerationResult], int]: ...
    async def generate(self, dataset_urn: str) -> GenerationResult: ...
    async def apply(self, dataset_urn: str, result_id: str) -> None: ...
    async def get_events(self, dataset_urn: str, offset: int, limit: int) -> tuple[list[EventRecord], int]: ...
```

**Generation Pipeline** (Temporal workflow):

1. Read current DataHub aspects (schema, properties, lineage, tags)
2. Find similar datasets via Qdrant embedding search
3. LLM analysis: generate field descriptions, table summary, suggested tags
4. If code references configured, analyze source code (GitHub extractor)
5. Diff against similar tables — highlight semantic overlaps and divergences
6. Produce `GenerationResult` with proposed changes (stored in PostgreSQL)
7. On `apply` — write approved proposals to DataHub via `DataHubClient`

### Search Service (`src/backend/search/`)

**Covers**: UC5 (Natural Language Search), UC7 (Text-to-SQL Metadata)

**Responsibilities**:
- Natural language query parsing (intent extraction, entity type detection)
- Embedding generation and Qdrant vector search
- Hybrid search combining Qdrant vectors + DataHub GraphQL filters
- SQL context enrichment (column profiles, join paths, sample queries)
- Reindex trigger for individual datasets

**Service interface**:

```python
class SearchService:
    async def search(self, query: str, sql_context: bool = False,
                     offset: int = 0, limit: int = 20) -> SearchResults: ...
    async def reindex(self, dataset_urn: str) -> None: ...
```

**Search pipeline**:

1. Parse NL query → intent, entity type hints, compliance context
2. Generate query embedding via LLM
3. Vector search against Qdrant `dataset_embeddings` collection
4. Parallel: DataHub GraphQL search for structured filters (tags, platform)
5. Merge and re-rank results (vector score + metadata relevance)
6. Enrich each result with metadata (tags, lineage, usage stats)
7. If `sql_context=true`: add column profiles, join paths, sample queries

**Embedding Sync** (`src/backend/search/embedding.py`):

Generates embeddings for dataset metadata and maintains the Qdrant index.

```python
async def generate_embedding(datahub: DataHubClient, llm: LLMClient,
                              dataset_urn: str) -> list[float]:
    """Generate embedding from dataset properties, schema, tags, and lineage."""
    ...

async def sync_dataset(datahub: DataHubClient, llm: LLMClient,
                        qdrant: QdrantManager, dataset_urn: str) -> None:
    """Generate embedding and upsert into Qdrant."""
    ...
```

### Ontology Service (`src/backend/ontology/`)

**Covers**: UC4 (Doc Generation), UC8 (Multi-Perspective Overview)

**Responsibilities**:
- Concept category CRUD (PostgreSQL: `concept_categories`)
- Concept-to-dataset mapping (PostgreSQL: `dataset_concept_map`)
- Cross-concept relationship management (PostgreSQL: `concept_relationships`)
- LLM-powered taxonomy construction and drift detection
- Approve/reject workflow for pending proposals

**Service interface**:

```python
class OntologyService:
    async def list_concepts(self, offset: int, limit: int) -> tuple[list[Concept], int]: ...
    async def get_concept(self, concept_id: str) -> Concept | None: ...
    async def get_concept_attr(self, concept_id: str) -> ConceptAttributes: ...
    async def get_concept_events(self, concept_id: str, offset: int, limit: int) -> tuple[list[EventRecord], int]: ...
    async def approve(self, concept_id: str) -> Concept: ...
    async def reject(self, concept_id: str) -> Concept: ...
```

**Taxonomy Build Pipeline** (Temporal workflow, scheduled weekly):

1. Enumerate all datasets from DataHub
2. For each dataset: LLM classifies into business concept categories
3. Synthesize categories into a hierarchy (parent-child)
4. Infer cross-concept relationships (pairwise semantic analysis)
5. Score confidence per mapping
6. Low-confidence (< 0.7) classifications → queued for human review
7. Persist to PostgreSQL tables
8. Detect drift/violations against existing approved taxonomy

### Metrics Service (`src/backend/metrics/`)

**Covers**: UC6 (Enterprise Metrics Dashboard)

**Responsibilities**:
- Metric definition CRUD (PostgreSQL: `metric_definitions`)
- Metric measurement execution (scheduled or on-demand)
- Health score aggregation by department
- Alarm evaluation and notification (via `NotificationService`)
- Issue tracking lifecycle: auto-detect gaps → create issues → email owners →
  auto-resolve when fixed (PostgreSQL: `metric_issues`)
- Activate/deactivate metric scheduling

**Service interface**:

```python
class MetricsService:
    async def list_metrics(self, offset: int, limit: int, filters: dict) -> tuple[list[MetricDefinition], int]: ...
    async def get_metric(self, metric_id: str) -> MetricDefinition | None: ...
    async def get_metric_attr(self, metric_id: str) -> MetricAttributes:
        """Lightweight attributes overview: theme, period, active status, alarm enabled.
        Maps to GET /metric/{id}/attr (separate from the full config at /attr/conf)."""
        ...
    async def get_metric_config(self, metric_id: str) -> MetricConfig: ...
    async def upsert_metric_config(self, metric_id: str, config: MetricConfigInput) -> MetricConfig: ...
    async def patch_metric_config(self, metric_id: str, patch: dict) -> MetricConfig: ...
    async def delete_metric_config(self, metric_id: str) -> None: ...
    async def get_results(self, metric_id: str, from_dt: datetime | None, to_dt: datetime | None,
                          offset: int, limit: int) -> tuple[list[MetricResult], int]: ...
    async def run(self, metric_id: str) -> MetricResult: ...
    async def activate(self, metric_id: str) -> MetricDefinition: ...
    async def deactivate(self, metric_id: str) -> MetricDefinition: ...
    async def get_events(self, metric_id: str, offset: int, limit: int) -> tuple[list[EventRecord], int]: ...
    async def list_issues(self, metric_id: str | None, offset: int, limit: int,
                          filters: dict) -> tuple[list[MetricIssue], int]:
        """List auto-detected metadata issues. If metric_id is None, list across all metrics."""
        ...
    async def update_issue(self, issue_id: str, patch: dict) -> MetricIssue:
        """Update issue status (open → in_progress → resolved), assignee, or due date."""
        ...
```

**Health Score Aggregation** (`src/backend/metrics/aggregator.py`):

```python
async def aggregate_health_scores(
    datahub: DataHubClient,
    db: AsyncSession,
) -> dict[str, DepartmentHealth]:
    """Enumerate datasets, compute quality scores, aggregate by department.

    Department mapping: dataset ownership URN → department via HR API or
    static mapping table (PostgreSQL: department_mapping).
    """
    ...
```

**Built-in metric types**:

| Metric Type | Description | Computation |
|------------|-------------|-------------|
| `dataset_count` | Total datasets per platform | DataHub enumeration |
| `poorly_documented` | Datasets with description < 20 chars | DataHub properties scan |
| `stale_datasets` | Datasets not updated in > 7 days | DataHub operation timeseries |
| `low_quality` | Datasets with quality score < 50 | Quality score engine |
| `unowned_datasets` | Datasets with no ownership aspect | DataHub ownership scan |
| `tag_coverage` | % of datasets with at least 1 classifying tag | DataHub tags scan |

### Overview Service (`src/backend/overview/`)

**Covers**: UC8 (Multi-Perspective Data Overview)

**Responsibilities**:
- Assemble graph topology from ontology + lineage data
- Medallion layer classification and coverage map
- Graph layout computation for visualization
- Blind spot detection (datasets not covered by any concept)

**Service interface**:

```python
class OverviewService:
    async def get_overview(self) -> OverviewSnapshot: ...
    async def get_config(self) -> OverviewConfig: ...
    async def patch_config(self, patch: dict) -> OverviewConfig: ...
```

**Overview Snapshot** assembles:
- Ontology graph (concept nodes + relationship edges)
- Dataset nodes colored by quality score
- Lineage edges from DataHub
- Medallion layer classification (bronze/silver/gold based on lineage depth)
- Blind spot list (datasets without concept mappings)

---

## Temporal Workflows (`src/workflows/`)

### Worker Entry Point (`src/workflows/worker.py`)

```python
"""Temporal worker — registers all workflows and starts polling."""

async def main() -> None:
    client = await Client.connect(f"{TEMPORAL_HOST}:{TEMPORAL_PORT}",
                                   namespace=TEMPORAL_NAMESPACE)
    worker = Worker(
        client,
        task_queue="dataspoke-main",
        workflows=[
            IngestionWorkflow,
            ValidationWorkflow,
            SLAMonitorWorkflow,
            GenerationWorkflow,
            EmbeddingSyncWorkflow,
            MetricsCollectionWorkflow,
            OntologyRebuildWorkflow,
        ],
        activities=[
            # All activity functions registered here
        ],
    )
    await worker.run()
```

### Workflow Catalogue

| Workflow | Task Queue | Trigger | Schedule |
|----------|-----------|---------|----------|
| `IngestionWorkflow` | `dataspoke-main` | API `POST .../method/run` | On-demand |
| `ValidationWorkflow` | `dataspoke-main` | API `POST .../method/run`, Kafka event | On-demand + event-driven |
| `SLAMonitorWorkflow` | `dataspoke-main` | Schedule (per-dataset, from `validation_configs.schedule`) | Scheduled (e.g., every 30 min for datasets with SLA targets) |
| `GenerationWorkflow` | `dataspoke-main` | API `POST .../method/generate` | On-demand |
| `EmbeddingSyncWorkflow` | `dataspoke-main` | Kafka event, API `POST .../method/reindex` | Event-driven + on-demand |
| `MetricsCollectionWorkflow` | `dataspoke-main` | API `POST .../method/run`, schedule | On-demand + scheduled (configurable per metric) |
| `OntologyRebuildWorkflow` | `dataspoke-main` | Schedule | Weekly (configurable) |

### Workflow Design Conventions

1. **Workflows are pure orchestration** — no I/O in workflow code; all I/O in activities
2. **Activities are idempotent** — safe to retry on transient failures
3. **Timeouts**: Activity start-to-close timeout = 5 minutes (default); workflow execution timeout = 1 hour
4. **Retry policy**: Max 3 attempts, 10s initial interval, 2.0 backoff coefficient
5. **Heartbeating**: Long-running activities (bulk DataHub scans) heartbeat every 30s
6. **Workflow ID convention**: `{feature}-{dataset_urn_or_metric_id}-{uuid}` for deduplication

### Concurrency Guards

Some workflows must not run concurrently for the same entity:

| Workflow | Guard | Mechanism |
|----------|-------|-----------|
| `IngestionWorkflow` | One per dataset_urn | Temporal workflow ID dedup (`REJECT_DUPLICATE`) |
| `ValidationWorkflow` | One per dataset_urn | Temporal workflow ID dedup |
| `GenerationWorkflow` | One per dataset_urn | Temporal workflow ID dedup |
| `MetricsCollectionWorkflow` | One per metric_id | Temporal workflow ID dedup |

If a duplicate is rejected, the API returns `409 Conflict` with
`INGESTION_RUNNING` / `VALIDATION_RUNNING` / `GENERATION_RUNNING` error codes.

---

## Kafka Consumers

DataSpoke runs a single consumer group (`dataspoke-consumers`) that routes
events by aspect name. Consumer implementation lives in
`src/shared/datahub/events.py`; feature-specific handlers live in each service.

### Consumer Architecture

```python
# src/shared/datahub/events.py

class EventRouter:
    """Routes MCL events to registered handlers by aspect name."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}

    def register(self, aspect_name: str, handler: Callable) -> None: ...

    async def dispatch(self, event: MetadataChangeLog) -> None:
        for handler in self._handlers.get(event.aspect_name, []):
            await handler(event)
```

### Event Routing Table

| Kafka Topic | Aspect | Handler | Feature |
|-------------|--------|---------|---------|
| `MetadataChangeLog_Versioned_v1` | `datasetProperties` | `sync_vector_index` | Search (UC5) |
| `MetadataChangeLog_Versioned_v1` | `schemaMetadata` | `sync_vector_index`, `detect_new_clusters` | Search (UC5), Generation (UC4) |
| `MetadataChangeLog_Versioned_v1` | `ownership` | `update_health_score` | Metrics (UC6) |
| `MetadataChangeLog_Versioned_v1` | `globalTags` | `sync_vector_index`, `update_health_score` | Search (UC5), Metrics (UC6) |
| `MetadataChangeLog_Timeseries_v1` | `datasetProfile` | `trigger_quality_check` | Validation (UC2, UC3) |
| `MetadataChangeLog_Timeseries_v1` | `operation` | `check_freshness_sla` | Validation (UC3) |

### Consumer Process

The Kafka consumer runs as a separate process from the Temporal worker. By
default, both are co-located in the `dataspoke-workers` deployment. For
production workloads, the consumer can be deployed as an independent pod
(`dataspoke-event-consumer`) for independent scaling and fault isolation — Kafka
consumers scale by partition count while Temporal workers scale by workflow
throughput. See [HELM_CHART §Component Matrix](HELM_CHART.md#component-matrix)
for the `event-consumer.enabled` toggle.

Offsets are committed after successful processing (`enable.auto.commit=false`).

---

## WebSocket Feed Mechanism

The API layer exposes two WebSocket channels (see
[API §WebSocket Channels](API.md#websocket-channels)). The backend feeds data
to these channels via **Redis pub/sub**, which decouples the Temporal workflow
(producer) from the FastAPI WebSocket handler (consumer).

### Architecture

```
Temporal Worker                    Redis                    FastAPI API
(workflow activity)                                        (WS handler)
       │                             │                         │
       │── PUBLISH channel msg ────►│                         │
       │                             │── push to subscriber ──►│
       │                             │                         │── send to WS client
```

### Pub/Sub Channels

| Redis Channel | Producer | Consumer | API WS Endpoint |
|---------------|----------|----------|-----------------|
| `ws:validation:{dataset_urn}` | `ValidationWorkflow` activities | `stream_validation` WS handler | `/spoke/common/data/{dataset_urn}/stream/validation` |
| `ws:metric:updates` | `MetricsCollectionWorkflow` activities | `stream_metrics` WS handler | `/spoke/dg/metric/stream` |

### Producer Side (Temporal Activity)

Workflow activities publish progress and result messages as JSON to the
appropriate Redis channel:

```python
# Inside a validation workflow activity
async def run_validation_activity(dataset_urn: str, config_id: str) -> dict:
    cache = RedisClient(...)
    channel = f"ws:validation:{dataset_urn}"

    # Step progress
    await cache.publish(channel, json.dumps({
        "type": "progress", "step": "fetch_aspects", "pct": 20,
        "msg": "Fetching DataHub aspects",
    }))

    # ... do work ...

    # Final result
    await cache.publish(channel, json.dumps({
        "type": "result", "status": "completed",
        "quality_score": 78, "issues": [...],
    }))
```

### Consumer Side (FastAPI WS Handler)

The WebSocket route handler subscribes to the Redis channel and forwards
messages to the connected client:

```python
# src/api/routers/spoke/common/data.py
@router.websocket("/{dataset_urn}/stream/validation")
async def stream_validation(dataset_urn: str, websocket: WebSocket) -> None:
    await websocket.accept()
    # Auth handshake (validate JWT from first message)
    ...
    channel = f"ws:validation:{dataset_urn}"
    async for message in cache.subscribe(channel):
        await websocket.send_text(message)
        payload = json.loads(message)
        if payload.get("type") == "result":
            break  # Stream complete
    await websocket.close()
```

### Message Format

Messages follow the JSON schemas defined in
[API §WebSocket Channels](API.md#websocket-channels). The backend is
responsible for emitting messages that conform to those schemas.

---

## Dependency Injection

### FastAPI Dependencies

API route handlers receive backend services via FastAPI `Depends()`:

```python
# src/api/dependencies.py

async def get_datahub() -> DataHubClient:
    return DataHubClient(settings.datahub_gms_url, settings.datahub_token)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session

async def get_validation_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
) -> ValidationService:
    return ValidationService(datahub=datahub, db=db, cache=cache)
```

### Temporal Activities

Temporal activities create their own service instances since they run outside
the FastAPI request lifecycle:

```python
# src/workflows/validation.py

@activity.defn
async def run_validation_activity(dataset_urn: str, config_id: str) -> dict:
    datahub = DataHubClient(settings.datahub_gms_url, settings.datahub_token)
    async with SessionLocal() as db:
        cache = RedisClient(settings.redis_host, settings.redis_port, settings.redis_password)
        service = ValidationService(datahub=datahub, db=db, cache=cache)
        result = await service.run(dataset_urn, config_id, dry_run=False)
        return result.model_dump()
```

---

## Error Handling

### Service-Level Exceptions

Backend services raise domain-specific exceptions. The API layer catches them
and maps to HTTP responses.

```python
# src/shared/exceptions.py

class DataSpokeError(Exception):
    """Base exception for all DataSpoke backend errors."""
    error_code: str = "INTERNAL_ERROR"

class EntityNotFoundError(DataSpokeError):
    error_code: str  # Set by subclass: DATASET_NOT_FOUND, CONFIG_NOT_FOUND, etc.
    def __init__(self, entity_type: str, entity_id: str) -> None: ...

class ConflictError(DataSpokeError):
    error_code: str  # DUPLICATE_CONFIG, INGESTION_RUNNING, etc.

class DataHubUnavailableError(DataSpokeError):
    error_code: str = "DATAHUB_UNAVAILABLE"

class StorageUnavailableError(DataSpokeError):
    error_code: str = "STORAGE_UNAVAILABLE"
```

### Exception-to-HTTP Mapping

Registered as FastAPI exception handlers in `src/api/main.py`:

| Exception | HTTP Status | Error Code |
|-----------|-------------|------------|
| `EntityNotFoundError` | 404 | `DATASET_NOT_FOUND`, `CONFIG_NOT_FOUND`, `METRIC_NOT_FOUND`, `CONCEPT_NOT_FOUND` |
| `ConflictError` | 409 | `DUPLICATE_CONFIG`, `INGESTION_RUNNING`, `VALIDATION_RUNNING`, `GENERATION_RUNNING` |
| `DataHubUnavailableError` | 502 | `DATAHUB_UNAVAILABLE` |
| `StorageUnavailableError` | 503 | `STORAGE_UNAVAILABLE` |
| `ValidationError` (Pydantic) | 422 | `INVALID_PARAMETER` |

Error response format matches [API §Error Catalogue](API.md#error-catalogue).

---

## Configuration

All configuration is sourced from `src/api/config.py` (`Settings` class) which
reads environment variables with the `DATASPOKE_` prefix.

### Backend-Specific Settings

In addition to the settings already defined in the API config, the following
constants are defined in `src/shared/config.py` for backend resilience and
tuning:

| Setting | Default | Purpose |
|---------|---------|---------|
| `RETRY_MAX_ATTEMPTS` | 3 | DataHub SDK retry limit |
| `RETRY_BACKOFF_BASE_MS` | 500 | Exponential backoff base |
| `CIRCUIT_BREAKER_THRESHOLD` | 5 | Consecutive failures to open breaker |
| `CIRCUIT_BREAKER_RESET_MS` | 60000 | Time before probe attempt |
| `BULK_BATCH_SIZE` | 100 | DataHub bulk scan batch size |
| `BULK_BATCH_DELAY_MS` | 100 | Delay between bulk batches |
| `QUALITY_SCORE_CACHE_TTL` | 300 | Quality score Redis cache TTL (seconds) |
| `VALIDATION_RESULT_CACHE_TTL` | 60 | Validation result Redis cache TTL (seconds) |
| `SEARCH_RESULT_CACHE_TTL` | 120 | Search result Redis cache TTL (seconds) |
| `EMBEDDING_DIMENSION` | 1536 | Vector dimension (matches LLM model) |
| `ONTOLOGY_CONFIDENCE_THRESHOLD` | 0.7 | Below this → pending human review |
| `SLA_MONITOR_INTERVAL_MINUTES` | 30 | Default interval for SLA monitoring schedule |
| `SLA_ALERT_BEFORE_MINUTES` | 120 | Default pre-breach alert lead time |
