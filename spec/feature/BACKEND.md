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
| `GET /metric/{id}/attr/conf` | `get_metric_conf` |
| `POST /metric/{id}/method/run` | `post_metric_run` |
| `GET /data/{urn}/attr/ingestion/conf` | `get_data_ingestion_conf` |
| `POST /data/{urn}/method/metagen/run` | `post_data_metagen_run` |
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
| LLM | `llm/client.py` | Provider-agnostic client (LangChain). Single completion, JSON completion, embedding, and tool-calling loop (`complete_with_tools`) bound to a service-supplied validator. | Configured via `DATASPOKE_LLM_PROVIDER`, `DATASPOKE_LLM_MODEL` env vars. Tool-calling loop semantics defined in [LLM Inference Loop](#llm-inference-loop). |
| Redis | `cache/client.py` | Async wrapper for caching, rate limiting, pub/sub | -- |
| Notifications | `notifications/service.py` | Outbound notifications (email, in-app alerts). Used by Validation (UC2) and Governance (UC5). | Master toggle `DATASPOKE_NOTIFICATION_ENABLED` (default `false` -- no-ops in dev) |
| Domain Models | `models/` | Shared Pydantic models (`QualityScore`, `EventRecord`, etc.) -- internal domain objects, not API schemas | API schemas live in `src/api/schemas/` |
| Exceptions | `exceptions.py` | `DataSpokeError` hierarchy with error codes for HTTP mapping | See [Error Handling](#error-handling) |
| Settings | `settings.py` | Pydantic `Settings` class reading `DATASPOKE_*` env vars | -- |

### LLM Inference Loop

LLM calls that produce structured business outputs (UC3 ontology generation, UC4 metadata
generation) run inside a bounded ReAct loop, not single-shot completions. The model is
bound to one tool — `{service}_validate(payload)` — and must call it with its proposed
output before the loop accepts a result. The tool enforces semantic rules the JSON schema
cannot express (ID-reference integrity, slug format, in-scope URN provenance, etc.) and
returns `{ok: true}` or `{ok: false, errors: [...]}`. On errors the model receives the
consolidated error list as a `ToolMessage` and revises.

Two enforcement layers stack:

| Layer | Mechanism | Provider wiring |
|-------|-----------|-----------------|
| Shape | LangChain `with_structured_output(OutputModel)` — Pydantic schema bound at the provider layer | OpenAI `response_format=json_schema`, Gemini `response_schema`, Anthropic forced tool-call. Uniform across providers. |
| Semantic rules | LangChain `bind_tools([validator])` — service-supplied validator with full rule table | Same three providers; one tool per service. |

**Common loop parameters**:

| Parameter | Value |
|-----------|-------|
| Max iterations | `3` per service, overridable via `DATASPOKE_ONTOGEN_LLM_MAX_ITERATIONS` / `DATASPOKE_METAGEN_LLM_MAX_ITERATIONS`. One iteration = one model invocation. |
| Exhaustion behavior | Soft. The last candidate is accepted; rows that fail individual rules are dropped before persistence. The run is **not** marked failed on validation exhaustion — UC3/UC4 already gate persistence through a human reviewer. |
| Observability | The run-complete event carries `validation_iterations` (1–`max`) and `validation_errors_dropped` (row count). Per-rule error samples are logged but not surfaced in the synchronous response. |
| Test mode | `StubLLMClient.complete_with_tools` returns one schema-valid empty payload on iteration 1; the loop never iterates. |

Each service ships its validator alongside the service code (`src/backend/{service}/validator.py`).
Rule tables for the two consuming services follow below.

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
field on `GET /spoke/common/data/{urn}` and `GET /spoke/common/dataset` list rows.
The full breakdown (`overall_score`, `dimensions`, optional `dimension_details`) is
returned by the dataset domain via the `QualityScore` model. The score is independent
of the validation feature — datasets without validation configs still have a
`quality_score` derived from aspects alone. The governance metric `validation-score`
(see §Metrics Service) is a separate measurement computed from `validation_results`,
not from aspects.

### Ingestion Service (`src/backend/ingestion/`)

**Covers**: MANIFESTO §2.1 Ingestion Control (UC1). Behavioural narrative — including the
`active-custom` / `passive` split — lives in
[USE_CASE §UC1](../USE_CASE_en.md#uc1-ingestion-control); DataHub aspect reads/writes are
catalogued in [DATAHUB_INTEGRATION §Aspect Reference](../DATAHUB_INTEGRATION.md#aspect-reference).
The DPI emission contract that all ingestors (in-house and external) must satisfy lives
in [DATAHUB_INTEGRATION §Custom Ingestor Guide](../DATAHUB_INTEGRATION.md#custom-ingestor-guide);
DataSpoke-side consumption (event/ingestion mapping, observation fallback) is
[below](#custom-ingestor-authoring-contract). This section describes the implementation only.

**Supported platforms** (in-house extractor module per platform; applies to `active-custom`
mode — `passive` mode is platform-agnostic since DataSpoke does not run the extractor):

| Platform | Status | Locator | Identifier |
|----------|--------|---------|------------|
| `postgres` | Implemented | `host`, `port` | `database`, `schema_name`, `table` |
| `kafka` | Implemented | `bootstrap_servers` | `topic`, `cluster` |
| `mysql`, `oracle`, `bigquery`, `snowflake` | Planned | platform-specific | platform-specific |

**Aspects emitted** (non-dry-run, per discovered dataset, by the `active-custom` extractor):
`StatusClass(removed=False)`, `DatasetPropertiesClass`, `SchemaMetadataClass`, plus
`DataProcessInstance` start + complete `RunEvent` aspects per run (see
[DATAHUB_INTEGRATION §Custom Ingestor Guide](../DATAHUB_INTEGRATION.md#custom-ingestor-guide)).
For postgres, `DatasetProperties.description` is sourced from the PG `obj_description()`
COMMENT and each `SchemaField.description` from `col_description()`; when no COMMENT is
set, the dataset description falls back to `"Ingested by DataSpoke: {database}.{schema}.{table}"`.
`dry_run: true` runs the extractor and returns the schema preview without emitting any aspects.

#### Implementation

CRUD for ingestion configurations (PostgreSQL: `ingestion_configs`). Config upsert registers
the dataset URN in `dataset_registry` (does not require the dataset to exist in DataHub yet).

Ingestion config model: see
[`BACKEND_SCHEMA §ingestion_configs`](BACKEND_SCHEMA.md#ingestion_configs). Key fields:
`dataset_urn` (unique per dataset), `mode` (`active-custom` | `passive`), `platform`
(`postgres`, `kafka` implemented for `active-custom`; any platform allowed for `passive`),
`locator`/`identifier`/`auth` (JSONB connection details; `locator`/`auth` are
`active-custom`-only), `is_enabled`/`schedule_tier` (`schedule_tier` is `active-custom`-only),
`status` (DAG verification outcome).

**`workflow_dag_id` derivation**: for `mode='active-custom'` configs with a valid
`schedule_tier` (`hourly` / `daily` / `weekly`), `workflow_dag_id` is set to
`ingestion-active-{schedule_tier}` on every upsert/PATCH so the periodic tier DAG can
fetch its dataset list deterministically. `passive` mode and missing/invalid tiers leave
it `null`.

**Mode is mutable post-creation** via PATCH. Switching `active-custom` → `passive` is
allowed and takes effect on the next periodic tier sweep; previously-scheduled
`active-custom` runs are not cancelled retroactively but no new ones are scheduled.
Switching `passive` → `active-custom` requires the `active-custom`-only fields
(`schedule_tier`, `locator`, `auth`) to be populated. `method/run` is rejected
(`409 INGESTION_NOT_APPLICABLE`) for `passive` configs because passive ingestion is
run externally; for `active-custom` configs with `is_enabled=false` it is rejected
(`409 INGESTION_DISABLED`) unless `dry_run=true`.

**Auth resolution** (`active-custom` only — passive ingestors handle their own auth
out-of-band): the `auth` field carries a structured `secret_ref: {name, key}` that
points at a Kubernetes Secret in DataSpoke's own namespace. On PUT, callers either supply
`password` (vault path: API writes the Secret then persists only the reference) or omit
`password` (reference path: API verifies a pre-existing Secret). Plaintext passwords are
never persisted in `ingestion_configs.auth`. Validation matrix, vault/verify/resolve
flows, RBAC, and error taxonomy live in [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md). At
run time the extractor calls the resolver; failures surface as `IngestionResult(errors=[…])`
→ `status="error"`.

**Active-custom run pipeline** (`IngestionService.run()`): load config → connect to source via
`locator`/`auth` → emit `DataProcessInstanceRunEvent(STARTED)` against a deterministic DPI
URN derived from `run_id` (skipped on `dry_run`; see
[DATAHUB_INTEGRATION §Custom Ingestor Guide](../DATAHUB_INTEGRATION.md#custom-ingestor-guide)) → discover schema
via `identifier` → emit `StatusClass` + `DatasetPropertiesClass` + `SchemaMetadataClass` to
DataHub (skipped on `dry_run`; a non-dry-run that ingests zero entities is treated as
failure) → emit `DataProcessInstanceRunEvent(COMPLETE | FAILED)` carrying the run outcome
(skipped on `dry_run`) → on success mark `dataset_registry.datahub_registered = true` via
`mark_registered()` in `src/shared/db/registry.py` (skipped on `dry_run`) → record
`INGESTION.COMPLETE` / `INGESTION.FAIL` event (recorded for both dry-run and non-dry-run;
the run's `dry_run` boolean is preserved in the event's `detail` payload so downstream
readers can distinguish them; see [Event Catalogue](#event-catalogue)).

**Passive status-sync pipeline** (`IngestionService.sync_passive_status()`,
called hourly by the `ingestion-passive-hourly` DAG): enumerate all configs with
`mode = passive` AND `is_enabled = true` → for each, query DataHub on **two surfaces**
and merge results:

1. **`DataProcessInstance` runs** via the `dataset(urn).runs` GraphQL field — picks up
   any DPI emitter that follows the [DATAHUB_INTEGRATION §Custom Ingestor Guide](../DATAHUB_INTEGRATION.md#custom-ingestor-guide)
   (DataSpoke's own `active-custom` extractors, custom acryl-datahub-SDK scripts,
   third-party pipelines). Each terminal `RunEvent` becomes one event row.
2. **`Operation` time-series aspects** with `operationType ∈ {INSERT, UPDATE, CREATE, ALTER}`
   — covers DataHub Managed Ingestion's standard source plugins, which emit `Operation`
   per run rather than DPI. Each ingestion-like Operation becomes one
   `INGESTION.COMPLETE` event. `DELETE`/`DROP`/`UNKNOWN` are excluded — they don't
   represent ingestion of new metadata.

Both surfaces feed the same insert path: rows in the unified `events` table with
`event_type = INGESTION.COMPLETE` / `INGESTION.FAIL` (mirroring the `active-custom`
path's event shape so clients see a uniform stream), deduplicated by
`(entity_id, event_type, occurred_at)`. Passive configs with `is_enabled=false` are
skipped. No aspects are emitted by DataSpoke; the registry's `datahub_registered`
flag is reconciled by the existing `datahub-sync-daily` DAG.

### Custom Ingestor Authoring Contract

The generic authoring contract — required aspects, ordering, failure semantics, URN
convention, `systemMetadata` requirements, and the authoring checklist — lives in
[DATAHUB_INTEGRATION §Custom Ingestor Guide](../DATAHUB_INTEGRATION.md#custom-ingestor-guide).
That guide is generic across all DataHub-targeting ingestors. This section covers
DataSpoke-side **consumption** only.

**DataSpoke's role**: the `ingestion-passive-hourly` DAG queries `dataset(urn).runs`
GraphQL hourly. Each terminal `RunEvent` becomes one row in the dataset's local
`event/ingestion` timeline — `result.resultType = SUCCESS` maps to
`INGESTION.COMPLETE`, `FAILURE` maps to `INGESTION.FAIL`. A run that emits STARTED
but never terminal is treated as in-flight (never surfaced) until a terminal event
arrives or a human cleans it up via DataHub's UI.

**Observation fallback (Managed Ingestion)**: DataSpoke's poll *also* surfaces
ingestion-like `Operation` aspects (`operationType ∈ {INSERT, UPDATE, CREATE,
ALTER}`) as `INGESTION.COMPLETE` events. This covers ingestors that emit `Operation`
but no DPI — most notably DataHub Managed Ingestion's standard source plugins. It
does not relax the DPI contract; authors targeting full DataSpoke parity (terminal
status, run identity, lineage to the producing job) must still emit DPI.

**DataSpoke's own conventions** (one example of how an in-house ingestor populates
the generic contract):

- `runId = "dataspoke-{platform}-{run_id}"`, with `run_id = uuid4()` per
  `IngestionService._run_inner` invocation.
- DPI URN = `urn:li:dataProcessInstance:{platform}-{run_id}`, matching the
  `runId` suffix so dataset aspects and the DPI cross-reference cleanly.

**Reference implementation**: `src/backend/ingestion/service.py::_run_inner` and
`src/backend/ingestion/extractors.py`. Authors building a new in-house extractor or
external script should mirror the same emit sequence using the `acryl-datahub`
Python SDK.

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

**Covers**: MANIFESTO §2.1 Metadata Generation (UC4)

CRUD for per-dataset metadata generation configs (PostgreSQL: `metagen_configs`).
LLM-powered proposals for documentation fields that already exist in DataHub. **Writes
only to editable DataHub aspects** (see
[DATAHUB_INTEGRATION §Editable vs Non-Editable Description Aspects](../DATAHUB_INTEGRATION.md#editable-vs-non-editable-description-aspects)).
Approval is field-level — reviewers may approve a subset of proposed fields in a single
PATCH.

**`targets` enum** on `attr/metagen/conf`. Three values are supported in baseline:

| Value | Scope | DataHub write target |
|-------|-------|----------------------|
| `dataset.description` | Per-data | `editableDatasetProperties.description` |
| `column.description` | Per-data, one entry per column | `editableSchemaMetadata.editableSchemaFieldInfo[].description` keyed by `fieldPath` |
| `cross_data.md` | Cross-data | `documentInfo.contents.text` (Markdown) on `document` entities whose `relatedAssets` list the involved dataset URNs — the proposal carries a list of actions (see below) |

Future scope: proposals for `domains` and `globalTags`.

**Generation Pipeline** (Airflow DAG): read DataHub evidence — `datasetProperties`,
`schemaMetadata`, `editableDatasetProperties`, `editableSchemaMetadata`,
`glossaryTerms`, and `documentInfo.contents.text` on `document` entities whose
`relatedAssets` overlap the in-scope dataset — plus UC3-approved nodes and triples
filtered by `dataset_node_map.status='approved'` → LLM analysis to draft per-field
proposals for the configured `targets` → for `cross_data.md`, decide create / modify /
delete actions over the candidate `document` entities → produce a `metagen_results`
row in PostgreSQL with status `pending_review`.

The LLM step in the Generation Pipeline runs inside the
[LLM Inference Loop](#llm-inference-loop). The bound tool is `metagen_validate(payload)`;
the schema model is `MetagenLLMOutput` (per-target proposal entries).

**Metagen validator rules**:

| Rule | Failure code |
|------|--------------|
| Pydantic shape of `MetagenLLMOutput` | `SCHEMA` |
| Every proposal entry's `target` ∈ the config's configured `targets` | `TARGET_NOT_CONFIGURED` |
| For `column.description`: `field_path` resolves to a real column in the dataset's `schemaMetadata` | `UNKNOWN_FIELD_PATH` |
| For `cross_data.md`: `action_type ∈ {create, modify, delete}` | `INVALID_ACTION_TYPE` |
| For `cross_data.md` `modify` / `delete` actions: `document_urn` references an existing document with `relatedAssets` overlapping the in-scope dataset set | `UNKNOWN_DOCUMENT_URN` |
| For `cross_data.md` `create` / `modify` actions: `related_dataset_urns` is non-empty and every entry ∈ in-scope dataset URNs | `OUT_OF_SCOPE_URN` |
| For `cross_data.md` `create` / `modify`: Markdown body is non-empty | `EMPTY_BODY` |
| `action_id` (cross_data.md only) matches `^[a-z0-9][a-z0-9_-]*$`; no `__` | `SLUG_FORMAT` / `DOUBLE_UNDERSCORE` |
| No duplicate `action_id` within a single proposal | `DUP_ACTION_ID` |
| For `dataset.description` / `column.description`: proposed text is non-empty | `EMPTY_DESCRIPTION` |

Metagen adopts the loop after Ontogen lands; until then, the existing single-call
implementation persists and the validator rules above describe the target state.

**Cross-data MD action types**. A single `cross_data.md` proposal carries an ordered list
of actions, each independently approvable:

| Action | Effect on DataHub |
|--------|-------------------|
| `create` | New `document` with a generator-chosen descriptive title (topic phrase), Markdown body in `documentInfo.contents.text`, and `relatedAssets` listing the involved dataset URNs (at least one — a cross-data document with no related datasets has no purpose). `documentInfo.source.sourceType = NATIVE`. |
| `modify` | Replace `documentInfo.contents.text` on an existing `document`; URN and title preserved. `relatedAssets` may be extended when the topic now covers additional datasets. |
| `delete` | Soft-delete an existing `document` via `Status.removed = true` when its topic is fully absorbed into a new replacement. No hard delete. |

**Approval flow**. `PATCH /attr/metagen/result/{result_id}` with
`{verdict, fields, reason}`:

- `verdict: "approve"` + `fields` omitted → approve all proposed fields and actions.
- `verdict: "approve"` + `fields: [...]` → approve only the listed field paths and / or
  cross-data actions referenced as `cross_data.md.<action_id>`.
- `verdict: "reject"` → reject the whole proposal (or the listed `fields` only).

On approval, the service writes the approved subset to the editable DataHub aspects in a
single `emit_mcp` per affected entity. Each successful write emits a `METAGEN.APPROVE`
event; rejections emit `METAGEN.REJECT` (see [Event Catalogue](#event-catalogue)).

**Disabled-config rejection**: `method/run` with `is_enabled=false` and `dry_run=false`
raises `409 GENERATION_DISABLED`. Dry-run is permitted regardless of `is_enabled`.

### Ontology Generation Service (`src/backend/ontogen/`)

**Covers**: MANIFESTO §2.1 Ontology Generation (UC3). Consumed by Metadata Generation
(UC4) and Governance (UC5 — blind-spot detection).

Singleton-config LLM pipeline that emits a **subject / predicate / object triple
ontology** — nodes (subjects / objects), edges (predicates), and triples
(`(subject_node, edge, object_node)` facts). Storage backed by PostgreSQL relational
tables + pgvector embeddings. Independent review workflow (approve / reject) per
result type, with triple review gated on its endpoint nodes and edge being approved.

**Singleton conf** at `/spoke/common/ontogen/attr/conf` — there is no per-dataset ontology
config. Fields:

| Field | Purpose |
|-------|---------|
| `is_enabled` | Master switch for the inference DAG. |
| `schedule_tier` | `hourly` / `daily` / `weekly` re-inference cadence. |
| `dataset_filter` | Optional scope filter — `tags` (DataHub tag URNs), `glossary_terms` (DataHub glossary term URNs), and `dataset_urns` (explicit `urn:li:dataset:(…)` URN list). OR-ed across dimensions; `{}` means all. URNs validated at PUT/PATCH (`422 INVALID_DATASET_URN`); unresolved-at-runtime entries are skipped and reported in the run-complete event's `unresolved_urns`. Same shape as UC5's `measurement_query.dataset_filter`. |
| `default_run_prompt` | Optional Markdown string used as the one-shot prompt for runs without an explicit body — i.e., the periodic Airflow DAG and manual `POST /method/run` calls with no body. Null disables the default. |

UC3 inputs are sourced entirely from DataHub-resident metadata (the proofread
boundary shared with UC4).

The conf is a single row in `ontogen_config` (singleton table; see
[BACKEND_SCHEMA §ontogen_config](BACKEND_SCHEMA.md#ontogen_config)).

**Seeds** at `/spoke/common/ontogen/attr/seed/{seed_id}` are human-authored Markdown
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
   `dataset_urns`. Listed URNs that don't resolve are skipped and accumulated for
   the run-complete event's `unresolved_urns`. `{}` means all datasets.
3. Fetch DataHub evidence per in-scope dataset (the proofread boundary shared
   with UC4): `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
   `editableSchemaMetadata`, `glossaryTerms`, and `documentInfo.contents.text` on
   `document` entities whose `relatedAssets` reference an in-scope dataset
   (Markdown body, capped per dataset). DataSpoke writes the editable aspects
   only after a UC4 reviewer approves the proposal (UC4 `field_status='approved'`);
   draft states (`pending` / `edited`) stay in `metagen_results.proposals`, so
   *presence* in DataHub is the approval signal — UC3 reads DataHub directly with
   no JOIN against `metagen_results`.
4. Load active seeds (`ontogen_seeds.status='active'`). Resolve the one-shot prompt:
   if the `POST /method/run` request carries a non-empty `text/markdown` body, use
   that body; otherwise fall back to `ontogen_config.default_run_prompt` (used by both
   the periodic Airflow DAG and bodyless manual calls). The one-shot prompt is
   appended after the seeds and is not stored.
5. LLM proposes nodes per dataset. For each candidate, look up the closest approved
   node via `node_embeddings` (cosine similarity, threshold
   `ONTOLOGY_NODE_REUSE_THRESHOLD`); if a match exists, reuse the approved node ID,
   otherwise emit a new pending node.
6. LLM proposes the edge (predicate) vocabulary, again preferring reuse of approved
   `ontogen_edges` rows.
7. LLM composes triples referencing already-proposed nodes and edges (mix of approved
   and pending). Reuse existing approved triples when subject / edge / object match.
8. Score confidence per node, edge, and triple (below
   `ONTOLOGY_CONFIDENCE_THRESHOLD` queued for human review).
9. Persist new / updated rows to PostgreSQL (relational + pgvector); refresh
   `node_embeddings` for any node whose name or description changed.

Steps 5–7 are executed as a single LLM call wrapped in the
[LLM Inference Loop](#llm-inference-loop). The bound tool is `ontogen_validate(payload)`;
the schema model is `OntogenLLMOutput` (nodes / edges / triples arrays).

**Ontogen validator rules**:

| Rule | Failure code |
|------|--------------|
| Pydantic shape of `OntogenLLMOutput` | `SCHEMA` |
| `node.id` and `edge.id` match `^[a-z0-9][a-z0-9_-]*$`; no `__` | `SLUG_FORMAT` / `DOUBLE_UNDERSCORE` |
| No duplicate ids within `nodes`, within `edges` | `DUP_ID` |
| Every `triple.subject_node_id` and `triple.object_node_id` resolves to a node in the payload | `UNKNOWN_NODE_REF` |
| Every `triple.edge_id` resolves to an edge in the payload | `UNKNOWN_EDGE_REF` |
| `confidence_score ∈ [0.0, 1.0]` on every node, edge, triple | `CONF_OUT_OF_RANGE` |
| `node.dataset_urns` is non-empty and every entry ∈ in-scope dataset URNs supplied as evidence | `MISSING_DATASET_URNS` / `OUT_OF_SCOPE_URN` |
| No duplicate `(subject_node_id, edge_id, object_node_id)` triples | `DUP_TRIPLE` |

The in-scope dataset URN set is the keys of `evidence_per_dataset` for the current run —
fabricated URNs (model invented a dataset the prompt never mentioned) are rejected.

Concurrent inference runs return `409 ONTOGEN_RUNNING`; `?dry_run=true` evaluates
steps 2–8 without persisting.

**Disabled-config rejection**: `method/run` with `is_enabled=false` and `dry_run=false`
raises `409 ONTOGEN_DISABLED`. Dry-run is permitted regardless of `is_enabled`.

**Approval flow**. Each result type uses `POST /spoke/common/ontogen/result/{node|edge|triple}/{id}/method/review`
with `{verdict, reason}`. Approval flips `status` in DataSpoke storage; the ontology
graph lives entirely in DataSpoke (relational + pgvector).

- **Node** `verdict: "approve"` → mark the node and its `dataset_node_map`
  memberships as approved.
- **Edge** `verdict: "approve"` → mark the edge (predicate vocabulary entry) as
  approved.
- **Triple** `verdict: "approve"` → requires both endpoint nodes and the edge to be
  already approved (otherwise `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`); on success,
  mark the triple as approved.
- `verdict: "reject"` → mark the result as rejected. Rejecting a node or edge does
  not auto-reject dependent triples — those simply remain stuck on
  `ONTOGEN_TRIPLE_DEPENDENCY_PENDING` until reinference produces a different
  proposal.

Each verdict emits a `NODE.APPROVE` / `NODE.REJECT` / `EDGE.APPROVE` / `EDGE.REJECT` /
`TRIPLE.APPROVE` / `TRIPLE.REJECT` event.

### Metrics Service (`src/backend/metrics/`)

**Covers**: MANIFESTO §2.1 Governance (UC5) — metric definition and aggregation. Baseline
metrics (`ingestion-freshness`, `validation-score`), `dataset_filter` semantics, and
`unresolved_urns` reporting live in [USE_CASE §UC5](../USE_CASE_en.md#uc5-governance);
DataHub aspect reads in
[DATAHUB_INTEGRATION §Aspect Usage by Feature](../DATAHUB_INTEGRATION.md#aspect-usage-by-feature).

**Pure aggregation**: metrics never probe source data — they aggregate pre-existing
DataHub aspects and DataSpoke result tables. The metrics layer therefore has no source
credentials and no SQL access to production databases. New metric types are added by
implementing a measurement function on top of those existing surfaces; unsupported
aggregations return `422 INVALID_PARAMETER`.

#### Implementation

Metric definition CRUD (PostgreSQL: `metric_definitions`). Scheduled or on-demand
measurement execution. Activate/deactivate metric scheduling. No alarm evaluation, no issue
tracking, no notification dispatch.

**`dataset_filter`**: Optional filter in `measurement_query` with `tags` (list of DataHub
tag URNs), `glossary_terms` (list of DataHub glossary term URNs), and `dataset_urns` (list
of explicit `urn:li:dataset:(…)` URNs for pinning to a known set). When specified, only
datasets matching ANY listed tag, glossary term, or explicit URN are included in the
measurement — filters are OR-ed across all three dimensions; an empty array on any
dimension contributes nothing; `{}` means all datasets. URN format is validated at
PUT/PATCH (`422 INVALID_DATASET_URN`); entries that don't resolve in DataHub at run time
are skipped and reported in the `METRIC.RUN_COMPLETE` event's `unresolved_urns` field.

**Breakdown format**: Every measurement result includes a `breakdown` JSONB with a unified
per-dataset entry shape:

```
{"dataset_count": <total scanned>, "datasets": [{"urn": "...", "category": "<classification>", "detail": {...}}]}
```

`category` is a machine-readable classification (e.g. `fresh`, `stale`,
`passing`, `failing`). `detail` is optional, type-specific metadata
(e.g. `{"last_event_at": "..."}` for ingestion-freshness,
`{"latest_data_time": "...", "score": 1.0}` for validation-score). Time-range queries on
`attr/result` use the breakdown to answer per-dataset historical questions without
re-running the metric.

**Disabled-config rejection**: `method/run` with `is_enabled=false` and `dry_run=false`
raises `409 METRIC_DISABLED`. This check is enforced both in `MetricsService._run_inner()`
and at the route layer in `post_metric_run()` (which bypasses `MetricsService.run()` to
call Airflow directly). Dry-run is permitted regardless of `is_enabled`.

### Overview Service (`src/backend/overview/`)

**Covers**: MANIFESTO §2.1 Governance (UC5) — single multi-perspective overview that
returns the latest value of every enabled metric, a per-dataset breakdown, and **blind
spots** (datasets present in DataHub that are not mapped to any UC3 ontology node).
Read-only aggregation over DataHub aspects, validation results, and the ontology.

`GET /spoke/dg/overview` composes:

| Section | Source |
|---------|--------|
| Metric values | Latest `metric_results.value` per enabled metric |
| Per-dataset breakdown | Aggregation of latest `metric_results.breakdown` rows |
| Blind spots | Datasets in DataHub with no row in `dataset_node_map` (or `status != approved`) |
| Ontology graph | `ontogen_nodes` + `ontogen_triples` (with `ontogen_edges` resolving the predicate label) |
| Medallion layers | Bronze = 0 upstreams, Silver = 1–2, Gold = 3+, derived from `upstreamLineage` |
| Ownership topology | DataHub `ownership` aspect grouped by owner / team |

`GET / PATCH /spoke/dg/overview/attr` reads / updates the visualization config singleton
(`overview_config`).

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
by every domain that owns a config — `INGESTION`, `VALIDATION`, `METAGEN`, `METRIC`,
`ONTOGEN` (singleton). Domain-specific actions:

| Domain (`entity_type`) | Action | Trigger |
|---|---|---|
| `INGESTION` (`dataset`) | `COMPLETE` / `FAIL` | `POST method/ingestion/run` succeeds / errors |
| `VALIDATION` (`dataset`) | `RESULT_RECORDED` | `POST attr/validation/result` succeeds (one event per accepted result) |
| `METAGEN` (`dataset`) | `COMPLETE` | `POST method/metagen/run` succeeds; recorded for both dry-run and non-dry-run, `dry_run` flag in detail. Detail keys: `run_id`, `result_id` (UUID on real-run, `null` on dry-run), `targets` (list mirroring the conf's `targets`), `dry_run` |
| `METAGEN` (`dataset`) | `APPROVE` / `REJECT` | `PATCH attr/metagen/result/{id}` with `verdict: "approve"\|"reject"` |
| `METRIC` (`metric`) | `RUN_COMPLETE` | `POST method/run` succeeds; payload carries `unresolved_urns` for any `dataset_filter.dataset_urns` entries that didn't resolve in DataHub |
| `ONTOGEN` (`ontogen`) | `SEED_CREATE` / `SEED_UPDATE` / `SEED_DELETE` | seed CRUD on `attr/seed/{seed_id}` |
| `ONTOGEN` (`ontogen`) | `RUN_COMPLETE` / `RUN_FAILED` | re-inference run end; `RUN_COMPLETE` recorded for both dry-run and non-dry-run, `dry_run` flag in detail. Detail keys: `unresolved_urns` (list, same shape as METRIC), `counts` (dict — `nodes_added/edges_added/triples_added` on real-run, `nodes_proposed/edges_proposed/triples_proposed` on dry-run), `dry_run` |
| `NODE` / `EDGE` / `TRIPLE` (`node` / `edge` / `triple`) | `APPROVE` / `REJECT` | `POST ontogen/result/{type}/{id}/method/review` |

### Querying Events

- **Entity-level endpoint** (`GET .../data/{urn}/event`): returns all events
  for the entity regardless of domain — filters only by `entity_type` +
  `entity_id`.
- **Domain-level endpoint** (`GET .../event/ingestion`): additionally
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
| `ingestion-passive-hourly` | `ingestion_passive_hourly.py` | Airflow schedule | `@hourly` |
| `metrics-hourly` | `metrics_hourly.py` | Airflow schedule | `@hourly` |
| `metrics-daily` | `metrics_daily.py` | Airflow schedule | `@daily` |
| `metrics-weekly` | `metrics_weekly.py` | Airflow schedule | `@weekly` |
| `metagen-hourly` | `metagen_hourly.py` | Airflow schedule | `@hourly` |
| `metagen-daily` | `metagen_daily.py` | Airflow schedule | `@daily` |
| `metagen-weekly` | `metagen_weekly.py` | Airflow schedule | `@weekly` |
| `metagen` | `metagen.py` | API | On-demand |
| `metrics` | `metrics.py` | API | On-demand |
| `ontogen-hourly` | `ontogen_hourly.py` | Airflow schedule | `@hourly` |
| `ontogen-daily` | `ontogen_daily.py` | Airflow schedule | `@daily` |
| `ontogen-weekly` | `ontogen_weekly.py` | Airflow schedule | `@weekly` |
| `ontogen` | `ontogen.py` | API | On-demand |
| `datahub-sync-daily` | `datahub_sync_daily.py` | Airflow schedule | `@daily` |

> **Tier-DAG selection**: For features with a `schedule_tier` field on their conf
> (`ingestion`, `metrics`, `metagen`), the periodic DAG that runs at a given tier
> fetches only the configs whose `schedule_tier` matches the DAG's tier. For
> `ontogen`, only the tier listed on the singleton conf runs at that tier (the other
> two tier DAGs short-circuit when triggered).

### DataHub Sync

`POST /internal/admin/datahub/sync` reconciles `dataset_registry.datahub_registered` against
the live DataHub URN set. Accepts an optional `dataset_urns` list in the body
(null/omitted = full sweep). Flips the flag bidirectionally: sets it true when a URN is
found in DataHub, false when it has disappeared. Returns counts
`{checked, flipped_true, flipped_false, unchanged, not_found}`. The `datahub-sync-daily`
DAG calls this endpoint daily (unparameterized, full sweep).

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

**Airflow DAG run conf-based dedup** (for Airflow-orchestrated DAGs):

| DAG | Conf Key |
|-----|----------|
| `metagen` | `metagen-{md5(urn)[:12]}` |
| `metrics` | `metrics-{metric_id}` |
| `ontogen` | `ontogen-singleton` |

If a duplicate is detected, the API returns `409 Conflict` with the appropriate `*_RUNNING`
error code (`GENERATION_RUNNING`, `METRIC_RUNNING`, `ONTOGEN_RUNNING`, …).

### Ingestion Workflow

Ingestion supports two trigger modes per dataset:

| Mode | Trigger | How |
|------|---------|-----|
| **Periodic** | Airflow schedule | Datasets are assigned to a schedule tier (`hourly`, `daily`, `weekly`); the corresponding static DAG runs all configs in that tier |
| **Manual** | User HTTP request | `POST .../method/ingestion/run` calls `IngestionService.run()` directly |

**Static tier-based DAGs**: DataSpoke uses three static Airflow DAGs per domain (hourly,
daily, weekly). Each DAG fetches the dataset list for its tier at execution time
(`POST /internal/activities/ingestion/list-active`), then uses dynamic task mapping
(`expand()`) to run ingestion for each dataset in parallel (`max_active_runs`: 5).

> **Scaling assumption**: ingestion activity endpoints execute synchronously inside
> the API process; Airflow is scheduler + fan-out, not worker.
> Combined with LocalExecutor (~1 CPU / 2 Gi), the baseline scales by *smearing across
> tiers* — operators move heavy datasets to `daily`/`weekly` and reserve `hourly` for
> genuinely time-sensitive pipelines. Holds for tens to low-hundreds of datasets with a
> small hourly hot set; "hundreds of datasets all on hourly" needs a follow-up
> (CeleryExecutor / KubernetesExecutor, dispatching via DAG run-conf like metagen, or
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
(`make_datahub`, `make_cache`, `make_db_session`, `make_llm`, `make_vector`) instead of
FastAPI `Depends()`. This decouples them from the FastAPI DI graph -- the same factories
work in any context (tests, CLI).

Activity endpoints map `DataSpokeError` to `400` (non-retryable) or `500` (retryable) JSON
responses, letting Airflow distinguish between errors worth retrying and permanent
failures.

---

## Error Handling

### Exception-to-HTTP Mapping

| Exception | HTTP Status | Error Code |
|-----------|-------------|------------|
| `EntityNotFoundError` | 404 | `DATASET_NOT_FOUND`, `CONFIG_NOT_FOUND`, `METRIC_NOT_FOUND`, `NODE_NOT_FOUND`, `EDGE_NOT_FOUND`, `TRIPLE_NOT_FOUND` |
| `ConflictError` | 409 | `DUPLICATE_CONFIG`, `INGESTION_RUNNING`, `GENERATION_RUNNING`, `METRIC_RUNNING`, `ONTOGEN_RUNNING`, `INGESTION_DISABLED`, `GENERATION_DISABLED`, `METRIC_DISABLED`, `ONTOGEN_DISABLED` |
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
| pgvector similarity search | MetagenService | No alternative suggestions |
| LLM dataset classification | OntogenService | Dataset excluded from classification |
| LLM cross-data MD synthesis | MetagenService | `cross_data.md` action list empty for the run |
| DataHub run-history poll | IngestionService (passive sync) | Skip the affected dataset for this hourly tick; retry next tick |

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
| `ONTOLOGY_CONFIDENCE_THRESHOLD` | 0.7 | Below this -> pending human review |

---

## Authentication & User Account Management

### Current (stub)

DataSpoke uses JWT for stateless authentication; the route surface, claim
shape, and group-to-route enforcement are defined in
[API §Authentication & Authorization](../API.md#authentication--authorization).

**Auth service** (`src/backend/auth/`):

- `POST /auth/token` — verify credentials against the stub identity store and
  issue an access token (15 min) plus a refresh token (7 d). The access token is
  returned in the response body; the refresh token is set as an HttpOnly cookie.
- `POST /auth/token/refresh` — verify the refresh-cookie JWT, check it is **not**
  in the Redis revocation list, and issue a new access token. Fails closed with
  `503 STORAGE_UNAVAILABLE` when Redis is unreachable.
- `POST /auth/token/revoke` — record the refresh token in Redis under
  `revoked_refresh:{sha256[:16]}` with TTL equal to the token's remaining lifetime.

**Stub identity store**: a single admin account configured via
`DATASPOKE_ADMIN_EMAIL` / `DATASPOKE_ADMIN_PASSWORD`. All other credentials are
rejected. The admin record carries every group claim (`admin`, `de`, `da`, `dg`).
Cookie `secure` flag is gated by `DATASPOKE_COOKIE_SECURE` (default `false` for
dev; production deployments must set it to `true`).

All stub code is marked with `TBD(user-accounts)` comments.

### Planned Components

- **User identity store**: PostgreSQL `users` table or external IdP integration (LDAP, OIDC)
- **Password hashing**: bcrypt via `passlib`
- **Group membership management**: Admin routes under `/admin/...`
- **Account information transfer**: Map DataSpoke users to DataHub owner URNs
- **Cookie `secure` flag**: Tied to `DATASPOKE_COOKIE_SECURE` env setting
