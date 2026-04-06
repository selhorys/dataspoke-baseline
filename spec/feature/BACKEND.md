# DataSpoke Backend

> This document specifies the backend service layer — feature services, shared
> libraries, Kestra workflow definitions, and infrastructure integration patterns
> that sit behind the API layer.
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
2. [Layered Architecture](#layered-architecture)
3. [Shared Services (`src/shared/`)](#shared-services-srcshared)
4. [Feature Services (`src/backend/`)](#feature-services-srcbackend)
5. [Event Emission](#event-emission)
6. [Kestra Workflows (`src/workflows/`)](#kestra-workflows-srcworkflows)
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
src/api/routers/          <- HTTP routing, Pydantic validation, auth
       |
       v  function calls
src/backend/              <- Feature service implementations
       |
       |---> src/shared/   <- DataHub client, DB sessions, LLM client, Qdrant client
       |
       +---> src/workflows/  <- Kestra flow YAML definitions + internal activity endpoints
```

**Key rule**: Business logic lives in `src/backend/`, not in API route handlers.
Route handlers validate input, call a service function, and format the response.
This keeps services testable independently of HTTP concerns.

Source layout is visible in the `src/` directory tree. The backend is organized
into feature modules under `src/backend/`, shared libraries under `src/shared/`,
and workflow definitions under `src/workflows/`. See the code itself for the
current file structure.

---

## Layered Architecture

### Request Flow

```
1. Router         -> Parse HTTP, validate input (Pydantic), enforce auth
2. Service        -> Orchestrate business logic, call shared clients
3. Shared Client  -> DataHub SDK, PostgreSQL, Qdrant, Redis, LLM
4. Infrastructure -> External systems (DataHub GMS, PostgreSQL, etc.)
```

### Layer Rules

| Layer | May import from | Must not import from |
|-------|----------------|---------------------|
| `src/api/` | `src/backend/`, `src/shared/` | -- |
| `src/backend/` | `src/shared/` | `src/api/` |
| `src/workflows/` | `src/backend/`, `src/shared/` | `src/api/` |
| `src/shared/` | -- | `src/api/`, `src/backend/`, `src/workflows/` |

### Route Handler Naming Convention

Route handler function names must mirror the REST path they serve.

**Rules:**

1. **CRUD verb prefix** -- use the HTTP method as the function prefix: `get_`, `post_`, `put_`, `patch_`, `delete_`. Never use domain verbs (`run_`, `activate_`, `apply_`, etc.) as prefix.
2. **Explicit entity names** -- include all entity names from the URL path so the function name is self-describing.
3. **Omit meta classifiers** -- path segments `attr` and `method` are structural classifiers; omit them from the function name.

**Examples:**

| Route | Function name |
|-------|---------------|
| `GET /metric/{id}/attr/conf` | `get_metric_conf` |
| `POST /metric/{id}/method/deactivate` | `post_metric_deactivate` |
| `GET /data/{urn}/attr/ingestion/conf` | `get_data_ingestion_conf` |
| `POST /data/{urn}/attr/gen/method/generate` | `post_data_gen_generate` |
| `POST /search/method/reindex` | `post_search_reindex` |

### Service Pattern

Every feature service is **stateless** -- dependencies are injected via constructor (`DataHubClient`, `AsyncSession`, `RedisClient`, etc.), and all persistent state lives in PostgreSQL, Redis, or DataHub. This allows any API instance or Kestra activity endpoint to instantiate a service and call its methods. See any `src/backend/<feature>/service.py` for the pattern.

---

## Shared Services (`src/shared/`)

Each shared service is a thin wrapper around an infrastructure client. See the source files for current method signatures.

| Service | Module | Role | Key design decisions |
|---------|--------|------|---------------------|
| DataHub Client | `datahub/client.py` | Unified read/write wrapper around `acryl-datahub` SDK | Exponential backoff (3 attempts, 500ms base). Circuit breaker (opens after 5 failures, 60s probe). See [DATAHUB_INTEGRATION](../DATAHUB_INTEGRATION.md). |
| PostgreSQL | `db/session.py`, `db/models.py` | SQLAlchemy 2.0 async with `asyncpg`. Session factory + ORM models. | Pool size 10, max overflow 5 |
| Qdrant | `vector/client.py` | Collection management, typed search/upsert | Wraps `qdrant-client` |
| LLM | `llm/client.py` | Provider-agnostic client (LangChain). Single completion, JSON completion, embedding. | Configured via `DATASPOKE_LLM_PROVIDER`, `DATASPOKE_LLM_MODEL` env vars |
| Redis | `cache/client.py` | Async wrapper for caching, rate limiting, pub/sub | -- |
| Notifications | `notifications/service.py` | Outbound notifications (email, in-app alerts). Used by Validation (UC2, UC3). | Master toggle `DATASPOKE_NOTIFICATION_ENABLED` (default `false` -- no-ops in dev) |
| Domain Models | `models/` | Shared Pydantic models (`QualityScore`, `EventRecord`, etc.) -- internal domain objects, not API schemas | API schemas live in `src/api/schemas/` |
| Exceptions | `exceptions.py` | `DataSpokeError` hierarchy with error codes for HTTP mapping | See [Error Handling](#error-handling) |
| Settings | `settings.py` | Pydantic `Settings` class reading `DATASPOKE_*` env vars | -- |

### Cache Key Conventions

| Pattern | TTL | Purpose |
|---------|-----|---------|
| `validation:{dataset_urn}:result` | 60s | Latest validation run result cache |
| `search:{query_hash}` | 120s | Search result cache |
| `rate_limit:{user_id}` | 60s | Rate limiting counter |

---

## Feature Services (`src/backend/`)

### Dataset Service (`src/backend/dataset/`)

**Covers**: Base dataset resource endpoints (`GET /data/{urn}`, `GET /data/{urn}/attr`, `GET /data/{urn}/event`)

Thin read-through service. Reads dataset identity/attributes from DataHub, aggregates cross-domain event history from the unified `events` table. Does not own any PostgreSQL configuration tables.

**DataHub aspects read**: `datasetProperties`, `ownership`, `globalTags`, `schemaMetadata`. Quality score from Redis cache.

### Ingestion Service (`src/backend/ingestion/`)

**Covers**: UC1 (Deep Technical Spec Ingestion)

#### Design Framework — DataHub Entity-Aspect Model & Source-Agnostic Extraction

DataSpoke ingestion implements a **source-agnostic metadata extraction** pattern built on [DataHub's entity-aspect model](https://datahubproject.io/docs/what/aspect). In DataHub, every dataset entity is described by composable _aspects_ — typed metadata facets such as `DatasetPropertiesClass` (human-readable name, description), `SchemaMetadataClass` (column-level schema), and `StatusClass` (entity lifecycle). DataSpoke's ingestion pipeline discovers schema metadata from heterogeneous data sources and expresses the results as standard DataHub aspects via the REST Emitter API.

This is architecturally similar to DataHub's own ingestion framework (which uses CLI-driven _recipes_ with pluggable _sources_), but DataSpoke provides a simplified, API-driven model optimized for on-demand and scheduled metadata refresh:

| Concern | DataHub Native Ingestion | DataSpoke Ingestion |
|---------|--------------------------|---------------------|
| Trigger | CLI batch (`datahub ingest`) | HTTP API + Kestra cron |
| Configuration | YAML recipes | JSONB config in PostgreSQL |
| Source plugins | 200+ community connectors | Focused extractors (extensible) |
| Output | Aspects + lineage + profiling | Core aspects (Status, Properties, Schema) |

**Source abstraction**: The `platform` / `locator` / `identifier` / `auth` model provides a uniform interface across data platforms. Each `platform` maps to a dedicated extractor that handles connection, schema discovery, and type mapping — mirroring DataHub's source plugin architecture but scoped to DataSpoke's metadata-first requirements rather than full profiling and lineage extraction.

| Platform | Status | Locator | Identifier |
|----------|--------|---------|------------|
| **postgres** | Implemented | host, port | database, schema_name, table |
| **kafka** | Implemented | bootstrap_servers | topic, cluster |
| **mysql** | Planned | host, port | database, schema_name, table |
| **oracle** | Planned | host, port | database, schema_name, table |
| **bigquery** | Planned | project_id | dataset, table |
| **snowflake** | Planned | account_id | database, schema_name, table |

**Aspect emission**: A successful non-dry-run ingestion emits three aspects to DataHub per discovered dataset:
- `StatusClass(removed=False)` — marks the entity as active
- `DatasetPropertiesClass` — name, qualified name, description, custom properties (source, database, schema)
- `SchemaMetadataClass` — field list with native-to-DataHub type mapping (e.g., PostgreSQL `integer` → DataHub `NUMBER`)

See [DATAHUB_INTEGRATION §Aspect Reference](../DATAHUB_INTEGRATION.md#aspect-reference) for the full aspect catalogue.

**`dry_run` semantics**: Extracts and validates source metadata without calling DataHub's REST Emitter — useful for verifying connection parameters and previewing schema before committing to DataHub.

#### Implementation

CRUD for ingestion configurations (PostgreSQL: `ingestion_configs`). Supports periodic (cron) and manual ingestion. Metadata ingestion via source-specific extractors, enrichment from external sources (TBD), custom extractors (TBD). Config upsert registers the dataset URN in `dataset_registry` (does not require the dataset to exist in DataHub yet).

Ingestion config model: see [`BACKEND_SCHEMA §ingestion_configs`](BACKEND_SCHEMA.md#ingestion_configs). Key fields: `dataset_urn` (unique per dataset), `platform` (`postgres`, `kafka` implemented; others TODO), `locator`/`identifier`/`auth` (JSONB connection details), `is_active`/`schedule_cron` (cron trigger), `status` (Kestra registration outcome).

**Run pipeline** (`IngestionService.run()`):

1. Load config from PostgreSQL
2. Connect to source using `locator`/`auth`
3. Discover schema metadata using `identifier`
4. Emit aspects to DataHub (`StatusClass`, `DatasetPropertiesClass`, `SchemaMetadataClass`; skip if `dry_run`). A non-dry-run that ingests zero entities is treated as failure.
5. Run enrichment sources, if configured (TBD)
6. Run custom extractors, if configured (TBD)
7. On success (non-dry-run): mark `dataset_registry.datahub_registered = true`
8. Record event (`INGESTION.COMPLETE` or `INGESTION.FAIL`; see [Event Catalogue](#event-catalogue))

### Validation Service (`src/backend/validation/`)

**Covers**: UC2 (Data Validation), UC3 (Predictive SLA via timeseries validation)

#### Design Framework — DataHub Assertion Framework & Open Assertions Spec

DataSpoke validation is a **convenience and customization layer** on top of DataHub's native [assertion framework](https://datahubproject.io/docs/managed-datahub/observe/assertions) — it does not implement its own quality scoring engine. DataHub models data quality through _assertions_: named checks that evaluate a specific quality dimension of a dataset and report pass/fail results. The [Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec) defines six assertion types that cover the primary data quality dimensions:

| DataHub Assertion Type | Quality Dimension | Example |
|----------------------|------------------|---------|
| [FRESHNESS](https://datahubproject.io/docs/managed-datahub/observe/freshness-assertions) | Timeliness | "Table updated within last 24 hours" |
| [VOLUME](https://datahubproject.io/docs/managed-datahub/observe/volume-assertions) | Completeness | "Row count between 1,000 and 100,000" |
| [FIELD](https://datahubproject.io/docs/managed-datahub/observe/column-assertions) | Accuracy / Validity | "Column `email` matches regex pattern" |
| [SCHEMA](https://datahubproject.io/docs/managed-datahub/observe/schema-assertions) | Conformance | "Required columns exist with expected types" |
| [SQL](https://datahubproject.io/docs/managed-datahub/observe/custom-sql-assertions) | Custom | "No orphaned foreign keys in `orders`" |
| CUSTOM | Any | DataSpoke-extended assertions (not in DataHub native) |

DataSpoke wraps all six types and adds a **DataSpoke-original extension**: `custom` type with `subtype: "sql_timeseries"`, which enables partition-aware SQL validation with optional ML-based anomaly detection. This is designed for SQL-runnable datasets (PostgreSQL, Trino, Snowflake) where traditional threshold-based assertions are insufficient — e.g., detecting whether today's row count deviates from the day-of-week historical pattern.

**How DataSpoke extends DataHub assertions**:

| Concern | DataHub Native Assertions | DataSpoke Validation |
|---------|--------------------------|---------------------|
| Configuration | Per-assertion definition | Per-dataset bundled config (multiple rules in one `validation_config`) |
| Partition targeting | Manual | Automatic via `partition`/`order` variables (SQL window-function semantics) |
| Result storage | `assertionRunEvent` timeseries aspect | DataHub aspect + PostgreSQL `validation_results` (for ML training data) |
| ML validation | Not supported | `ml_validation` extension (range model, day-of-week baseline) |

DataSpoke registers assertion definitions (`assertionInfo` aspect) and reports results (`assertionRunEvent` aspect) back to DataHub, making DataSpoke-managed validations visible in DataHub's native assertion UI. See [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects) for the assertion entity model.

#### Implementation

CRUD for validation configurations (PostgreSQL: `validation_configs`). Partition-aware rule execution, assertion registration in DataHub, and result reporting. Config upsert registers the dataset URN in `dataset_registry` (requires the dataset to already exist in DataHub).

**Supported rule types**: All 6 DataHub assertion types — freshness, volume, field, schema, SQL, custom. Each rule can specify partition and order variables (like SQL window functions) for determining the target partition.

**Configuration model**: Per-dataset config stored in `validation_configs` with:
- `schedule_cron` (TEXT): Cron expression for periodic execution (required when `is_active=true`).
- `rules` (JSONB): list of rule dicts compatible with DataHub's Open Assertions Spec, extended with `rule_id`, `partition`, `order`, and (for custom type) `ml_validation`.

**SQL-Based Timeseries Engine** (`timeseries.py`): The `custom` type with `subtype: "sql_timeseries"` enables DataSpoke-original validation for SQL-runnable datasets (PostgreSQL, Trino, Snowflake). Defines data manipulation SQL, partition/order/value variables, and optional ML-based validation settings (model type, lookback window, validation range).

**Validation Run Pipeline** (ad-hoc runs execute directly; periodic runs are orchestrated via `validation-periodic-*` flows):

1. Resolve target partition (manual request → specified partition; cron → latest partition via partition/order variables)
2. For each rule in the dataset's config, compute metrics for the target partition
3. For `custom/sql_timeseries` rules: execute SQL against source, extract partition values
4. For rules with `ml_validation`: validate selected values against historical records (range model, day-of-week baseline, etc.)
5. Register assertion definitions in DataHub (`assertionInfo` aspect) if not already present
6. Report each rule's result to DataHub (`assertionRunEvent` aspect: SUCCESS/FAILURE/ERROR)
7. Persist results in PostgreSQL (`validation_results`), publish progress to Redis pub/sub channel (`ws:validation:{dataset_urn}`)
8. Record event (`VALIDATION.COMPLETE`)

### Generation Service (`src/backend/generation/`)

**Covers**: UC4 (Automated Doc Generation)

CRUD for generation configurations (PostgreSQL: `generation_configs`). LLM-powered metadata generation (descriptions, tags, deprecation notes), source code analysis, similar-table diffing (Qdrant + LLM), apply generated results to DataHub with approval gate.

**Generation Pipeline** (Kestra flow):

1. Read current DataHub aspects (schema, properties, lineage, tags)
2. Find similar datasets via Qdrant embedding search
3. LLM analysis: generate field descriptions, table summary, suggested tags
4. If code references configured, analyze source code
5. Diff against similar tables
6. Produce `GenerationResult` (stored in PostgreSQL)
7. On `apply` -- write approved proposals to DataHub

### Search Service (`src/backend/search/`)

**Covers**: UC5 (Natural Language Search), UC7 (Text-to-SQL Metadata)

NL query parsing, embedding generation, hybrid search (Qdrant vectors + DataHub GraphQL filters), SQL context enrichment, reindex trigger.

**Search pipeline**: Parse NL query -> generate embedding -> vector search -> parallel DataHub GraphQL search -> merge and re-rank -> enrich with metadata -> add SQL context if requested.

**Embedding Sync** (`embedding.py`): Generates embeddings for dataset metadata and maintains the Qdrant index.

### Ontology Service (`src/backend/ontology/`)

**Covers**: UC4 (Doc Generation), UC8 (Multi-Perspective Overview)

Concept category CRUD, concept-to-dataset mapping, cross-concept relationship management (all PostgreSQL). LLM-powered taxonomy construction and drift detection. Approve/reject workflow for pending proposals.

**Taxonomy Build Pipeline** (Kestra flow, scheduled weekly):

1. Enumerate all datasets from DataHub
2. LLM classifies each dataset into business concept categories
3. Synthesize categories into hierarchy, infer cross-concept relationships
4. Score confidence per mapping; low-confidence (< 0.7) queued for human review
5. Persist to PostgreSQL, detect drift against existing approved taxonomy

### Metrics Service (`src/backend/metrics/`)

**Covers**: UC6 (Enterprise Metrics Dashboard)

#### Design Framework — Observatory Pattern & Data Governance Dimensions

> **Core principle**: Metrics are pure aggregation over pre-existing data. A metric does
> not observe the data estate directly — it aggregates results that already exist in
> DataHub metadata or DataSpoke validation results.

DataSpoke metrics implement what governance frameworks call the **observatory pattern**: metrics aggregate pre-existing metadata rather than directly probing source data. This architectural separation means the metrics layer has no data source credentials, no SQL execution against production databases, and no network access to external systems beyond DataHub's API. This makes metrics lightweight, fast, and free of credential management.

Built-in metric types are categorized by the data governance quality dimension they measure:

| Governance Dimension | Metric Type | Data Source |
|---------------------|-------------|-------------|
| **Completeness** (metadata) | `poorly_documented` | DataHub `DatasetPropertiesClass.description` — counts datasets where description < 20 chars |
| **Freshness** (timeliness) | `stale_datasets` | DataSpoke `validation_results` — counts datasets with no active freshness rule or failing freshness validation |
| *(extensible)* | Custom types | Any DataHub aspect or DataSpoke result table |

New metric types are added by implementing a measurement function that reads from DataHub aspects or DataSpoke tables — never by adding direct source connections. This constraint preserves the observatory pattern.

**DataHub relationship**: Metrics are **read-only consumers** of DataHub metadata. They read aspects (`DatasetPropertiesClass`, `OwnershipClass`, `globalTags`, `glossaryTerms`) via the DataHub SDK but never write aspects. Metric results are stored exclusively in DataSpoke's PostgreSQL `metric_results` table. See [DATAHUB_INTEGRATION §Aspect Usage by Feature](../DATAHUB_INTEGRATION.md#aspect-usage-by-feature) for the full read/write matrix.

**`measurement_query` model**: Each metric definition carries a `measurement_query` JSONB with a `type` field that selects the aggregation function. The query vocabulary is currently fixed (`poorly_documented`, `stale_datasets`); unsupported types return `422 UNSUPPORTED_METRIC_TYPE`. The vocabulary is extensible by adding new measurement functions to the metrics service without schema changes.

#### Implementation

Metric definition CRUD (PostgreSQL: `metric_definitions`). Scheduled or on-demand measurement execution. Activate/deactivate metric scheduling. No alarm evaluation, no issue tracking, no notification dispatch.

**`dataset_filter`**: Optional filter in `measurement_query` with `tags` (list of DataHub tag URNs) and `glossary_terms` (list of DataHub glossary term URNs). When specified, only datasets matching ANY of the listed tags or glossary terms are included in the measurement. Filters are OR-ed across all dimensions.

**Breakdown format**: Every measurement result includes a `breakdown` JSONB with a unified per-dataset entry shape:

```
{"dataset_count": <total scanned>, "datasets": [{"urn": "...", "category": "<classification>", "detail": {...}}]}
```

`category` is a machine-readable classification (e.g. `short_description`, `no_freshness_rule`, `freshness_failure`). `detail` is optional, type-specific metadata (e.g. `{"length": 5, "value": "short"}` for poorly_documented, `{"rule_id": "fresh-1"}` for stale_datasets).

**Health Score Aggregation** (`aggregator.py`): Enumerates datasets, computes quality scores, aggregates by department. Department mapping: dataset ownership URN -> department via HR API or static mapping table.

### Overview Service (`src/backend/overview/`)

**Covers**: UC8 (Multi-Perspective Data Overview)

Assembles graph topology from ontology + lineage data, medallion layer classification (bronze = 0 upstreams, silver = 1-2, gold = 3+, based on `upstreamLineage` aspect), graph layout computation, blind spot detection (datasets not covered by any concept).

---

## Event Emission

Every successful mutating API call records an event to the unified `events`
table (see [BACKEND_SCHEMA §events](BACKEND_SCHEMA.md#events)). GET requests
do not emit events. If a request is rejected before reaching the service layer
(e.g., 409 concurrency guard, 404 not found), no event is recorded.

### Naming Convention

Event type values are **uppercase**, dot-delimited: `{DOMAIN}.{ACTION}`.

- **Domain** identifies the feature: `INGESTION`, `VALIDATION`, `GENERATION`,
  `METRIC`, `CONCEPT`.
- **Action** describes what happened. Two categories:
  - *Config lifecycle*: `CONFIG_CREATE`, `CONFIG_UPDATE`, `CONFIG_DELETE` —
    emitted by PUT, PATCH, DELETE on a configuration resource.
  - *Action*: domain-specific operations beyond CRUD (pipeline runs, approvals,
    state transitions).

### Event Catalogue

#### Ingestion (`entity_type=dataset`)

| Event Type | Trigger |
|---|---|
| `INGESTION.CONFIG_CREATE` | PUT config (new) |
| `INGESTION.CONFIG_UPDATE` | PUT config (existing) or PATCH |
| `INGESTION.CONFIG_DELETE` | DELETE config |
| `INGESTION.COMPLETE` | POST run succeeds |
| `INGESTION.FAIL` | POST run encounters errors |

#### Validation (`entity_type=dataset`)

| Event Type | Trigger |
|---|---|
| `VALIDATION.CONFIG_CREATE` | PUT config (new) |
| `VALIDATION.CONFIG_UPDATE` | PUT config (existing) or PATCH |
| `VALIDATION.CONFIG_DELETE` | DELETE config |
| `VALIDATION.COMPLETE` | POST run succeeds |

#### Generation (`entity_type=dataset`)

| Event Type | Trigger |
|---|---|
| `GENERATION.CONFIG_CREATE` | PUT config (new) |
| `GENERATION.CONFIG_UPDATE` | PUT config (existing) or PATCH |
| `GENERATION.CONFIG_DELETE` | DELETE config |
| `GENERATION.COMPLETE` | POST generate succeeds |
| `GENERATION.APPLY` | POST apply succeeds |

#### Metrics (`entity_type=metric`)

| Event Type | Trigger |
|---|---|
| `METRIC.CONFIG_CREATE` | PUT definition (new) |
| `METRIC.CONFIG_UPDATE` | PUT definition (existing) or PATCH |
| `METRIC.CONFIG_DELETE` | DELETE definition |
| `METRIC.RUN_COMPLETE` | POST run measurement succeeds |
| `METRIC.ACTIVATE` | POST activate |
| `METRIC.DEACTIVATE` | POST deactivate |

#### Ontology (`entity_type=concept`)

| Event Type | Trigger |
|---|---|
| `CONCEPT.APPROVE` | POST approve |
| `CONCEPT.REJECT` | POST reject |

### Querying Events

- **Entity-level endpoint** (`GET .../data/{urn}/event`): returns all events
  for the entity regardless of domain — filters only by `entity_type` +
  `entity_id`.
- **Domain-level endpoint** (`GET .../attr/ingestion/event`): additionally
  filters by `event_type` prefix (e.g., `INGESTION.%`) to return only
  domain-specific events.

See [BACKEND_SCHEMA §events](BACKEND_SCHEMA.md#events) for the filtering
convention and [API §Meta-Classifier Conventions](API.md#meta-classifier-conventions)
for the response contract.

---

## Kestra Workflows (`src/workflows/`)

### Architecture

Kestra v1.3.3 serves as the workflow orchestration engine. Workflows are defined as YAML flow definitions in `src/workflows/flows/`. Each flow uses Kestra's `io.kestra.plugin.core.http.Request` tasks to call internal activity endpoints on the DataSpoke API at `/internal/activities/{domain}/*`. Kestra handles scheduling, retry, and execution.

### Kestra Client Subpackage (`src/workflows/kestra/`)

Wraps Kestra's REST API via `httpx`: flow CRUD, execution lifecycle (trigger, poll, wait), label-based dedup, and cleanup. See the source files for current API.

### Flow Catalogue

| Flow | File | Trigger | Schedule |
|------|------|---------|----------|
| `ingestion-config-sync` | `ingestion_config_sync.yaml` | Kestra cron | `*/10 * * * *` (default) |
| `ingestion-periodic-*` | dynamically generated | Kestra cron (grouped by schedule) + manual | Per-config |
| `validation-config-sync` | `validation_config_sync.yaml` | Kestra cron | `*/10 * * * *` (default) |
| `validation-periodic-*` | dynamically generated | Kestra cron (grouped by schedule) + manual | Per-config schedule |
| `generation` | `generation.yaml` | API | On-demand |
| `embedding-sync` | `embedding_sync.yaml` | Kafka event + API | Event-driven + on-demand |
| `metrics` | `metrics.yaml` | API + Kestra schedule | On-demand + periodic (per-config) |
| `metrics-config-sync` | `metrics_config_sync.yaml` | Kestra cron | `*/10 * * * *` (default) |
| `metrics-periodic-*` | dynamically generated | Kestra cron (grouped by schedule) | Per-config schedule |
| `ontology-rebuild` | `ontology_rebuild.yaml` | Kestra schedule | Weekly (configurable) |

### Workflow Design Conventions

1. **Flows are YAML-defined orchestration** -- each task is an HTTP Request to an internal activity endpoint
2. **Activity endpoints are idempotent** -- safe to retry on transient failures
3. **Timeouts**: Per-task = 5 minutes (default); flow-level = 1 hour
4. **Retry policy**: Max 3 attempts, 10s initial interval
5. **Concurrency**: Per-entity guards prevent duplicate runs

### Concurrency Guards

**Redis SET NX** (for direct-execution flows):

| Flow | Redis Key | TTL |
|------|-----------|-----|
| `ingestion` | `ingestion:running:{dataset_urn}` | 1 hour |
| `validation` | `validation:running:{dataset_urn}` | 1 hour |

**Kestra label-based dedup** (for Kestra-orchestrated flows):

| Flow | Label Value |
|------|-------------|
| `generation` | `generation-{md5(urn)[:12]}` |
| `metrics` | `metrics-{metric_id}` |

If a duplicate is detected, the API returns `409 Conflict` with the appropriate `*_RUNNING` error code.

### Ingestion Workflow

Ingestion supports two trigger modes per dataset:

| Mode | Trigger | How |
|------|---------|-----|
| **Periodic** | Kestra cron schedule | Datasets with the same `schedule` value are grouped into a single Kestra flow |
| **Manual** | User HTTP request | `POST .../attr/ingestion/method/run` calls `IngestionService.run()` directly |

**Periodic flow generation**: DataSpoke dynamically generates one Kestra flow per unique cron schedule. Sync runs on backend startup and via the `ingestion-config-sync` workflow (cron `*/10 * * * *`). Sync logic: query distinct schedules -> generate/update flows -> delete orphaned flows.

Each generated flow fetches the dataset list dynamically at execution time (`POST /internal/activities/ingestion/list-periodic`), then runs ingestion for each dataset in parallel (concurrency limit: 5).

---

## Kafka Consumers

DataSpoke runs a single consumer group (`dataspoke-consumers`) that routes events by aspect name. Consumer implementation: `src/shared/datahub/events.py` (EventRouter) and `src/shared/datahub/consumer.py`.

### Event Routing Table

| Kafka Topic | Aspect | Handler | Feature |
|-------------|--------|---------|---------|
| `MetadataChangeLog_Versioned_v1` | `datasetProperties` | `sync_vector_index` | Search (UC5) |
| `MetadataChangeLog_Versioned_v1` | `schemaMetadata` | `sync_vector_index`, `detect_new_clusters` | Search (UC5), Generation (UC4) |
| `MetadataChangeLog_Versioned_v1` | `ownership` | `update_health_score` | Metrics (UC6) |
| `MetadataChangeLog_Versioned_v1` | `globalTags` | `sync_vector_index`, `update_health_score` | Search (UC5), Metrics (UC6) |
| `MetadataChangeLog_Timeseries_v1` | `datasetProfile` | `update_health_score` | Metrics (UC6) |

### Consumer Process

Runs as `python -m src.shared.datahub.consumer`, separate from the API server. By default co-located in `dataspoke-api` deployment; can be deployed independently as `dataspoke-event-consumer` for partition-based scaling. See [HELM_CHART](HELM_CHART.md#component-matrix) for the `event-consumer.enabled` toggle.

Uses `confluent-kafka` with manual offset commit (commit only after successful handler dispatch). Deserialization failures are logged and skipped; handler failures leave offset uncommitted for redelivery.

---

## WebSocket Feed Mechanism

The API exposes WebSocket channels fed via **Redis pub/sub**, decoupling activity endpoints (producers) from FastAPI WebSocket handlers (consumers).

### Pub/Sub Channels

| Redis Channel | Producer | API WS Endpoint |
|---------------|----------|-----------------|
| `ws:validation:{dataset_urn}` | Validation activities | `/spoke/common/data/{dataset_urn}/stream/validation` |
| `ws:metric:updates` | Metrics activities | `/spoke/dg/metric/stream` |

Activity endpoints publish JSON progress/result messages to the appropriate Redis channel. The WebSocket handler subscribes and forwards messages to clients. Message schemas are defined in [API](API.md#websocket-channels).

---

## Dependency Injection

**API route handlers** receive backend services via FastAPI `Depends()` (see `src/api/dependencies.py`).

**Internal activity endpoints** use factory functions from `src/workflows/_common.py` (`make_datahub`, `make_cache`, `make_db_session`, `make_llm`, `make_qdrant`) instead of FastAPI `Depends()`. This decouples them from the FastAPI DI graph -- the same factories work in any context (tests, CLI).

Activity endpoints map `DataSpokeError` to `400` (non-retryable) or `500` (retryable) JSON responses, letting Kestra distinguish between errors worth retrying and permanent failures.

---

## Error Handling

### Exception-to-HTTP Mapping

| Exception | HTTP Status | Error Code |
|-----------|-------------|------------|
| `EntityNotFoundError` | 404 | `DATASET_NOT_FOUND`, `CONFIG_NOT_FOUND`, `METRIC_NOT_FOUND`, `CONCEPT_NOT_FOUND` |
| `ConflictError` | 409 | `DUPLICATE_CONFIG`, `INGESTION_RUNNING`, `VALIDATION_RUNNING`, `GENERATION_RUNNING`, `METRIC_RUNNING` |
| `DataHubUnavailableError` | 502 | `DATAHUB_UNAVAILABLE` |
| `StorageUnavailableError` | 503 | `STORAGE_UNAVAILABLE` |
| `ValidationError` (Pydantic) | 422 | `INVALID_PARAMETER` |

Error response format matches [API](API.md#error-catalogue). Exception hierarchy is defined in `src/shared/exceptions.py`.

### Best-Effort Operations

Non-critical operations execute best-effort -- if they fail, the primary operation completes with reduced enrichment. All failures are logged at WARNING with `exc_info=True`.

| Operation | Service | Fallback |
|-----------|---------|----------|
| LLM description enrichment | IngestionService | Ingested without enriched description |
| Source SQL execution | ValidationService | Rule skipped, marked as ERROR in `assertionRunEvent` |
| ML validation model fit | ValidationService | Value recorded without validation verdict |
| Redis pub/sub + cache write | ValidationService | WebSocket unnotified; next read hits DB |
| Qdrant similarity search | GenerationService | No alternative suggestions |
| LLM sample query generation | SearchService | SQL context without example query |
| LLM dataset classification | OntologyRebuild | Dataset excluded from classification |

---

## Configuration

All configuration is sourced from `src/shared/settings.py` (`Settings` class)
which reads environment variables with the `DATASPOKE_` prefix.

### Backend-Specific Settings

Resilience and tuning constants defined in `src/shared/config.py`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `RETRY_MAX_ATTEMPTS` | 3 | DataHub SDK retry limit |
| `RETRY_BACKOFF_BASE_MS` | 500 | Exponential backoff base |
| `CIRCUIT_BREAKER_THRESHOLD` | 5 | Consecutive failures to open breaker |
| `CIRCUIT_BREAKER_RESET_MS` | 60000 | Time before probe attempt |
| `BULK_BATCH_SIZE` | 100 | DataHub bulk scan batch size |
| `BULK_BATCH_DELAY_MS` | 100 | Delay between bulk batches |
| `VALIDATION_RESULT_CACHE_TTL` | 60 | Validation result Redis cache TTL (seconds) |
| `SEARCH_RESULT_CACHE_TTL` | 120 | Search result Redis cache TTL (seconds) |
| `EMBEDDING_DIMENSION` | 1536 | Vector dimension (matches LLM model) |
| `ONTOLOGY_CONFIDENCE_THRESHOLD` | 0.7 | Below this -> pending human review |

---

## User Account Management (TBD)

> This section outlines the planned user identity and account features that will
> replace the current stub admin authentication. All stub code is marked with
> `TBD(user-accounts)` comments.

### Planned Components

- **User identity store**: PostgreSQL `users` table or external IdP integration (LDAP, OIDC)
- **Password hashing**: bcrypt via `passlib`
- **Group membership management**: Admin routes under `/admin/...`
- **Redis-backed refresh token revocation**: Replace the in-memory `_revoked_refresh_tokens` set
- **Account information transfer**: Map DataSpoke users to DataHub owner URNs
- **Cookie `secure` flag**: Tied to `DATASPOKE_COOKIE_SECURE` env setting
