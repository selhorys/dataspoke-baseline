# DataSpoke Backend

> This document specifies the backend service layer — feature services, shared
> libraries, Airflow DAG definitions, and infrastructure integration patterns
> that sit behind the API layer.
> Data contracts (PostgreSQL schema including pgvector tables) in
> [BACKEND_SCHEMA](BACKEND_SCHEMA.md).
>
> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Architecture context in [ARCHITECTURE](../ARCHITECTURE.md).
> API routes that delegate to these services in [API](../API.md).
> DataHub SDK patterns in [DATAHUB_INTEGRATION](../DATAHUB_INTEGRATION.md).
> Testing conventions in [TESTING](../TESTING.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Layered Architecture](#layered-architecture)
3. [Shared Services (`src/shared/`)](#shared-services-srcshared)
4. [Feature Services (`src/backend/`)](#feature-services-srcbackend)
5. [Event Emission](#event-emission)
6. [Airflow Workflows (`src/workflows/`)](#airflow-workflows-srcworkflows)
7. [Kafka Consumers *(optional, not enabled in baseline)*](#kafka-consumers-optional-not-enabled-in-baseline)
8. [Dependency Injection](#dependency-injection)
9. [Error Handling](#error-handling)
10. [Configuration](#configuration)

Data contracts (PostgreSQL schema including pgvector tables) are specified in
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
       |---> src/shared/   <- DataHub client, DB sessions, LLM client, pgvector client
       |
       +---> src/workflows/  <- Airflow DAG definitions + internal activity endpoints
```

**Key rule**: Business logic lives in `src/backend/`, not in API route handlers.
Route handlers validate input, call a service function, and format the response.
This keeps services testable independently of HTTP concerns.

Source layout is visible in the `src/` directory tree. The backend is organized
into feature modules under `src/backend/`, shared libraries under `src/shared/`,
and Airflow DAG definitions under `src/workflows/dags/`. See the code itself for
the current file structure.

---

## Layered Architecture

### Request Flow

```
1. Router         -> Parse HTTP, validate input (Pydantic), enforce auth
2. Service        -> Orchestrate business logic, call shared clients
3. Shared Client  -> DataHub SDK, PostgreSQL (incl. pgvector), Redis, LLM
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

1. **CRUD verb prefix** -- use the HTTP method as the function prefix: `get_`, `post_`,
   `put_`, `patch_`, `delete_`. Never use domain verbs (`run_`, `activate_`, `apply_`,
   etc.) as prefix.
2. **Explicit entity names** -- include all entity names from the URL path so the function
   name is self-describing.
3. **Omit meta classifiers** -- path segments `attr` and `method` are structural
   classifiers; omit them from the function name.

**Examples:**

| Route | Function name |
|-------|---------------|
| `GET /governance/metric/{id}/attr/conf` | `get_metric_conf` |
| `POST /governance/metric/{id}/method/run` | `post_metric_run` |
| `GET /spoke/ingestion/sources/{id}/attr/conf` | `get_ingestion_source_conf` |
| `POST /metagen/method/run` | `post_metagen_run` |
| `POST /spoke/common/data/{urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` | `post_data_metagen_item_candidate_review` |
| `POST /ontogen/result/node/{node_id}/method/review` | `post_ontogen_node_review` |
| `POST /ontogen/result/edge/{edge_id}/method/review` | `post_ontogen_edge_review` |
| `POST /ontogen/result/triple/{triple_id}/method/review` | `post_ontogen_triple_review` |

### Service Pattern

Every feature service is **stateless** -- dependencies are injected via constructor
(`DataHubClient`, `AsyncSession`, `RedisClient`, etc.), and all persistent state lives in
PostgreSQL, Redis, or DataHub. This allows any API instance or Airflow activity endpoint to
instantiate a service and call its methods. See any `src/backend/<feature>/service.py` for
the pattern.

---

## Shared Services (`src/shared/`)

Each shared service is a thin wrapper around an infrastructure client. See the source files
for current method signatures.

| Service | Module | Role | Key design decisions |
|---------|--------|------|---------------------|
| DataHub Client | `datahub/client.py` | Unified read/write wrapper around `acryl-datahub` SDK | Exponential backoff (3 attempts, 500ms base). Circuit breaker (opens after 5 failures, 60s probe). See [DATAHUB_INTEGRATION](../DATAHUB_INTEGRATION.md). |
| PostgreSQL | `db/session.py`, `db/models.py` | SQLAlchemy 2.0 async with `asyncpg`. Session factory + ORM models. | Pool size 10, max overflow 5 |
| Vector (pgvector) | `vector/client.py` | Table-backed vector upsert/search (cosine, HNSW-indexed). Shares the PostgreSQL session factory. | `PgVectorManager` + `VectorHit` dataclass; collection name whitelisted against `EMBEDDING_COLLECTION`. |
| Graph (Apache AGE, reserved) | `graph/client.py` | AGE extension installed on the same PG instance for future graph-shaped queries. `AgeGraph` exposes `materialize_triple` / `delete_triple` / `traverse` helpers usable by any service that opts in. | See [BACKEND_SCHEMA §Graph](BACKEND_SCHEMA.md#graph-apache-age-reserved). |
| LLM | `llm/client.py` | Provider-agnostic client (LangChain). Single completion, JSON completion, embedding, and tool-calling loop (`complete_with_tools`) bound to a service-supplied validator. | Provider/model from the `llm_provider`/`llm_model` runtime config (`/api/v1/admin/conf`); the API key is read at runtime from the `dataspoke-llm-secret` Secret and rotated online via the same conf surface. Loop semantics, validator rule tables, debate framework, and test-mode toggles defined in [BACKEND_LLM](BACKEND_LLM.md). |
| Redis | `cache/client.py` | Async wrapper for caching, rate limiting, pub/sub | -- |
| Notifications | `notifications/service.py` | Outbound notifications (email, in-app alerts). Used by Validation (UC2) and Governance (UC5). | Master toggle `DATASPOKE_NOTIFICATION_ENABLED` (default `false` -- no-ops in dev) |
| Domain Models | `models/` | Shared Pydantic models (`QualityScore`, `EventRecord`, etc.) -- internal domain objects, not API schemas | API schemas live in `src/api/schemas/` |
| Exceptions | `exceptions.py` | `DataSpokeError` hierarchy with error codes for HTTP mapping | See [Error Handling](#error-handling) |
| Settings | `settings.py` | Pydantic `Settings` class reading `DATASPOKE_*` env vars | -- |

### LLM Inference Loop

Bounded ReAct loop wrapping every structured-output LLM call (UC3 ontogen,
UC4 metagen). The mechanics (two-layer enforcement, iteration bounds,
exhaustion behaviour), per-service validator rule tables, the
producer-reviewer adversarial debate framework (used by both UC3 ontogen
and UC4 metagen), and the test-mode env-var toggles all live in
[BACKEND_LLM](BACKEND_LLM.md). Service sections below point at it where
they invoke the loop.

### Cache Key Conventions

| Pattern | TTL | Purpose |
|---------|-----|---------|
| `quality:{dataset_urn}:score` | 300s | Cached `QualityScore` aggregation for dataset attr-get |
| `ontogen:node:{node_id}` | 300s | Ontology Generation node lookup cache |
| `ontogen:edge:{edge_id}` | 300s | Ontology Generation edge lookup cache |
| `ontogen:triple:{triple_id}` | 300s | Ontology Generation triple lookup cache |
| `rate_limit:{user_id}` | 60s | Rate limiting counter |

---

## Feature Services (`src/backend/`)

### Dataset Service (`src/backend/dataset/`)

**Covers**: Base dataset resource endpoints (`GET /data/{urn}`, `GET /data/{urn}/attr`,
`GET /data/{urn}/event`)

Thin read-through service. Reads dataset identity/attributes from DataHub, aggregates
cross-domain event history from the unified `events` table. Does not own any PostgreSQL
configuration tables.

**DataHub aspects read**: `datasetProperties`, `editableDatasetProperties`, `ownership`,
`globalTags`, `glossaryTerms`, `schemaMetadata`, `editableSchemaMetadata`.

**`quality_score` (server-side)**: A 0–100 composite score computed from DataHub
aspects. The dataset service delegates to `src/backend/dataset/scoring.py`, which
combines five dimensions with fixed weights (must sum to 1.0):

| Dimension | Weight | Source aspect(s) | Computation |
|-----------|--------|------------------|-------------|
| `completeness` | 0.25 | `SchemaMetadata` | Percentage of fields with a non-empty `description`. |
| `freshness` | 0.25 | `Operation` (timeseries, latest) | 100 if last operation ≤ 1 day ago, 0 if ≥ 30 days, linear in between. |
| `schema_stability` | 0.15 | DataHub Timeline `getSchemaVersionList` | Starts at 100; subtracts `10 × major_changes + 1 × minor_change` over the last 30 days. Single-version (initial 0.0.0) scores 100. |
| `data_quality` | 0.20 | `DatasetProfile` (timeseries, latest) | Starts at 100; subtracts `avg(nullProportion) × 100`; further -30 if `rowCount == 0`. |
| `ownership_tags` | 0.15 | `Ownership`, `GlobalTags` | 100 if both present, 50 if exactly one, 0 if neither. |

`overall = round(sum(dimensions[k] × weights[k]), 2)`, clamped to `[0, 100]`. The
score is read-through cached at `quality:{dataset_urn}:score` (TTL 300s; see
[Cache Key Conventions](#cache-key-conventions)) and surfaced as the `quality_score`
field on per-dataset rows of the function list views (`GET /spoke/ingestion`,
`GET /spoke/validation`).
The full breakdown (`overall_score`, `dimensions`, optional `dimension_details`) is
returned by the dataset domain via the `QualityScore` model. The score is independent
of the validation feature — datasets without validation configs still have a
`quality_score` derived from aspects alone. The governance metric `validation-score`
(see §Metrics Service) is a separate measurement computed from `validation_results`,
not from aspects.

### Ingestion Service (`src/backend/ingestion/`)

**Covers**: MANIFESTO §2.1 Ingestion Control (UC1). Ingestion is modeled **per source /
recipe** — one source produces many datasets, mirroring DataHub. Behavioural narrative —
including the three-mode split — lives in
[USE_CASE §UC1](../USE_CASE_en.md#uc1-ingestion-control); DataHub aspect reads/writes and the
source-sync surfaces are catalogued in
[DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync).
The custom-extractor extension seam (how a forked extractor consumes `recipe.source.config`
and emits MCPs) is [below](#custom-extractor-authoring-contract). This section describes the
implementation only.

DataSpoke's two goals: **augment** DataHub's native ingestion with a custom, forkable
extractor for cases DataHub's connectors can't cover, and **make all ingestion visible** —
which datasets each source covers, and which are ingested in an unmanaged way.

**Ingestion modes** (see [`BACKEND_SCHEMA §ingestion_source`](BACKEND_SCHEMA.md#ingestion_source)):

| Mode | Who ingests | DataSpoke's role |
|------|-------------|------------------|
| `DATAHUB_MANAGED` | DataHub's own recipe + cron | Sync the source definition (recipe, schedule) down; map its datasets; mirror run events. **Read-only** in DataSpoke (DataHub is SSOT) |
| `ACTIVE_CUSTOM_MANAGED` | DataSpoke's Airflow tier DAG | Build the recipe; run the pluggable extractor; emit to DataHub; record runs; map datasets |
| `PASSIVE` | External pipeline / DataHub CLI (not registered as a DataHub source) | Record the registration + declared `AllowDenyPattern` scope; sync results from DataHub; map datasets |

**SSOT split**: DataSpoke owns *registration* (the source row, recipe, schedule, declared
scope); DataHub owns *results* (runs, observed datasets). This split enables freshness
measurement — registered cadence vs. observed runs.

**Supported `source.type` for `ACTIVE_CUSTOM_MANAGED`** (one extractor module per type,
registered by `recipe.source.type`):

| `source.type` | Status |
|---------------|--------|
| `postgres` | Implemented (table + column metadata only) |
| `kafka`, `mysql`, `oracle`, `bigquery`, `snowflake` | Fork-and-extend (custom-extractor seam) |

`DATAHUB_MANAGED` and `PASSIVE` are platform-agnostic on the DataSpoke side (DataSpoke does
not run the extractor).

**Aspects emitted** (non-dry-run, per discovered dataset, by the `ACTIVE_CUSTOM_MANAGED`
postgres extractor): `StatusClass(removed=False)`, `ContainerClass(container=<schema_container_urn>)`,
`DatasetPropertiesClass`, `SchemaMetadataClass`, plus `DataProcessInstance` start + complete
`RunEvent` aspects per run, and a two-level container hierarchy (database → schema) with a
`BrowsePathsV2Class` aspect on each dataset referencing both container URNs (explicit emission
for parity with DataHub's managed-PG source). See
[DATAHUB_INTEGRATION §Container URN Construction](../DATAHUB_INTEGRATION.md#container-urn-construction)
for the URN-parity invariant. `DatasetProperties.description` is sourced from PG
`obj_description()` and each `SchemaField.description` from `col_description()`; absent a
COMMENT, the dataset description falls back to
`"Ingested by DataSpoke: {database}.{schema}.{table}"`. The extractor MAY stamp
`systemMetadata.pipelineName` = the source id for observed mapping. Baseline scope is narrow —
no profiling, no stateful-ingestion soft-delete. `dry_run: true` runs the extractor and returns
the schema preview without emitting any aspects.

#### Implementation

CRUD for ingestion sources (PostgreSQL: `ingestion_source`, keyed on `id`). The recipe is
stored DataHub-compatible (`recipe.source.{type,config}`). See
[`BACKEND_SCHEMA §ingestion_source`](BACKEND_SCHEMA.md#ingestion_source) and
[`§ingestion_source_dataset`](BACKEND_SCHEMA.md#ingestion_source_dataset). Key columns:
`mode`, `name`, `platform` (= `recipe.source.type`), `recipe`, `schedule`, `datahub_source_urn`,
`status` (plus the internal-only derived `schedule_tier`).

**API body shape**: the request/response JSON mirrors the UC1 recipe YAML 1:1, using
DataHub-recipe-standard wording only — `{mode, name, schedule, recipe:{source:{type,config}}}`
— plus read-only management fields (`id`, `status`, `created_at`, `updated_at`, and
`datahub_source_urn` for `DATAHUB_MANAGED`). No DataSpoke-isms on the wire: `schedule` is the
cron string (not `schedule_cron`/`schedule_tier`), and the frontend renders/edits this JSON as
YAML. On `GET`, `${name__key}` secret references inside `recipe` are masked.

**Editability**: `DATAHUB_MANAGED` rows are read-only (DataHub is SSOT) — create/update/delete
return `409 INGESTION_SOURCE_READONLY`; they are written only by the sync sweep. `ACTIVE_CUSTOM_MANAGED`
and `PASSIVE` are user-managed via the API.

**Schedule**: `schedule` is a cron string. For `ACTIVE_CUSTOM_MANAGED`, on upsert the service
validates it maps to one of the three tiers (`hourly`/`daily`/`weekly`) and caches the result in
the internal `schedule_tier` column, which selects the Airflow tier DAG; `schedule: null` means
manual-only (runs only on `…/method/run`, never on a tier DAG). `DATAHUB_MANAGED` mirrors
DataHub's schedule; `PASSIVE` has none.

**Secret resolution** (`secret_resolver.py`): recipes reference secrets DataHub-compatibly as
`${name__key}`. Before a run, the service pre-resolves: split on the last `__` → read K8s Secret
`dataspoke-source-cred-<name>` key `<key>` (the `dataspoke-source-cred-` prefix vault) →
substitute plaintext into the recipe dict at run time. Plaintext is never persisted; the stored
recipe keeps only the `${name__key}` reference. Vault/verify flows, RBAC, and error taxonomy
live in [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md). Failures surface as
`IngestionResult(errors=[…])` → `status="error"`.

**Active-custom run pipeline** (`IngestionService.run()`): load source → reject if
`mode != ACTIVE_CUSTOM_MANAGED` (`409 INGESTION_RUN_NOT_APPLICABLE`) → reject concurrent run via
Redis SETNX (`ingestion:running:{source_id}`) → resolve `${name__key}` recipe secrets → emit
`DataProcessInstanceRunEvent(STARTED)` against a deterministic DPI URN derived from `run_id`
(skipped on `dry_run`) → dispatch to the extractor registered for `recipe.source.type`; emit
dataset aspects (skipped on `dry_run`; a non-dry-run that ingests zero entities is treated as
failure) → emit `DataProcessInstanceRunEvent(COMPLETE | FAILED)` (skipped on `dry_run`) → record
the extractor's emitted URNs into `ingestion_source_dataset` (`origin = emitted`, authoritative)
→ record `INGESTION.COMPLETE` / `INGESTION.FAIL` event (the run's `dry_run` boolean is preserved
in the event's `detail`; see [Event Catalogue](#event-catalogue)).

**Sync + mapping sweep** (`IngestionService.sync()`, called hourly by the `datahub-sync-hourly`
DAG) reconciles all modes:

1. **Source defs**: pull `DATAHUB_MANAGED` source recipes + schedules via DataHub's
   `listIngestionSources` / `ingestionSource(urn)`; upsert read-only rows. Mask secrets in the
   stored/displayed recipe (DataHub returns them raw).
2. **Mapping**: list the DataHub dataset set once and rebuild `ingestion_source_dataset` by
   evaluating each source's **filter-matcher** — derived from the recipe's `platform`+`database`+
   `schema_pattern`/`table_pattern` for `DATAHUB_MANAGED`/`ACTIVE_CUSTOM_MANAGED`; the declared `AllowDenyPattern`
   scope for `PASSIVE`. `origin = matcher`. Matching parses dataset URNs and applies filters the
   way the connector names them — declared/derived coverage, an explicit approximation (DataHub
   exposes no native source→dataset reverse lookup). A source with no derivable selection patterns
   (`schema_pattern`/`table_pattern`/`topic_patterns`/`dataset_pattern` all absent) maps no datasets.
3. **Observed enrichment (optional, the two MANAGED modes)**: read `systemMetadata.pipelineName`
   per dataset to link datasets to their source authoritatively — `DATAHUB_MANAGED` (DataHub
   stamps the source URN), `ACTIVE_CUSTOM_MANAGED` (DataSpoke's extractor stamps the source id).
   `origin = pipeline_name`. Not used for `PASSIVE`.
4. **Run events**: mirror run history into the `events` table — `listExecutionRequests` for
   `DATAHUB_MANAGED`; `Operation` / `DataProcessInstance` observation for `PASSIVE` — with
   `event_type = INGESTION.COMPLETE` / `INGESTION.FAIL`, deduplicated by
   `(entity_id, event_type, occurred_at)`.
5. **Unmanaged bucket**: datasets in DataHub linked to no source (served by
   `GET /spoke/ingestion/unmanaged`).

See [DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync)
for the GraphQL surfaces and field citations.

### Custom Extractor Authoring Contract

`ACTIVE_CUSTOM_MANAGED` runs DataSpoke's own pluggable extractor — the seam for covering
sources DataHub's native connectors can't. An extractor registry is keyed by
`recipe.source.type`; this release ships a **postgres extractor only**. A forked extractor
reads a subset of the DataHub-format `recipe.source.config`, crawls the source, and emits via
acryl-datahub MCP/schema classes. The DPI emission contract (required aspects, ordering,
failure semantics, URN convention, `systemMetadata` incl. stamping `pipelineName` = source id,
authoring checklist) lives in
[DATAHUB_INTEGRATION §Custom Extractor Guide](../DATAHUB_INTEGRATION.md#custom-extractor-guide).
Adding an extractor for a new `source.type` (with AI-assisted coding) is the expected
fork-and-extend path — the project's Productized-Scaffold identity.

**Run-event consumption**: every observed run maps into the dataset's `event/ingestion` timeline.
DataSpoke's own extractor records its runs inline (see run pipeline above). `DATAHUB_MANAGED` and
`PASSIVE` runs are observed by the `datahub-sync-hourly` DAG — `listExecutionRequests` for
DataHub-managed, and `DataProcessInstance` runs + ingestion-like `Operation` aspects
(`operationType ∈ {INSERT, UPDATE, CREATE, ALTER}`) for passive. `result.resultType = SUCCESS`
maps to `INGESTION.COMPLETE`, `FAILURE` to `INGESTION.FAIL`.

**DataSpoke's own conventions**:

- `runId = "dataspoke-{source_id}-{run_id}"`, with `run_id = uuid4()` per run.
- DPI URN = `urn:li:dataProcessInstance:{source_id}-{run_id}`, matching the `runId` suffix so
  dataset aspects and the DPI cross-reference cleanly.

**Reference implementation**: `src/backend/ingestion/service.py` and the extractor registry in
`src/backend/ingestion/extractors.py`.

### Validation Service (`src/backend/validation/`)

**Covers**: MANIFESTO §2.1 Validation (UC2). The full feature contract — philosophy,
scope, API surface, configuration / result shapes, DataHub aspect mapping, and the
single-rule-per-dataset rationale — lives in
[`spec/feature/VALIDATION.md`](VALIDATION.md). The DataHub assertion-aspect emission
conventions are in
[DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects).

DataSpoke is a **passive result store**: external pipelines run validation logic and
POST results; DataSpoke stores the configuration + results in PostgreSQL and emits the
matching DataHub aspects (`assertionInfo`, `assertionRunEvent`, `status`) on the
pipeline's behalf.

#### Implementation

`ValidationService` is thin, stateless, and built on the shared DataHub emitter and
PostgreSQL session. It exposes two responsibility areas — configuration CRUD and result
ingest/query — surfaced through the routes below.

**Configuration CRUD** — `GET/PUT/PATCH/DELETE /attr/validation/conf`.

- `PUT`/`PATCH` validates the body against the conf shape (`description ≤ 2,000` chars,
  `variables` list ≤ 200 entries, each entry matching `[a-z][a-z0-9_]{0,99}`, unique
  within the rule). The dataset must already exist in DataHub or the request is rejected
  with `422 DATASET_NOT_IN_DATAHUB`. On success the service:
  1. upserts the row in `validation_configs` (`dataset_urn` PK; clears `is_removed`),
  2. emits `assertionInfo` to DataHub with `type = CUSTOM`, `source.type = EXTERNAL`,
     `customAssertion.type = "DATASPOKE_VALIDATION"`,
     `customAssertion.entity = <dataset_urn>`, and
     `customAssertion.logic = "<comma-joined declared variable names>"`,
  3. emits `status.removed = false` together with `assertionInfo` to clear any prior
     soft-delete and resurrect the assertion at the same deterministic URN
     (`urn:li:assertion:<datahub_guid({"platform": "dataspoke-validation", "entity": dataset_urn})>`).
- `DELETE` performs a soft-delete: marks the row `is_removed = true` in
  `validation_configs` and emits `status.removed = true` to DataHub. The deterministic
  URN is preserved so a later `PUT` resurrects the same assertion.
- A DataHub error during `assertionInfo` / `status` emission surfaces as `502` or `503`
  per the DataHub error envelope — config save and DataHub assertion lifecycle are
  coupled by design because DataHub is the SSOT for assertion definitions.

**Result ingest and query** — `POST/GET /attr/validation/result`.

- `POST` validates `data_time` (RFC 3339 → `400 INVALID_PARAMETER` if not),
  `score ∈ [0.0, 1.0]` (else `422 INVALID_SCORE`), and `variables` keys ⊆ the conf's
  declared `variables` (else `422 UNKNOWN_VARIABLE` listing the offending names).
  Missing declared keys are accepted silently — partial coverage is a legitimate signal.
  On success the service:
  1. inserts the row in `validation_results` (`dataset_urn`, `data_time`, `score`,
     `variables` JSONB, `ingestion_time = now()`),
  2. emits `assertionRunEvent` to DataHub with `timestampMillis = data_time` (epoch ms,
     UTC), `runId = uuid4()`, `result.type = SUCCESS` if `score == 1.0` else `FAILURE`,
     `result.actualAggValue = score`, `result.nativeResults` populated as
     `Map<string,string>` with `repr(float)` of each variable plus `"score"` itself,
     `runtimeContext.ingestion_time = now()`.
- `GET` filters `validation_results` by `data_time ∈ [from, until)` (server cap
  `limit ≤ 10,000`, default `1,000`); when multiple rows share a `data_time`, returns
  the most recent (last-write-wins) — see VALIDATION.md §Duplicate `data_time` policy.

**Append-only timeseries.** Multiple POSTs with the same `data_time` are stored as
distinct rows and emit distinct `assertionRunEvent` entries; this matches DataHub's
timeseries aspect being fundamentally append-only. The GET endpoint collapses duplicates
on read.

**Run-event emission is best-effort but not silent.** A DataHub error while emitting
`assertionRunEvent` keeps the row in `validation_results` (the local store remains the
source of truth for the historical-baseline cache) but returns `502/503` to the caller
so the pipeline can decide whether to retry.

**Trigger surface.** The data pipeline is the trigger — it computes the result and
POSTs it.

**Multi-rule scope-out.** Teams that need multiple distinct checks per dataset (separate
freshness / volume / field assertions, per-column validators, multi-team ownership) use
DataHub's native assertion APIs directly; DataSpoke is the opinionated single-rule
shortcut for the 80% case, not the only path.

### Metadata Generation Service (`src/backend/metagen/`)

**Covers**: MANIFESTO §2.1 Metadata Generation (UC4). Behavioural narrative —
including the global-conf / per-dataset-boundary split, item / candidate
model, and approval lifecycle — lives in
[USE_CASE §UC4](../USE_CASE_en.md#uc4-metadata-generation). DataHub aspect
write rules are catalogued in
[DATAHUB_INTEGRATION §Editable vs Non-Editable Description Aspects](../DATAHUB_INTEGRATION.md#editable-vs-non-editable-description-aspects).
This section describes the implementation only.

**Singleton conf** at `/spoke/metagen/attr/conf` — there is no
per-dataset operational config; the per-dataset row
(`/spoke/common/data/{urn}/attr/metagen/conf`) is the opt-in boundary only.
`GET` on either resource returns a `null` body with `200 OK` when the row has
never been written; clients distinguish the unset state by `null` rather than
by a `404`. Fields:

| Field | Purpose |
|-------|---------|
| `is_enabled` | Master switch for the metagen DAG. |
| `schedule_tier` | `hourly` / `daily` / `weekly` re-generation cadence. |
| `dataset_filter` | Optional scope filter — `origin` (DataHub `FabricType` value carried as the third URN segment; AND-ed with the OR-group), `tags`, `glossary_terms`, `dataset_urns` (OR-ed across the three list dimensions; `{}` means all). Each list dimension is capped at 1,000 entries (`422 INVALID_PARAMETER` on overflow); URN format validated at PUT/PATCH (`422 INVALID_DATASET_URN`); unresolved-at-runtime entries are skipped and reported in the run-complete event's `unresolved_urns`. Same shape as UC3 `ontogen/attr/conf.dataset_filter` and UC5 `metric/{metric_id}/attr/conf.dataset_filter`. |
| `result_limit` | Integer ∈ `[1, 20]`, default `3`. Maximum candidate count per item at any time. |
| `overwrite_pending` | Boolean, default `true`. When an item already holds `result_limit` non-rejected candidates and has no `approved` candidate, controls whether a new run evicts the oldest `llm_approved` candidate (`true`) or skips the item (`false`). |

The conf is a single row in `metagen_config` (singleton table; see
[BACKEND_SCHEMA §metagen_config](BACKEND_SCHEMA.md#metagen_config)).

**Per-dataset boundary** at `/spoke/common/data/{urn}/attr/metagen/conf`,
stored in `metagen_boundary`. A row with `is_enabled=true` opts the dataset
in; missing row or `is_enabled=false` is opt-out. The `allowed` array
restricts which element kinds the global generator may write on this
dataset. Baseline values: `dataset.description`, `column.description`.

**Item kinds**. Two values supported in baseline:

| `kind` | DataHub write target |
|--------|----------------------|
| `dataset.description` | `editableDatasetProperties.description` |
| `column.description` | `editableSchemaMetadata.editableSchemaFieldInfo[].description` keyed by `fieldPath` |

Future scope: `domains` and `globalTags` proposals.

**Item status** (derived from sibling candidates, surfaced on `GET .../metagen/item` and `.../metagen/item/{item_id}` responses):

| Status | Condition |
|--------|-----------|
| `approved` | The item has one candidate with `status='approved'` (the partial unique index guarantees at most one). |
| `llm_approved` | No approved candidate, but at least one `llm_approved` candidate awaits review. |
| `pending` | No non-rejected candidates exist for the item yet — typically a freshly enumerated slot before its first successful debate run. |

Status is not persisted; it is computed per request from `(has_approved, candidate_count)` over the item's candidates.

**Generation Pipeline** (Airflow tier DAG, schedule from
`metagen_config.schedule_tier`, or manual `POST /method/run`):

1. Enumerate **in-scope datasets** — union of datasets matching the global
   `dataset_filter` (`origin` AND-ed with the OR-group of `tags`,
   `glossary_terms`, `dataset_urns`) **intersected** with the set of datasets
   that have a `metagen_boundary` row with `is_enabled=true`. Boundary-less or
   boundary-disabled datasets are excluded regardless of `dataset_filter`.
   Unresolved `dataset_urns` entries are accumulated for the run-complete
   event's `unresolved_urns`. If the in-scope set is empty, the run still
   completes successfully and emits `METAGEN.RUN_COMPLETE` with all
   counts at zero so reviewers and ops dashboards see every scheduled
   tick.
2. **Clear `rejected` candidates** across all in-scope datasets so the
   per-item budget frees up.
3. Per in-scope dataset, assemble the Producer evidence dictionary from four
   sources. The Producer prompt is the union of all four — the LLM never
   queries DataHub or pgvector itself.

   - **DataHub static + editable aspects.** `datasetProperties`,
     `schemaMetadata`, `editableDatasetProperties`, `editableSchemaMetadata`,
     `glossaryTerms`.
   - **Related documents.** `documentInfo.contents.text` on `document`
     entities whose `relatedAssets` overlap the dataset. Capped at 5
     documents; each title/body capped per the shared untrusted-content size
     limit (see [BACKEND_LLM §Inference Loop](BACKEND_LLM.md#inference-loop)).
     Matches the UC3 ontogen evidence diet.
   - **Curated ontology assignments.** UC3 nodes filtered by
     `dataset_node_map.status='approved'` — the human-authored
     dataset-to-node binding. Metagen excludes `llm_approved` rows so
     user-facing metadata is gated to human-curated ontology entities. Empty
     for datasets with no explicit assignments.
   - **Per-dataset ontology RAG.** Embed the dataset's textual context (URN +
     name + description + field paths) and run bounded top-k vector search
     against `node_embeddings`, `edge_embeddings`, `triple_embeddings` (the
     three approved-ontology pgvector collections from UC3). Hits hydrate to
     `{id, name, description}` / `{id, label}` / `{subject_name, edge_label,
     object_name}` and surface as a distinct prompt section. Top-k is
     per-collection-tunable; setting a collection's k to `0` disables that
     contribution. Distinct from the curated-ontology path: the curated path
     is reviewer-asserted bindings; the RAG path surfaces semantically-nearby
     approved ontology fragments regardless of assignment. Both coexist in
     the prompt. RAG failure is best-effort — the evidence dict falls back
     to empty lists and the run proceeds.
4. **Enumerate target items** — `(dataset_urn, dataset.description)` and one
   `(dataset_urn, column.<fieldPath>.description)` per column. Drop items
   whose kind is outside the dataset's `metagen_boundary.allowed`. Drop
   items that currently have an `approved` candidate — the reviewer has
   expressed a settled preference, so the run pauses on this item until
   the approval is moved to a different sibling.
5. **Producer-Reviewer Adversarial Debate** generates candidates per
   surviving (dataset, item) pair. See
   [BACKEND_LLM §Metagen Adversarial Debate](BACKEND_LLM.md#metagen-adversarial-debate).
   The producer emits candidate `value`s; the reviewer evaluates each
   against ontology context and existing approved descriptions; only
   candidates with reviewer outcome `accept` and
   `confidence_score >= METAGEN_CONFIDENCE_THRESHOLD` persist.
6. **Apply per-item budget** — for each (dataset, item) whose surviving
   candidate count exceeds the slack (`result_limit - non_rejected_count`),
   either evict the oldest `llm_approved` candidate (FIFO by `created_at`,
   when `overwrite_pending=true`) or drop the new candidate
   (when `overwrite_pending=false`).
7. **Persist** the accepted candidates as `metagen_candidates` rows with
   `status='llm_approved'`. Refresh `metagen_candidate_embeddings` for the
   approved candidates that will inform the next run's Reviewer RAG.

The LLM step in step 5 runs inside the
[Inference Loop](BACKEND_LLM.md#inference-loop) with the producer-reviewer
adversarial debate enabled. Bound tool `metagen_validate(payload)`, schema
model `MetagenLLMOutput`. Validator rule table:
[BACKEND_LLM §Metagen Validator](BACKEND_LLM.md#metagen-validator).

**Approval flow** (mutable).
`POST /spoke/common/data/{urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review`
with body `{verdict, reason}`:

- `verdict: "approve"` → in a single transaction: flip the target
  candidate's status to `approved`, flip any previously-`approved` sibling
  on the same item back to `llm_approved`, then emit the new value to the
  corresponding editable DataHub aspect and emit `METAGEN.CANDIDATE_APPROVE`.
  Approval is mutable — the reviewer can switch which sibling is approved at
  any time, and the partial unique index `UNIQUE (dataset_urn, item_id)
  WHERE status='approved'` keeps "at most one approved per item" a hard
  invariant. Generation runs skip items that currently have an `approved`
  candidate, so accumulating siblings only happens before the first
  approval (or after the user demotes the current approval by approving a
  different sibling).
- `verdict: "reject"` → flip the candidate's status to `rejected` and emit
  `METAGEN.CANDIDATE_REJECT`. The row is deleted at the start of the next
  run. Reject is only valid for `llm_approved` candidates; rejecting an
  `approved` candidate returns `409 METAGEN_CANNOT_REJECT_APPROVED` (to
  drop the current approval the reviewer approves a different sibling,
  which atomically demotes the current one).

Sibling `llm_approved` candidates on the same item are not auto-touched on
approval — they remain visible as read-only history and are eligible for
later approval.

**Concurrency**. Generation runs are serialised by a global Redis lock
(`metagen`). A duplicate `method/run` while one is in flight returns
`409 METAGEN_RUNNING`.

**Disabled-config rejection**. `method/run` with `is_enabled=false` and
`dry_run=false` raises `409 METAGEN_DISABLED`. Dry-run is permitted
regardless of `is_enabled`.

**Boundary guard**. Candidate review against a dataset whose
`metagen_boundary` is absent or `is_enabled=false` returns
`422 METAGEN_DATASET_NOT_IN_BOUNDARY`.

### Ontology Generation Service (`src/backend/ontogen/`)

**Covers**: MANIFESTO §2.1 Ontology Generation (UC3). Consumed by Metadata Generation
(UC4).

Singleton-config LLM pipeline that emits a **subject / predicate / object triple
ontology** — nodes (subjects / objects), edges (predicates), and triples
(`(subject_node, edge, object_node)` facts). Storage backed by PostgreSQL relational
tables + pgvector embeddings. Independent review workflow (approve / reject) per
result type, with triple review gated on its endpoint nodes and edge being approved.

**Singleton conf** at `/spoke/ontogen/attr/conf` — there is no per-dataset ontology
config. Fields:

| Field | Purpose |
|-------|---------|
| `is_enabled` | Master switch for the inference DAG. |
| `schedule_tier` | `hourly` / `daily` / `weekly` re-inference cadence. |
| `dataset_filter` | Optional scope filter — `origin` (DataHub `FabricType` value carried as the third URN segment; AND-ed with the OR-group), `tags` (DataHub tag URNs), `glossary_terms` (DataHub glossary term URNs), and `dataset_urns` (explicit `urn:li:dataset:(…)` URN list). The three list dimensions are OR-ed among themselves; `{}` means all. Each list dimension is capped at 1,000 entries (`422 INVALID_PARAMETER` on overflow); URNs validated at PUT/PATCH (`422 INVALID_DATASET_URN`); unresolved-at-runtime entries are skipped and reported in the run-complete event's `unresolved_urns`. Same shape as UC4 `metagen/attr/conf.dataset_filter` and UC5 `metric/{metric_id}/attr/conf.dataset_filter`. |
| `default_run_prompt` | Optional Markdown string used as the one-shot prompt for runs without an explicit body — i.e., the periodic Airflow DAG and manual `POST /method/run` calls with no body. Null disables the default. |

UC3 inputs are sourced entirely from DataHub-resident metadata (the proofread
boundary shared with UC4).

The conf is a single row in `ontogen_config` (singleton table; see
[BACKEND_SCHEMA §ontogen_config](BACKEND_SCHEMA.md#ontogen_config)).

**Seeds** at `/spoke/ontogen/attr/seed/{seed_id}` are human-authored Markdown
documents (prompts, domain hints, naming conventions) that the inference run consumes
alongside the data sources. The endpoint accepts and returns raw Markdown
(`Content-Type: text/markdown`); only `seed_id` and timestamps are managed out-of-band.
Stored in `ontogen_seeds` (see
[BACKEND_SCHEMA §ontogen_seeds](BACKEND_SCHEMA.md#ontogen_seeds)).

**Triple model**. The baseline ontology is built around three independently reviewable
result types — *node* (subject / object), *edge* (predicate), *triple*
(`(subject_node, edge, object_node)`). A triple references nodes and edges by ID and
inherits their lifecycle: a triple may only be approved once both endpoint nodes and
the edge are approved. There is no parent/child hierarchy among nodes.

**Inference Pipeline** (Airflow tier DAG, schedule from `ontogen_config.schedule_tier`,
or manual `POST /method/run`):

1. Load the working ontology — all `status='approved'` rows from `ontogen_nodes`,
   `ontogen_edges`, `ontogen_triples` (incremental inference; pending and rejected
   rows are not carried forward).
2. Enumerate datasets matching `dataset_filter` from DataHub — union of datasets
   carrying any listed `tags`, any listed `glossary_terms`, and any of the explicit
   `dataset_urns`, AND-ed with `origin` when set. Listed URNs that don't resolve
   are skipped and accumulated for the run-complete event's `unresolved_urns`.
   `{}` means all datasets.
3. Fetch DataHub evidence per in-scope dataset (the proofread boundary shared
   with UC4): `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
   `editableSchemaMetadata`, `glossaryTerms`, and `documentInfo.contents.text` on
   `document` entities whose `relatedAssets` reference an in-scope dataset
   (Markdown body, capped per dataset). DataSpoke writes the editable aspects
   only after a UC4 reviewer approves a candidate; unreviewed candidates stay in
   `metagen_candidates` with `status='llm_approved'`, so *presence* in DataHub is
   the approval signal — UC3 reads DataHub directly with no JOIN against
   `metagen_candidates`.
4. Load active seeds (`ontogen_seeds.status='active'`). Resolve the one-shot prompt:
   if the `POST /method/run` request carries a non-empty `text/markdown` body, use
   that body; otherwise fall back to `ontogen_config.default_run_prompt` (used by both
   the periodic Airflow DAG and bodyless manual calls). The one-shot prompt is
   appended after the seeds and is not stored.
5. LLM proposes nodes per dataset. For each candidate, look up the closest existing
   node via `node_embeddings` (cosine similarity, threshold
   `ONTOLOGY_NODE_REUSE_THRESHOLD`); if a match exists, reuse the existing node ID.
   The reuse pool spans all non-`rejected` statuses (`llm_pending`, `llm_approved`,
   `approved`) so same-name proposals consolidate to one row regardless of which
   gate it has cleared. Otherwise emit a new `llm_pending` node.
6. LLM proposes the edge (predicate) vocabulary, applying the same reuse rule
   against `ontogen_edges`.
7. LLM composes triples referencing already-proposed nodes and edges. Reuse
   existing triples when subject / edge / object match.
8. Score confidence per node, edge, and triple (below
   `ONTOLOGY_CONFIDENCE_THRESHOLD` queued for human review).
9. Persist new / updated rows to PostgreSQL (relational + pgvector); refresh
   `node_embeddings` for any node whose name or description changed.

Steps 5–7 are executed as a single LLM call wrapped in the
[Inference Loop](BACKEND_LLM.md#inference-loop). Bound tool
`ontogen_validate(payload)`, schema model `OntogenLLMOutput` (nodes / edges
/ triples arrays). The full validator rule table lives in
[BACKEND_LLM §Ontogen Validator](BACKEND_LLM.md#ontogen-validator). An
opt-in adversarial debate layer that adds a Reviewer agent on top of the
Producer's inference loop is specified in
[BACKEND_LLM §Adversarial Debate Framework](BACKEND_LLM.md#adversarial-debate-framework).

Concurrent inference runs return `409 ONTOGEN_RUNNING`; `?dry_run=true` evaluates
steps 2–8 without persisting.

**Trigger surface**. Inference is triggered by `POST /spoke/ontogen/method/run`
(synchronous, in-process) or one of the three periodic tier DAGs. Concurrency across all
triggers is enforced by the Redis `ontogen:running:singleton` SET NX guard.

**Disabled-config rejection**: `method/run` with `is_enabled=false` and `dry_run=false`
raises `409 ONTOGEN_DISABLED`. Dry-run is permitted regardless of `is_enabled`.

**Approval flow**. Each result type uses `POST /spoke/ontogen/result/{node|edge|triple}/{id}/method/review`
with `{verdict, reason}`. Approval flips `status` in DataSpoke storage; the ontology
graph lives entirely in DataSpoke (relational + pgvector).

- **Node** `verdict: "approve"` → mark the node and its `dataset_node_map`
  memberships as approved.
- **Edge** `verdict: "approve"` → mark the edge (predicate vocabulary entry) as
  approved.
- **Triple** `verdict: "approve"` → requires both endpoint nodes and the edge to
  carry `status='approved'` (human-approved). An `llm_approved` dependency does NOT
  satisfy the gate — the human must explicitly approve each component first.
  Otherwise returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`. On success, marks the
  triple as `approved`.
- `verdict: "reject"` → mark the result as rejected. Rejecting a node or edge does
  not auto-reject dependent triples — those simply remain stuck on
  `ONTOGEN_TRIPLE_DEPENDENCY_PENDING` until reinference produces a different
  proposal.

Each verdict emits a `NODE.APPROVE` / `NODE.REJECT` / `EDGE.APPROVE` / `EDGE.REJECT` /
`TRIPLE.APPROVE` / `TRIPLE.REJECT` event.

### Metrics Service (`src/backend/metrics/`)

**Covers**: MANIFESTO §2.1 Governance (UC5) — metric definition and aggregation. Built-in
metric types (`ingestion-freshness`, `validation-score`, `doc-health`), mode semantics,
factory defaults, `dataset_filter`, and result shape live in
[USE_CASE §UC5](../USE_CASE_en.md#uc5-governance); DataHub aspect reads in
[DATAHUB_INTEGRATION §Aspect Usage by Feature](../DATAHUB_INTEGRATION.md#aspect-usage-by-feature).

**Pure aggregation**: metrics never probe source data — they aggregate pre-existing
DataHub aspects (`DatasetProperties`, `SchemaMetadata`, `GlobalTags`, `GlossaryTerms`,
and the dataset URN's `origin` segment for the origin filter) and DataSpoke event /
validation-result rows. The metrics layer therefore has no source credentials and no
SQL access to production databases. Unsupported `metric_type` or unknown `metrics[]`
keys return `422 INVALID_PARAMETER`.

#### Implementation

Metric definition CRUD (PostgreSQL: `metric_definitions`). Scheduled (Airflow
`metrics-{hourly,daily,weekly}` DAGs scanning `is_enabled=true` rows of the matching
`schedule_tier`) or on-demand (`POST .../method/run` → `metrics` on-demand DAG)
measurement execution.

**Create vs replace**: `POST /spoke/governance/metric` creates a metric — `metric_id` is supplied
in the body and must not collide with an existing row (`MetricsService.create_metric_config`
raises `409 METRIC_EXISTS`; the concurrent-create race is closed by catching the primary-key
`IntegrityError` and re-raising the same conflict). `PUT .../attr/conf` replaces an existing
definition only (`replace_metric_config` raises `404 METRIC_NOT_FOUND` when absent); it does
not create. `PATCH` applies a partial update. The factory-default bootstrap inserts rows
directly and is unaffected by the create route.

**Mode**: `active` runs the built-in measurer matching `metric_type`. `passive` is
rejected at the schema layer with `501 NOT_IMPLEMENTED` — placeholder for ingesting
results emitted by an external system, deferred to a future release.

**Time windows** (`ingestion-freshness`, `validation-score`): the measurement window is
resolved **per dataset**, not from a single `metric_conf` value. `metric_conf.time_window_sec`
is only the fallback used when no per-dataset window can be derived.

- `ingestion-freshness`: the window is read from each dataset's owning ingestion source
  (resolved via the `ingestion_source_dataset` mapping). `ACTIVE_CUSTOM_MANAGED` /
  `DATAHUB_MANAGED` with a schedule → `SCHEDULE_TIER_SECONDS[schedule_tier] × 2`; `PASSIVE` (no
  schedule) → `PASSIVE_SYNC_PERIOD_SEC × 2` (mirrors the `@hourly` `datahub-sync-hourly` DAG);
  a dataset mapped to no source, or a source with no derivable schedule →
  `metric_conf.time_window_sec`. The `× 2` factor leaves room for transient late ingestion.
  Tier→seconds and the passive period live in `src/shared/schedule.py`.
- `validation-score`: for each dataset the measurer reads its most recent `N + 1` validation
  results (`N` = the `validation_score_n_intervals` runtime config (`/api/v1/admin/conf`),
  default 3) and sets the window to `mean(last N inter-arrival gaps) × 2`. A dataset with
  fewer than `N + 1` results falls back to `metric_conf.time_window_sec`. The score counted
  is the latest result whose `data_time` is inside the window.

Both measurers stay pure-aggregation and DataSpoke-DB-side: they read `events`,
`validation_results`, `ingestion_source`, and `ingestion_source_dataset` only — no DataHub call.

**Measurers** (`src/backend/metrics/measurers/`): one async function per built-in
`metric_type`, registered via the measurer registry. Each measurer receives the resolved
dataset URN list, `metric_conf`, a `DataHubClient`, and an `AsyncSession`, and returns
`(values: dict[str, float], breakdown: dict)`. `values` keys are exactly those listed in
[USE_CASE §UC5 — Built-in active metric types](../USE_CASE_en.md#built-in-active-metric-types);
the service filters the dict to the subset declared by `attr/conf.metrics[]` before
persisting.

**`doc-health`** sources table description from `DatasetPropertiesClass.description` (or
`EditableDatasetPropertiesClass.description` when present) and column descriptions from
`SchemaMetadataClass.fields[*].description` (overlaid by `EditableSchemaMetadataClass`
when present). A dataset scores `1.0` iff the resolved table description is non-empty
and every column has a non-empty description; otherwise `0.0`.

**Dataset resolution**: UC3 ontogen, UC4 metagen, and UC5 metrics services share
the same `dataset_filter` resolver in `src/backend/_dataset_filter.py`. The resolver
calls `DataHubClient.enumerate_datasets(origin=…, tags=…, glossary_terms=…)`. The
client emits `origin` as its own AND-clause inside each `or` clause of
`scrollAcrossEntities` so DataHub returns datasets that match the requested origin
AND any one of the tag / glossary-term clauses — see
[DATAHUB_INTEGRATION §Origin filter group](../DATAHUB_INTEGRATION.md#origin-filter-group)
for the GraphQL shape and the `FabricType` enum it accepts. `origin` values are
forwarded to DataHub verbatim; unknowns are rejected at DataHub query time.
Explicit `dataset_urns` are AND-ed against `origin` by inspecting each URN's third
segment before the per-URN aspect probe; mismatches and URNs that don't resolve in
DataHub at run time are accumulated into the run-complete event's
`unresolved_urns`.

**Breakdown format**: Every measurement result includes a `breakdown` JSONB with a
unified shape:

```
{"dataset_count": <total scanned>, "datasets": [{"urn": "...", "detail": {...}}]}
```

`datasets[]` lists **only failed datasets** — membership in the list is itself
the classification. A dataset is failed when:

- `ingestion-freshness`: latest `INGESTION.COMPLETE` is older than the dataset's
  freshness window (see **Time windows** above) or absent
- `validation-score`: latest validation `score` inside the dataset's window is `< 1.0`
  (or no result inside the window)
- `doc-health`: documentation score is `< 1.0` (table description missing OR any
  column description missing)

`detail` is optional, type-specific metadata. `ingestion-freshness` and `validation-score`
report the applied window via `time_window_sec` (the resolved per-dataset value) and
`window_source` (`"managed:<tier>"` / `"passive"` / `"default"` for freshness;
`"intervals"` / `"default"` for validation-score), alongside `last_event_at` (freshness)
or `latest_data_time` + `score` (validation-score). `dataset_count` is the total scanned
(matching `dataset_filter`),
not the number of failed entries; `len(datasets) == failed count` is implied. The
breakdown lets time-range queries on `attr/result` answer per-dataset historical
questions without re-running the metric.

**Factory defaults**: On API startup, an idempotent bootstrap inserts one
`metric_definitions` row for each built-in `metric_type` if absent. Defaults are
`mode="active"`, `is_enabled=false`, `schedule_tier="daily"`, `dataset_filter={}`,
type-appropriate `metric_conf` (`{"time_window_sec": 172800}` for the first two, `{}`
for `doc-health`). Seeds ship disabled so scheduled DAG runs are a no-op until the
governance lead PATCHes `is_enabled=true`; the bootstrap never overwrites an
existing row.

**Disabled-config rejection**: `method/run` with `is_enabled=false` and `dry_run=false`
raises `409 METRIC_DISABLED`. This check is enforced both in `MetricsService._run_inner()`
and at the route layer in `post_metric_run()` (which bypasses `MetricsService.run()` to
call Airflow directly). Dry-run is permitted regardless of `is_enabled`.

---

## Event Emission

Every successful mutating API call records an event to the unified `events`
table (see [BACKEND_SCHEMA §events](BACKEND_SCHEMA.md#events)). GET requests
do not emit events. If a request is rejected before reaching the service layer
(e.g., 409 concurrency guard, 404 not found), no event is recorded.

### Naming Convention

Event type values are **uppercase**, dot-delimited: `{DOMAIN}.{ACTION}`.

- **Domain** identifies the feature: `INGESTION`, `VALIDATION`, `METAGEN`,
  `METRIC`, `NODE`, `EDGE`, `TRIPLE`, `ONTOGEN`.
- **Action** describes what happened. Two categories:
  - *Config lifecycle*: `CONFIG_CREATE`, `CONFIG_UPDATE`, `CONFIG_DELETE` —
    emitted by PUT, PATCH, DELETE on a configuration resource.
  - *Action*: domain-specific operations beyond CRUD (pipeline runs, approvals,
    state transitions).

### Event Catalogue

Config-lifecycle actions (`CONFIG_CREATE`, `CONFIG_UPDATE`, `CONFIG_DELETE`) are emitted
by every domain that owns a config — `INGESTION`, `VALIDATION`, `METRIC`,
`ONTOGEN` (singleton), `METAGEN` (singleton). Domain-specific actions:

| Domain (`entity_type`) | Action | Trigger |
|---|---|---|
| `INGESTION` (`dataset`) | `COMPLETE` / `FAIL` | An ingestion run completes — `ACTIVE_CUSTOM_MANAGED` via `POST sources/{id}/method/run` inline; `DATAHUB_MANAGED`/`PASSIVE` mirrored by the sync sweep |
| `VALIDATION` (`dataset`) | `RESULT_RECORDED` | `POST attr/validation/result` succeeds (one event per accepted result) |
| `METAGEN` (`metagen`) | `RUN_COMPLETE` / `RUN_FAILED` | global generation run end; `RUN_COMPLETE` recorded for both dry-run and non-dry-run, `dry_run` flag in detail. Detail keys: `run_id` (uuid4), `unresolved_urns` (list, same shape as METRIC), `counts` (dict — `items_considered`, `candidates_added`, `candidates_evicted`, `rejected_cleared` on real-run; `items_considered`, `candidates_proposed` on dry-run), `dry_run`, `producer_iterations`, `debate_outcome` (`accept` / `turns_exhausted` / `cycle_detected`) |
| `METAGEN` (`dataset`) | `CANDIDATE_APPROVE` / `CANDIDATE_REJECT` | `POST attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` with `verdict: "approve"\|"reject"`. Detail keys: `item_id`, `candidate_id`, `reason` |
| `METRIC` (`metric`) | `RUN_COMPLETE` | `POST method/run` succeeds. Detail keys: `run_id`, `metric_id`, `values` (dict[str,float] — the persisted result), `dry_run`, `unresolved_urns` (list — `dataset_filter.dataset_urns` entries that didn't resolve in DataHub), `breakdown_summary` (`{dataset_count, affected_count}`) |
| `ONTOGEN` (`ontogen`) | `SEED_CREATE` / `SEED_UPDATE` / `SEED_DELETE` | seed CRUD on `attr/seed/{seed_id}` |
| `ONTOGEN` (`ontogen`) | `RUN_COMPLETE` / `RUN_FAILED` | re-inference run end; `RUN_COMPLETE` recorded for both dry-run and non-dry-run, `dry_run` flag in detail. Detail keys: `run_id` (uuid4), `unresolved_urns` (list, same shape as METRIC), `counts` (dict — `nodes_added/edges_added/triples_added` on real-run, `nodes_proposed/edges_proposed/triples_proposed` on dry-run), `dry_run`, `producer_iterations` (inference-loop turns the Producer took), `producer_errors_dropped` (validator-rejected row count), `debate_outcome` (`accept` / `turns_exhausted` / `cycle_detected`) |
| `NODE` / `EDGE` / `TRIPLE` (`node` / `edge` / `triple`) | `APPROVE` / `REJECT` | `POST ontogen/result/{type}/{id}/method/review` |

### Querying Events

- **Entity-level endpoint** (`GET .../data/{urn}/event`): returns all events
  for the entity regardless of domain — filters only by `entity_type` +
  `entity_id`.
- **Domain-level endpoint** (`GET .../event`): additionally
  filters by `event_type` prefix (e.g., `INGESTION.%`) to return only
  domain-specific events.

See [BACKEND_SCHEMA §events](BACKEND_SCHEMA.md#events) for the filtering
convention and [API §Meta-Classifier Conventions](../API.md#meta-classifier-conventions)
for the response contract.

---

## Airflow Workflows (`src/workflows/`)

### Architecture

Apache Airflow serves as the workflow orchestration engine with LocalExecutor. Workflows
are defined as Python DAG files in `src/workflows/dags/`. Each DAG uses Airflow's
`HttpOperator` to call internal activity endpoints on the DataSpoke API at
`/internal/activities/{domain}/*`. Airflow handles scheduling, retry, and execution.

### Airflow Client Subpackage (`src/workflows/airflow/`)

Wraps Airflow's REST API via `httpx`: DAG verification, DAG run lifecycle (trigger, poll,
wait), conf-based dedup, and cleanup. See the source files for current API.

### DAG Catalogue

Source of truth: `src/workflows/registry.py` exposes `ALL_DAG_IDS`
(on-demand + periodic + sync). Admin DAG-verification imports it directly.

| DAG | File | Trigger | Schedule |
|-----|------|---------|----------|
| `ingestion-active-hourly` | `ingestion_active_hourly.py` | Airflow schedule | `@hourly` |
| `ingestion-active-daily` | `ingestion_active_daily.py` | Airflow schedule | `@daily` |
| `ingestion-active-weekly` | `ingestion_active_weekly.py` | Airflow schedule | `@weekly` |
| `datahub-sync-hourly` | `datahub_sync_hourly.py` | Airflow schedule | `@hourly` |
| `metrics-hourly` | `metrics_hourly.py` | Airflow schedule | `@hourly` |
| `metrics-daily` | `metrics_daily.py` | Airflow schedule | `@daily` |
| `metrics-weekly` | `metrics_weekly.py` | Airflow schedule | `@weekly` |
| `metagen-hourly` | `metagen_hourly.py` | Airflow schedule | `@hourly` |
| `metagen-daily` | `metagen_daily.py` | Airflow schedule | `@daily` |
| `metagen-weekly` | `metagen_weekly.py` | Airflow schedule | `@weekly` |
| `metrics` | `metrics.py` | API | On-demand |
| `ontogen-hourly` | `ontogen_hourly.py` | Airflow schedule | `@hourly` |
| `ontogen-daily` | `ontogen_daily.py` | Airflow schedule | `@daily` |
| `ontogen-weekly` | `ontogen_weekly.py` | Airflow schedule | `@weekly` |
| `auth-role-sync-daily` | `auth_role_sync_daily.py` | Airflow schedule | `@daily` |

> **Tier-DAG selection**: For features with a `schedule_tier` field on their conf
> (`ingestion`, `metrics`), the periodic DAG that runs at a given tier fetches
> only the configs whose `schedule_tier` matches the DAG's tier. For singleton-conf
> features (`ontogen`, `metagen`), only the tier listed on the singleton conf
> runs at that tier (the other two tier DAGs short-circuit when triggered).

### DataHub Sync

`POST /internal/admin/datahub/sync` reconciles `dataset_registry.datahub_registered` against
the live DataHub URN set. Accepts an optional `dataset_urns` list in the body
(null/omitted = full sweep). Flips the flag bidirectionally: sets it true when a URN is
found in DataHub, false when it has disappeared. Returns counts
`{checked, flipped_true, flipped_false, unchanged, not_found}`. This endpoint is the
**on-demand / scoped** path (e.g. validation's per-dataset precision check). Scheduled
full-estate reconciliation runs **hourly** as part of the `datahub-sync-hourly` sweep, which
enumerates DataHub once and reconciles `dataset_registry` — inserting newly-seen URNs and
soft-flagging `datahub_registered` true/false — alongside the ingestion source→dataset mapping.
An empty (but successful) enumeration is treated as "no signal" and skips the deregister pass
so a transient zero-result search cannot mass-deregister the registry.

### DAG Verification

`POST /internal/admin/dags/verify` checks that every DAG ID in `ALL_DAG_IDS`
(see [DAG Catalogue](#dag-catalogue)) is registered with the in-cluster Airflow
deployment. Returns `{found, missing, total_expected}`. Used as a post-deploy
smoke check by `dataspoke-test-mode.sh` and by the test fixture
`tests/integration/conftest.py::airflow_client`.

### Workflow Design Conventions

1. **DAGs are Python-defined orchestration** -- each task is a HttpOperator call to an
   internal activity endpoint
2. **Activity endpoints are idempotent** -- safe to retry on transient failures
3. **Timeouts**: Per-task = 5 minutes (default); DAG-level = 1 hour
4. **Retry policy**: Max 3 attempts, 10s initial interval
5. **Concurrency**: `max_active_runs` per DAG prevents overlapping runs

### Concurrency Guards

**Redis SET NX** (for direct-execution flows):

| Flow | Redis Key | TTL |
|------|-----------|-----|
| `ingestion` | `ingestion:running:{dataset_urn}` | 1 hour |
| `ontogen` | `ontogen:running:singleton` | 1 hour |
| `metagen` | `metagen:running:singleton` | 1 hour |

**Airflow DAG run conf-based dedup** (for Airflow-orchestrated DAGs):

| DAG | Conf Key |
|-----|----------|
| `metrics` | `metrics-{metric_id}` |

If a duplicate is detected, the API returns `409 Conflict` with the appropriate `*_RUNNING`
error code (`METAGEN_RUNNING`, `METRIC_RUNNING`, `ONTOGEN_RUNNING`, …).

### Ingestion Workflow

An `ACTIVE_CUSTOM_MANAGED` source supports two trigger modes:

| Mode | Trigger | How |
|------|---------|-----|
| **Periodic** | Airflow schedule | A source's `schedule_tier` (`hourly`, `daily`, `weekly`) is derived from its recipe cron; the corresponding static DAG runs all `ACTIVE_CUSTOM_MANAGED` sources in that tier |
| **Manual** | User HTTP request | `POST .../ingestion/sources/{id}/method/run` calls `IngestionService.run()` directly |

**Static tier-based DAGs**: DataSpoke uses three static Airflow DAGs per domain (hourly,
daily, weekly). Each DAG fetches the source list for its tier at execution time
(`POST /internal/activities/ingestion/list-active`), then uses dynamic task mapping
(`expand()`) to run the extractor for each source in parallel (`max_active_runs`: 5).

> **Scaling assumption**: ingestion activity endpoints execute synchronously inside
> the API process; Airflow is scheduler + fan-out, not worker.
> Combined with LocalExecutor (~1 CPU / 2 Gi), the baseline scales by *smearing across
> tiers* — operators move heavy datasets to `daily`/`weekly` and reserve `hourly` for
> genuinely time-sensitive pipelines. Holds for tens to low-hundreds of datasets with a
> small hourly hot set; "hundreds of datasets all on hourly" needs a follow-up
> (CeleryExecutor / KubernetesExecutor, dispatching via DAG run-conf like metrics, or
> per-source-DB concurrency caps) — none in baseline.

---

## Kafka Consumers *(optional, not enabled in baseline)*

> **Baseline UC1–UC5 do not subscribe to Kafka events.** Cross-feature cadence in the
> baseline is schedule-driven via the Airflow tier DAGs above. The Kafka consumer is
> retained as an extensibility surface for organisations that want to add event-driven
> reactions on top of the baseline; it is shipped disabled and gated by the
> `event-consumer.enabled` Helm toggle (see
> [HELM_CHART §Component Matrix](HELM_CHART.md#component-matrix)).

If enabled, DataSpoke runs a single consumer group (`dataspoke-consumers`) that routes
events by aspect name. Reference implementation: `src/shared/datahub/events.py`
(EventRouter) and `src/shared/datahub/consumer.py`. The reference handler set is
documented in
[DATAHUB_INTEGRATION §Event Subscription](../DATAHUB_INTEGRATION.md#event-subscription-optional-not-used-by-baseline)
— extensions can register their own handlers without modifying baseline code.

The consumer runs as `python -m src.shared.datahub.consumer` in a separate Deployment
(`dataspoke-event-consumer`) when enabled. Uses `confluent-kafka` with manual offset
commit; deserialization failures are logged and skipped, handler failures leave the offset
uncommitted for redelivery.

---

## Dependency Injection

**API route handlers** receive backend services via FastAPI `Depends()` (see
`src/api/dependencies.py`).

**Internal activity endpoints** use factory functions from `src/workflows/_common.py`
(`make_datahub`, `make_redis_client`, `make_db_session`, `make_llm_client`,
`make_pgvector_manager`, `make_notification_service`) instead of FastAPI `Depends()`. This decouples them from the FastAPI DI graph -- the same factories
work in any context (tests, CLI).

Activity endpoints map `DataSpokeError` to `400` (non-retryable) or `500` (retryable) JSON
responses, letting Airflow distinguish between errors worth retrying and permanent
failures.

---

## Error Handling

### Exception-to-HTTP Mapping

| Exception | HTTP Status | Error Code |
|-----------|-------------|------------|
| `EntityNotFoundError` | 404 | `DATASET_NOT_FOUND`, `CONFIG_NOT_FOUND`, `INGESTION_SOURCE_NOT_FOUND`, `METRIC_NOT_FOUND`, `NODE_NOT_FOUND`, `EDGE_NOT_FOUND`, `TRIPLE_NOT_FOUND` |
| `ConflictError` | 409 | `DUPLICATE_CONFIG`, `INGESTION_RUNNING`, `INGESTION_SOURCE_READONLY`, `INGESTION_RUN_NOT_APPLICABLE`, `METAGEN_RUNNING`, `METRIC_RUNNING`, `ONTOGEN_RUNNING`, `METAGEN_DISABLED`, `METRIC_DISABLED`, `ONTOGEN_DISABLED`, `METAGEN_CANNOT_REJECT_APPROVED` |
| `DataHubUnavailableError` | 502 | `DATAHUB_UNAVAILABLE` |
| `StorageUnavailableError` | 503 | `STORAGE_UNAVAILABLE` |
| `ValidationError` (Pydantic) | 422 | `INVALID_PARAMETER`, `INVALID_DATASET_URN` |
| `PreconditionFailedError` | 422 | `DATASET_NOT_IN_DATAHUB`, `ONTOGEN_TRIPLE_DEPENDENCY_PENDING`, `UNKNOWN_VARIABLE`, `INVALID_SCORE` |

Error response format matches [API](../API.md#error-catalogue). Exception hierarchy is
defined in `src/shared/exceptions.py`.

### Best-Effort Operations

Non-critical operations execute best-effort -- if they fail, the primary operation
completes with reduced enrichment. All failures are logged at WARNING with `exc_info=True`.

| Operation | Service | Fallback |
|-----------|---------|----------|
| `assertionRunEvent` emission | ValidationService | Row stays in `validation_results` (local store remains the historical-baseline cache); caller receives `502/503` so the pipeline can decide whether to retry |
| pgvector similarity search | MetagenService | Reviewer proceeds without prior-approved-candidate RAG; debate quality drops but the run completes |
| LLM dataset classification | OntogenService | Dataset excluded from classification |
| DataHub run-history poll | IngestionService (sync sweep) | Skip the affected source for this hourly tick; retry next tick |

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
| `EMBEDDING_DIMENSION` | 1536 | Vector dimension (matches LLM model) |
| `ONTOLOGY_CONFIDENCE_THRESHOLD` | 0.7 | Ontogen: below this -> row persists as `llm_pending` |
| `METAGEN_CONFIDENCE_THRESHOLD` | 0.7 | Metagen: below this -> candidate is dropped (metagen has no `llm_pending`) |

---

## Authentication & User Account Management

The identity, lifecycle, and DataHub-mirror doctrine live in
[AUTH](AUTH.md). The route catalogue and JWT claim shape live in
[API §Authentication & Authorization](../API.md#authentication--authorization).
DataHub-side primitives (corpuser/corpGroup/role aspects, GraphQL mutations,
`hard_delete_entity`) live in
[DATAHUB_INTEGRATION §User & Role Management](../DATAHUB_INTEGRATION.md#user--role-management).
This section captures the service-layer composition only.

### Service Modules (`src/backend/auth/`)

| Module | Responsibility |
|--------|---------------|
| `users.py` | DataSpoke user repository — create / read / update name / update password / hard delete; reads and writes `users.role`. bcrypt via `passlib.hash.bcrypt` at cost factor 12. Google `sub` linking onto existing rows. UNIQUE(email) → `409 EMAIL_ALREADY_REGISTERED`. |
| `tokens.py` | JWT issue / refresh / revoke. Refresh-token revocation list in Redis under `revoked_refresh:{sha256[:16]}`. The JWT carries identity only (`sub`, `email`, `exp`, `iat`); role is **not** in the JWT (read from `users.role` per request). |
| `api_tokens.py` | Long-lived opaque API token CRUD. Mint generates `dsk_<token_urlsafe(32)>`, stores SHA-256 hash in `api_tokens.token_hash`, snapshots `users.role` into `role_snapshot`. Enforces 10-token-per-user cap (`409 TOKEN_LIMIT_EXCEEDED`). On lookup: computes `effective_role = min(role_snapshot, users.role)`; updates `last_used_at` throttled to per-minute granularity. Revoke sets `revoked_at = now()`. |
| `oauth_google.py` | Google OAuth handler via `authlib.integrations.starlette_client`. State cookie (random opaque, HMAC-signed with `DATASPOKE_OAUTH_STATE_SECRET`) + ID-token `nonce` validation. On callback: resolve user by Google `sub`, then by email; create otherwise. |
| `reset.py` | Password-reset token issuance (256-bit `secrets.token_urlsafe`, SHA-256 hashed into `password_reset_tokens`) and confirm. Email transport via `aiosmtplib` driven by the SMTP peripheral (below). |
| `privilege.py` | The `require_role(...)` FastAPI dependency family. Reads caller's role from `users.role` (or `min(role_snapshot, users.role)` for API tokens). Method × tier matrix enforcement per [AUTH §Privilege Model](AUTH.md#privilege-model). |

### DataHub Mirror (`src/backend/datahub/users.py`)

Single module wrapping the SDK + GraphQL primitives catalogued in
[DATAHUB_INTEGRATION §User & Role Management](../DATAHUB_INTEGRATION.md#user--role-management):

- `ensure_corpuser_exists(email, name)` — idempotent `emit_mcp(corpUserInfo)`.
- `ensure_marker_group_exists()` — idempotent `emit_mcp(corpGroupInfo)` using the group name read from `runtime_config.auth_datahub_corp_group`.
- `add_user_to_marker_group(corpuser_urn)` — GraphQL `addGroupMembers`.
- `propagate_role(corpuser_urn, role)` — GraphQL `batchAssignRole`. Called after every DataSpoke-side role write (registration default `Reader`, admin role change). DataHub-side is a mirror; DataSpoke `users.role` is the SSOT.
- `read_role(corpuser_urn)` — SDK `get_aspect(corpuser_urn, RoleMembershipClass)` (atomic single-role per DataHub `RoleService`); the `IsMemberOfRole` GraphQL relationship index is **not** used because it lags MCL→ES indexing. **Used only by the nightly reconciliation DAG**, not on the request hot path.
- `hard_delete_corpuser(corpuser_urn)` — SDK `hard_delete_entity`.

The module never writes `corpUserCredentials`.

### Registration Composition

`POST /auth/register` orchestrates the mirror create sequence
([AUTH §Mirror create sequence](AUTH.md#mirror-create-sequence)). Each step is
idempotent in isolation, so re-running after a DataHub-side failure resumes
correctly:

1. `users.create()` (DataSpoke DB) with `role = 'Reader'`.
2. `datahub.users.ensure_corpuser_exists()`.
3. `datahub.users.ensure_marker_group_exists()` → `add_user_to_marker_group()`.
4. `datahub.users.propagate_role(urn, "Reader")`.
5. `tokens.issue()` → 200 with access JWT + refresh cookie.

Any DataHub-side failure (steps 2–4) triggers compensating hard-delete of the
DataSpoke `users` row and returns `503 DATAHUB_SYNC_FAILED`. Subsequent
registration with the same email is fresh on the DataSpoke side and resumes the
DataHub-side writes idempotently.

The Google OAuth callback uses the same composition when the resolved user is
new (no matching `google_sub`, no matching email).

### Role-Change Composition

`PATCH /admin/users/{id}/role` orchestrates a two-step write where DataSpoke
is SSOT:

1. `users.update_role(user_id, new_role)` — DataSpoke `users.role` updated.
2. `datahub.users.propagate_role(corpuser_urn, new_role)` — DataHub mirror.

If step 2 fails, the API returns `200` to the admin caller (DataSpoke-side
state is correct), logs a warning, and relies on the nightly
`auth-role-sync-daily` DAG to reassert the role on DataHub. No compensating
action on the DataSpoke side — divergence is by definition DataSpoke-correct.

### Privilege Enforcement

The `require_role` dependency family in `src/backend/auth/privilege.py`
implements the [Privilege Model](AUTH.md#privilege-model) matrix:

- `require_authenticated` — JWT decode or API-token lookup; populates
  `request.state.user` and `request.state.effective_role`.
- `require_writer` — used on `/spoke/*` and `/hub/*` write methods (POST /
  PUT / PATCH / DELETE). Rejects with `403 READ_ONLY_ROLE` if
  `effective_role == "Reader"`.
- `require_admin` — used on `/admin/*`. Rejects with `403 FORBIDDEN` if
  `effective_role != "Admin"`.

GET / HEAD / OPTIONS on `/spoke/*` and `/hub/*` use `require_authenticated`
only. `/auth/*` writes use `require_authenticated` only (the method gate is
exempt — self-scoped writes).

The `effective_role` is computed once per request:

- JWT-authenticated request: `SELECT role FROM users WHERE id = sub` (one DB
  round trip, shares the request's DB session).
- API-token-authenticated request: `SELECT t.role_snapshot, u.role FROM
  api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?`,
  then `effective_role = min(t.role_snapshot, u.role)` with ordering
  `Admin > Editor > Reader`. Returns `401 INVALID_API_TOKEN` /
  `401 TOKEN_REVOKED` / `401 TOKEN_EXPIRED` on the token state checks.

The same query updates `last_used_at` when `now - last_used_at > 60s` (or
NULL) — the throttle keeps a high-frequency client from flooding the row
with UPDATEs.

### Deletion Composition

`DELETE /admin/users/{id}` runs the mirror delete sequence:

1. Hard-delete the DataSpoke `users` row (cascade deletes
   `password_reset_tokens`).
2. `datahub.users.hard_delete_corpuser(urn)`.

If step 2 fails, the DataSpoke caller still sees `204`; the orphan corpuser is
operator-cleanable. The order is chosen so the user is immediately unable to
log into DataSpoke even if DataHub is unavailable.

### SMTP Peripheral

Password reset is the only baseline consumer of SMTP. Configuration follows
the existing peripheral pattern (parallel to DataHub and Langfuse):

- Non-secret fields (`host`, `port`, `username`, `from_address`, `use_tls`)
  live in the `peripheral_config` DB table under key `smtp`.
- The `password` field lives in a dedicated K8s Secret
  `dataspoke-smtp-secret` (data key `password`), accessed at runtime via
  RBAC — mirroring `dataspoke-datahub-secret.token` and
  `dataspoke-langfuse-secret.secret_key`. The `PATCH` handler routes the
  `password` field to the Secret, never to the DB.
- Configured at runtime via `PATCH /api/v1/admin/peripherals/smtp` and the
  unattended mirror `/internal/admin/peripherals/smtp`.
- Absence ⇒ `POST /auth/password/reset/request` returns
  `503 PERIPHERAL_NOT_CONFIGURED` with `detail.peripheral = "smtp"`. All
  other auth flows remain functional.

### Settings sourced from chart

| Setting | Source | Purpose |
|---------|--------|---------|
| `DATASPOKE_JWT_SECRET_KEY` | `dataspoke-secrets` | JWT HS256 signing key |
| `DATASPOKE_OAUTH_STATE_SECRET` | `dataspoke-secrets` | HMAC key for the OAuth state cookie |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | `dataspoke-secrets` | Google OAuth client secret |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_ID` | chart values → configmap | Google OAuth client ID (public) |
| `DATASPOKE_COOKIE_SECURE` | chart values → configmap | Refresh-token cookie `Secure` attribute (`false` dev, `true` prod) |

`auth_datahub_corp_group` lives in `runtime_config` (DB-backed) per
[BACKEND_SCHEMA §`runtime_config`](BACKEND_SCHEMA.md#runtime_config). SMTP
credentials live in `peripheral_config` (DB-backed).
