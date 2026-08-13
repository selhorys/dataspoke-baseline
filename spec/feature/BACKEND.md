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
8. [Health reporting](#health-reporting)
9. [Dependency Injection](#dependency-injection)
10. [Error Handling](#error-handling)
11. [Configuration](#configuration)
12. [Authentication & User Account Management](#authentication--user-account-management)

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
| `GET /spoke/governance/metric/{id}/attr/conf` | `get_metric_conf` |
| `POST /spoke/governance/metric/{id}/method/run` | `post_metric_run` |
| `GET /spoke/ingestion/sources/{id}/attr/conf` | `get_ingestion_source_conf` |
| `POST /spoke/metagen/conf/{conf_id}/method/run` | `post_metagen_conf_run` |
| `POST /spoke/common/data/{dataset_urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` | `post_data_metagen_item_candidate_review` |
| `POST /spoke/ontogen/result/node/{node_id}/method/review` | `post_ontogen_node_review` |
| `POST /spoke/ontogen/result/edge/{edge_id}/method/review` | `post_ontogen_edge_review` |
| `POST /spoke/ontogen/result/triple/{triple_id}/method/review` | `post_ontogen_triple_review` |

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
| PostgreSQL | `db/session.py`, `db/models.py` | SQLAlchemy 2.0 async with `asyncpg`. Session factory + ORM models. | Pool size 10, max overflow 5. Credentials are carried as `sqlalchemy.URL` fields rather than interpolated into a DSN string, so `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim from this connection layer whatever characters they contain, and the URL's string form masks the password rather than carrying it into a log line or traceback. A write that must commit on its own terms while the caller holds a session of its own — one that has to survive a rollback the caller is about to take, or land on a read-only request that never commits — opens a session from a factory built on the **bind of the injected session**, so it reaches the database the caller is actually using. The module-level factory is bound at import time to the app-runtime `DATASPOKE_POSTGRES_*` values, which an in-process caller carrying a session on another engine does not have; the write would otherwise be aimed at a different address than every other statement in the same call, with no diagnostic distinguishing that from success. A session with no usable bind falls back to the module-level factory, the only address available in that case, and the helper is total -- it never propagates. A bind that is absent (the attribute is simply not set), `None`, or not an async engine falls back silently -- those are shapes the caller can see in what it injected. A bind whose read fails for any other reason is logged at WARNING with the exception, because that shape is invisible to the caller. The unset case is recognised by the `AttributeError` the read raises, so an `AttributeError` raised from *inside* a `bind` property is indistinguishable from an unset attribute and is silent too. |
| Vector (pgvector) | `vector/client.py` | Table-backed vector upsert/search (cosine, HNSW-indexed). Shares the PostgreSQL session factory. | `PgVectorManager` + `VectorHit` dataclass; collection name whitelisted against `EMBEDDING_COLLECTION`. |
| Graph (Apache AGE, reserved) | `graph/client.py` | AGE extension installed on the same PG instance for future graph-shaped queries. `AgeGraph` exposes `materialize_triple` / `delete_triple` / `traverse` helpers usable by any service that opts in. | See [BACKEND_SCHEMA §Graph](BACKEND_SCHEMA.md#graph-apache-age-reserved). |
| LLM | `llm/client.py` | Provider-agnostic client (LangChain). Single completion, JSON completion, embedding, and tool-calling loop (`complete_with_tools`) bound to a service-supplied validator. | Provider/model from the `llm_provider`/`llm_model` runtime config (`/api/v1/admin/conf`); the API key is read at runtime from the `dataspoke-llm-secret` Secret and rotated online via the same conf surface. Loop semantics, validator rule tables, debate framework, and test-mode toggles defined in [BACKEND_LLM](BACKEND_LLM.md). |
| Redis | `cache/client.py` | Async wrapper for caching, concurrency locks, refresh-token revocation, pub/sub | Owns the default logical DB. API rate limiting does **not** go through this wrapper — see [Cache Key Conventions](#cache-key-conventions). |
| Notifications | `notifications/service.py` | Outbound email notifications. Used by Validation (UC2) and Governance (UC5). | Enablement is governed by the SMTP peripheral: connection settings read from the `peripheral_config` row and the password from `dataspoke-smtp-secret` at send time. When SMTP is unconfigured (no row, host/from_address empty, or password unset), `send_email` raises `PeripheralNotConfiguredError('smtp')` -- best-effort callers swallow it; password reset propagates it. See [API §`/admin/peripherals/smtp`](../API.md). |
| Domain Models | `models/` | Shared Pydantic models (`QualityScore`, `EventRecord`, etc.) -- internal domain objects, not API schemas | API schemas live in `src/api/schemas/` |
| Exceptions | `exceptions.py` | `DataSpokeError` hierarchy with error codes for HTTP mapping | See [Error Handling](#error-handling) |
| Settings | `settings.py` | Pydantic `Settings` class reading `DATASPOKE_*` env vars | -- |

### LLM Inference Loop

Bounded ReAct loop wrapping every structured-output LLM call (UC3 ontogen,
UC4 metagen). The mechanics (two-layer enforcement, iteration bounds,
exhaustion behaviour), per-service validator rule tables, the
producer-reviewer adversarial debate framework (used by both UC3 ontogen
and UC4 metagen), and the DB-backed test-mode stub toggles (`RuntimeConfig`
row, flipped online via `PATCH /api/v1/admin/conf`) all live in
[BACKEND_LLM](BACKEND_LLM.md). Service sections below point at it where
they invoke the loop.

### Cache Key Conventions

| Pattern | TTL | Purpose |
|---------|-----|---------|
| `quality:{dataset_urn}:score` | 300s | Cached `QualityScore` aggregation for dataset attr-get |
| `ontogen:node:{node_id}` | 300s | Ontology Generation node lookup cache |
| `ontogen:edge:{edge_id}` | 300s | Ontology Generation edge lookup cache |
| `ontogen:triple:{triple_id}` | 300s | Ontology Generation triple lookup cache |

Rate-limit counters are outside this namespace and outside the `cache/client.py`
wrapper. The API's SlowAPI limiters delegate storage to the `limits` library,
which owns its own `LIMITS:LIMITER/*` keyspace with per-window expiry, in a
**dedicated Redis logical DB** separate from the keys above — so evicting cached
data can never clear a rate-limit or brute-force counter. The storage URI percent-encodes
the password, so `DATASPOKE_REDIS_PASSWORD` accepts any character. See
[API.md §Middleware Stack](../API.md#middleware-stack) and
[AUTH.md §Client-IP attribution for rate limiting](AUTH.md#client-ip-attribution-for-rate-limiting).

---

## Feature Services (`src/backend/`)

### Dataset Service (`src/backend/dataset/`)

**Covers**: Base dataset resource endpoints (`GET /data`, `GET /data/{urn}`,
`GET /data/{urn}/attr`, `GET /data/{urn}/event`)

Thin read-through service. Reads dataset identity/attributes from DataHub, aggregates
cross-domain event history into the **unified per-dataset timeline** (`GET /data/{urn}/event`).
Does not own any PostgreSQL configuration tables.

**Dataset catalog (`list_datasets` → `GET /data`)**: the cross-feature collection root.
It pages `dataset_registry` in SQL first (offset/limit/total_count, sortable by `dataset_urn`
via `parse_sort` — the same registry-paging pattern as `/ingestion/unmanaged`), then resolves
ingestion, validation, and metagen coverage **only for the page's URNs**, so cost is bounded by
page size, not by the whole registry. Coverage is composed from sibling services: `IngestionService`
(already held for the unified timeline) and `MetagenService`, plus a direct validation-coverage
lookup. Each row carries `dataset_urn`, the `ingestion` source list
(`[{source_id, name, mode, platform}]`, empty when no source covers the dataset), the `validation`
summary (`{covered}`), and the `metagen` conf list (`[{conf_id, name}]`, possibly empty).

- **`IngestionService.reverse_lookup_all_batch(urns)`** — batched reverse-lookup that resolves
  **every** covering source per URN in two queries (one over `IngestionSourceDataset` filtered by
  `dataset_urn IN urns`, one over the parent `IngestionSource` rows to pull `name`, `mode`, and
  `platform`), grouped per URN. Because `ingestion_source_dataset` is keyed `(source_id,
  dataset_urn)`, a dataset may be covered by several sources; this method returns all of them
  rather than collapsing to a single priority winner. Avoids the per-URN N+1 of calling
  `reverse_lookup` in a loop.
- **Validation coverage** — a single batch query (`SELECT dataset_urn FROM validation_configs
  WHERE dataset_urn IN (...)`) over the page's URN list; membership in the result set drives the
  per-URN `validation.covered` flag. Mirrors the ingestion batch pattern (one extra query per page).
- **`MetagenService.match_confs_for_urns(urns)`** — inverts the enabled-conf `dataset_filter`
  resolution into a `urn → [{conf_id, name}]` map. It evaluates each enabled conf's compiled
  filter clause against the page's URNs (the same matcher `list_uncovered` uses) and buckets
  matches by URN; cost is bounded by the conf count, not the dataset count.

**Unified timeline aggregation**: a dataset's events live in two places — validation and
metagen events are booked on `entity_type="dataset"` (`entity_id=urn`), while ingestion run
events are booked on the owning **source** (`entity_type="ingestion_source"`), not on the
dataset. `get_events` therefore unions two streams:

1. **Dataset-level events** — the `entity_type="dataset"` rows for this `urn` (validation
   `RESULT_RECORDED`, metagen `CANDIDATE_APPROVE`/`CANDIDATE_REJECT`).
2. **Ingestion runs** — reverse-looked-up via `IngestionService.reverse_lookup(urn)` to find
   the covering source, then that source's aggregated run events (the source-and-wrappers union
   already used by `GET /spoke/ingestion/sources/{id}/event`, see [§Querying Events](#querying-events)),
   each row carrying the derived `wrapper: bool`. The union is narrowed to **this** dataset:
   a row qualifies when its `detail.dataset_urn` is this URN **or is absent** — the source's
   per-dataset observations for sibling datasets are excluded, while its run-level rows, which
   carry no scalar `dataset_urn` at all, are kept (see
   [producers](#ingestioncomplete--ingestionfail-producers)). Absence covers both shapes: a
   missing key and an explicit JSON `null`. The predicate belongs to the shared base select, so
   the page query and its `total_count` cannot diverge.

The two streams are merged, sorted `occurred_at` newest-first, filtered by `from`/`to` and by
the repeatable `event_major_type` prefix set (`INGESTION`/`VALIDATION`/`METAGEN`; omitted = all),
then paginated in-memory (per-dataset event volume is small) with a correct `total_count`. The
`wrapper` flag is carried through unchanged; rows that are not ingestion events report
`wrapper: false`.

**DataHub aspects read**: `datasetProperties`, `editableDatasetProperties`, `ownership`,
`globalTags`, `glossaryTerms`, `schemaMetadata`, `editableSchemaMetadata`.

**`quality_score` (optional, cache-backed)**: an optional composite quality score
exposed via the `QualityScore` model (`overall_score`, `dimensions`). The dataset
service reads it from the Redis cache key `quality:{dataset_urn}:score` (see
[Cache Key Conventions](#cache-key-conventions)) and returns `null` when the key is
absent — it does **not** compute the score itself. The baseline ships no scoring
engine, so the field reads `null` unless an out-of-band process populates the cache.
Dashboard-facing quality measurement is owned by the governance `validation-score`
metric (see §Metrics Service), computed from `validation_results`.

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
no profiling, no stateful-ingestion soft-delete. `?dry_run=true` runs the extractor and returns
the schema preview without emitting any aspects.

The dataset URN's env/fabric segment is taken from the recipe's `source.config.env`; when the
recipe omits it, the extractor falls back to the DataHub peripheral's configured `default_env`
(see [`spec/API.md`](../API.md) `/admin/peripherals/datahub`), not a hardcoded literal. Likewise
the corpuser actor stamped on the assertion `lastUpdated` and the ingestion
run-event (`DataProcessInstance` `created`) audit stamps is the configured
`service_corpuser_urn`, defaulting to `urn:li:corpuser:dataspoke` when unset.

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
YAML. On `GET`, `${name__key}` references inside `recipe` are returned as-is; any plaintext
secret value is masked.

**Editability**: `DATAHUB_MANAGED` rows are read-only (DataHub is SSOT) — create/update/delete
return `409 INGESTION_SOURCE_READONLY`; they are written only by the sync sweep. `ACTIVE_CUSTOM_MANAGED`
and `PASSIVE` are user-managed via the API.

**Schedule**: `schedule` is a cron string. For `ACTIVE_CUSTOM_MANAGED`, on upsert the service
validates it maps to one of the three tiers (`hourly`/`daily`/`weekly`) and caches the result in
the internal `schedule_tier` column, which selects the Airflow tier DAG; `schedule: null` means
manual-only (runs only on `…/method/run`, never on a tier DAG). `DATAHUB_MANAGED` mirrors
DataHub's schedule; `PASSIVE` has none.

**Secret resolution** (the `src/shared/secrets/` shared resolver): recipes reference secrets DataHub-compatibly as
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
dataset aspects (skipped on `dry_run`; a non-dry-run whose emitted set is empty is treated as
failure) → emit `DataProcessInstanceRunEvent(COMPLETE | FAILED)` (skipped on `dry_run`) → record
the extractor's emitted URNs into `ingestion_source_dataset` (`derivation = emitted`, authority `high`)
→ record `INGESTION.COMPLETE` / `INGESTION.FAIL` event (see [Event Catalogue](#event-catalogue)).

The extractor discovers tables on **both** dry-run and real runs: it connects, crawls
`information_schema`, and applies the recipe's `schema_pattern` filter to produce the
**discovered** set (the dataset URNs it would emit — the plan). A dry-run stops there and emits
nothing; a real run then emits each discovered table's aspects, and the **emitted** set is the
subset whose aspects all wrote successfully. The run report (`detail` on both the run response and
the event) carries the discovered set vs the emitted set, so `discovered − emitted > 0` on a real
run signals per-table emission failures; on a dry-run the emitted set is empty. The runner only
ever emits table datasets, so both sides are plain dataset-URN lists. Exact key names are in the
[Event Catalogue](#event-catalogue) INGESTION row.

**Sync + mapping sweep** (`IngestionService.sync()`, called every two hours by the
`datahub-sync-hourly` DAG) reconciles all modes:

1. **Source defs (reconciling sync)**: pull `DATAHUB_MANAGED` source recipes + schedules via
   DataHub's `listIngestionSources` / `ingestionSource(urn)`; upsert read-only rows. The list is a
   faithful **mirror** of DataHub — at the end of the sweep, any `DATAHUB_MANAGED` row no longer
   present in DataHub is deleted, so removed and stale sources drop out of DataSpoke. The sweep
   mirrors only non-system sources (`sourceType != SYSTEM`, plus a deny-list on the reserved system
   source types `datahub-gc` and `datahub-documents` since their CLI wrappers are not tagged
   SYSTEM), matching DataHub's own Manage Data Sources view — system-internal jobs are excluded.
   Mask plaintext secret values in the stored/displayed recipe (DataHub returns them raw); `${...}`
   secret references are preserved as-is (not masked, not resolved).

   **Wrapper linkage (two passes).** When `datahub ingest` (or a UI/API `Run`) executes a
   registered source, DataHub auto-creates a **CLI wrapper source** that books the run on its own
   row rather than the registered source's. Wrappers are internal plumbing: they are linked to their
   registered parent and hidden from the source list. A wrapper is **detected** by DataHub-generated
   markers only — a `cli-` URN-id prefix, a `__datahub_cli_` `executor_id` prefix, or (last resort)
   a `[CLI] ` display-name prefix. A wrapper is **linked** to its parent via its recipe's top-level
   `pipeline_name` field, which DataHub sets to the registered parent's source URN. The wrapper's
   display name (`[CLI] <type> [<pipeline_name>]`) is cosmetic and derived; it is **never** used for
   linking, because DataHub names are user-editable and a renamed wrapper would silently lose its
   parent. The sweep resolves the link in two passes over the listed sources:
   - *Pass A (regular):* skip any row detected as a CLI wrapper; upsert the rest with
     `parent_source_id = NULL` and record `datahub_source_urn → id`.
   - *Pass B (wrappers):* for a detected wrapper, read its `recipe.pipeline_name`; if that value
     equals a regular source's `datahub_source_urn` from Pass A, upsert the wrapper with
     `parent_source_id = <parent id>`. A wrapper with no `pipeline_name`, or one whose
     `pipeline_name` resolves to no stored regular parent, is an **orphan**: it is **not stored** and
     is treated as stale.

   Because resolution is two-pass, parent ordering within the DataHub list does not matter. A row is
   a **wrapper** iff `parent_source_id IS NOT NULL`; a **regular** `DATAHUB_MANAGED` source iff it is
   `NULL`. Stale removal runs after both passes commit — orphan wrappers and removed sources alike
   drop out, and `ingestion_source.parent_source_id`'s `ON DELETE CASCADE` removes a deleted parent's
   wrappers automatically. The linkage rests on a three-way identity: a wrapper's
   `recipe.pipeline_name` == its parent's `datahub_source_urn` == the `systemMetadata.pipelineName`
   DataHub stamps on the run's emitted aspects. See
   [DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync)
   for wrapper detection markers and the `pipeline_name` linkage rule.
2. **Mapping**: list the DataHub dataset set once and rebuild `ingestion_source_dataset` by
   evaluating each source's **filter-matcher** — derived from the recipe's `platform`+`database`+
   `schema_pattern`/`table_pattern` for `DATAHUB_MANAGED`/`ACTIVE_CUSTOM_MANAGED`; the declared `AllowDenyPattern`
   scope for `PASSIVE`. `derivation = matched` (authority `medium`). Matching parses dataset URNs and applies filters the
   way the connector names them — declared/derived coverage, an explicit approximation (DataHub
   exposes no native source→dataset reverse lookup).

   **Name-shape contract.** `table_pattern` / `dataset_pattern` are matched against the full URN
   name. `schema_pattern` is matched against the name's **container segment**, whose position
   depends on how many segments the connector puts in the name — in a two-segment name the
   trailing segment is always the table, so the leading one is the container:

   | URN name shape | Platforms | `schema_pattern` evaluated against |
   |---|---|---|
   | `database.schema.table` | postgres, oracle with `add_database_name_to_urn` | second segment |
   | `schema.table` / `database.table` | athena, mysql, oracle (default) | leading segment |
   | single segment | unqualified names with no container in the URN | the whole name |

   On a two-tier platform whose leading segment is the database rather than a schema (mysql), an
   anchored `schema_pattern` is therefore filtering the database name. Kafka is the one platform
   outside this contract: `topic_patterns` is its own branch, tested against both the full name and
   — when a dot is present — the substring after the first dot (a kafka URN name is `<topic>` or
   `<platform_instance>.<topic>`), with allow and deny evaluated independently.

   When both `schema_pattern` and `table_pattern` are present a dataset must pass both, so an
   anchored `schema_pattern` evaluated against the wrong segment cannot be rescued by a correct
   `table_pattern`.

   **Coverage outcomes and prune invariant.** Rebuilding the `matched` mappings prunes only on
   evidence. When a source contributes no matches, three distinct outcomes hide behind that
   emptiness, and the sweep keeps them apart because they justify different actions on the rows
   already stored:

   | Outcome | Reached when | Stored `matched` rows | Signal |
   |---|---|---|---|
   | **Evaluated, no derivable patterns** | the recipe is well-formed and carries none of the four selection-pattern keys | **pruned** | none |
   | **Not evaluated** | the recipe cannot be parsed at all; the deciding selection-pattern key is wrongly shaped; a declared pattern does not compile; or the acryl-datahub library supplying `AllowDenyPattern` semantics cannot be imported while the source declares patterns | **left in place** | warning naming the source and what could not be read; `sources_pattern_degraded` |
   | **Evaluated, derivable, matched nothing** | the patterns ran and no dataset matched | **pruned** | warning naming the source and its platform, and `sources_zero_coverage`, **when DataHub holds datasets for that platform** |

   The first outcome prunes because coverage that cannot be inferred is never assumed: a source that
   declares nothing covers nothing, and an empty match set is the correct answer. The second does
   not, and the difference is load-bearing rather than pedantic — a source that *declares* no
   coverage and a source whose declared coverage *could not be read* are different facts, and only
   the first is an assertion about the estate. A `DATAHUB_MANAGED` recipe is mirrored from DataHub,
   so an unreadable recipe is a failed read of an upstream fact; pruning on it would delete mappings
   that are still true, because DataSpoke could not see the evidence for them this sweep.

   The third outcome is a defect signal rather than a legitimate result: without it, a misconfigured
   or wrongly-evaluated pattern is indistinguishable from a source that legitimately covers nothing.
   It is counted once per registered source — a CLI wrapper mirrors its parent's recipe, so counting
   it too would report one misconfiguration twice.

   Whether a source declares derivable coverage at all is decided by the **first selection-pattern
   key the matcher reads** in its cascade order — `schema_pattern` → `table_pattern` →
   `topic_patterns` → `dataset_pattern`: the source declares coverage when that deciding key carries
   an `AllowDenyPattern`-shaped value. Recipe JSONB is writer-supplied, so the key may instead hold
   `null` or a bare string; that is a **recipe defect**, and it places the source in the
   not-evaluated outcome — warned with the source and the offending key named — rather than in
   either pruning outcome.

   **Trust boundary on writer-supplied patterns.** A malformed, wrongly-typed or uncompilable
   pattern is caught when the matcher is built, degrading that one source to the not-evaluated
   outcome with a log line rather than aborting the sweep. The reason that log line reports is
   derived from recipe content and is therefore itself untrusted, so it is bounded in length and
   escaped before it reaches a log record: a writer cannot forge log structure or grow a record
   without limit. Pattern *execution* time is not bounded, so a pathological pattern — one whose
   backtracking cost grows explosively with the length of the name it is tested against — is
   unbounded synchronous CPU work inside the API process. Its blast radius is the whole process,
   not just the sweep: requests are served by a single worker, so all API request handling stalls
   until the liveness probe restarts the pod. Bounding pattern execution is tracked as issue
   **#114**.
3. **Dataset attribute sync**: refresh the `dataset_registry` columns every `dataset_filter`
   resolves against — `origin` and `platform_urn` parsed out of each dataset URN, `tag_urns` and
   `glossary_term_urns` from one paged attribute read, and `attrs_synced_at` as the watermark.
   Shape and hardening: [DATAHUB_INTEGRATION §Dataset attribute sync](../DATAHUB_INTEGRATION.md#dataset-attribute-sync).

   **Partial failure never narrows a filter.** The step **upserts per dataset and never
   deletes-then-inserts**: a dataset the attribute read did not return keeps its stored
   attributes. A half-completed read that blanked `tag_urns` would silently shrink the scope of
   every UC3, UC4, and UC5 filter in the system — a wrong answer that looks like a correct one —
   whereas retaining stale attributes is bounded, visible through `attrs_synced_at`, and
   self-correcting on the next sweep. The sweep summary counts refreshed rows as `attrs_synced`.
4. **Observed enrichment (optional, the two MANAGED modes)**: read `systemMetadata.pipelineName`
   per dataset to link datasets to their source authoritatively — `DATAHUB_MANAGED` (DataHub
   stamps the source URN), `ACTIVE_CUSTOM_MANAGED` (DataSpoke's extractor stamps the source id).
   `derivation = pipeline_name` (authority `high`). Not used for `PASSIVE`. A dataset's
   `pipelineName` awards `pipeline_name`/`high` to **every** source that corresponds to it: the
   registered source whose own `datahub_source_urn` equals the `pipelineName`, **and** any wrapper
   linked to that source. When DataHub runs a registered source, the aspects it emits are stamped
   `systemMetadata.pipelineName = <parent registered-source URN>`, while the run itself is booked on
   a CLI wrapper source. The wrapper is already linked to its parent in the source-defs step (via the stored
   `parent_source_id`), so the enrichment resolves the inheritance directly from that stored link — a
   wrapper inherits `pipeline_name`/`high` when its parent's `datahub_source_urn` equals the
   dataset's `pipelineName`. Sources that only recipe-match
   the same tables (no `pipelineName` correspondence) stay `matched`/`medium`. Orphan wrappers do
   not reach this step — they are never stored (source-defs step).
5. **Run and observation events**: book ingestion evidence into the `events` table with
   `event_type = INGESTION.COMPLETE` / `INGESTION.FAIL`, in **three sub-passes**:

   | Sub-pass | DataHub surface | Modes | Grain | Outcomes | `detail.source` |
   |---|---|---|---|---|---|
   | Execution-request mirror | `listExecutionRequests` | `DATAHUB_MANAGED` | per run | `COMPLETE` + `FAIL` | `datahub_sync` |
   | `Operation` observation | `Operation` aspects (`operationType ∈ {INSERT, UPDATE, CREATE, ALTER}`) | `PASSIVE` | per dataset | `COMPLETE` only | `passive_observation` |
   | `lastIngested` observation | `Dataset.lastIngested`, read once for the whole estate | **all modes** | per dataset | `COMPLETE` only | `last_ingested_observation` |

   The run layer and the observation layer are **additive, not alternatives**. Observation is
   inherently success-only — an `Operation` is written when data changes and `lastIngested` advances
   when aspects are written, so neither can express a failure. The run layer is therefore what
   carries run outcome, run identity, duration and diagnostics; observation is what answers *"was
   this dataset ingested, and when"*. A dataset's timeline consequently shows its own per-dataset
   `COMPLETE`s plus its owning source's run-level `FAIL`s, and never a per-dataset `FAIL`.

   **`lastIngested` observation** gives `PASSIVE` a per-dataset signal that does not depend on the
   estate's pipelines emitting `Operation` aspects, and is the only per-dataset evidence the two
   managed modes have. For every dataset mapped to a source, when DataHub reports a
   non-null `Dataset.lastIngested`, the sweep books an `INGESTION.COMPLETE` carrying
   `detail.dataset_urn`. Four properties of that guarantee are load-bearing:

   - **It is not an event *on* the dataset.** `entity_type = "ingestion_source"`,
     `entity_id = <source_id>`; the dataset link is `detail.dataset_urn` alone, and the per-dataset
     timeline resolves it by reverse-lookup plus a `detail.dataset_urn` predicate (see
     [Querying Events](#querying-events)).
   - **A dataset mapped to N sources books N events.** There is no owner arbitration at write time;
     the owning-source rule is a read-side resolution recomputed on every read. CLI wrapper sources
     are skipped: `lastIngested` is a property of the dataset with no wrapper affinity, the parent
     already covers the URN, and the per-source feed unions parent with wrappers — booking on both
     would show one fact twice on the parent.
   - **A null `lastIngested` books nothing.** It is null when every aspect on the dataset carries
     DataHub's `"no-run-id-provided"` sentinel — there is nothing observable to date. Absence is the
     guard; the alternative is minting an event at an instant DataHub never reported.
   - **The estate read is one call per sweep**, hoisted out of the per-source loop the same way
     the mapping step's dataset enumeration and the observed-enrichment step's `pipelineName` read
     are. Shape and paging rules:
     [DATAHUB_INTEGRATION §Observed Ingestion Recency](../DATAHUB_INTEGRATION.md#observed-ingestion-recency).

   The **execution-request mirror** follows DataHub's own model: a request has **no result aspect
   until the executor starts** (queued = "Pending…"), `startTimeMs` is the optional *execution
   start* time (absent/`0` before the executor runs), and DataHub's "last run" is shown with its
   real status — never coerced to failure. Each request carries a stable URN
   `urn:li:dataHubExecutionRequest:<id>`. Citations:
   [DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync).

   - **Identity is per *producer*, not per mode**, discriminated by `detail.source`:

     | `detail.source` | Producer | Identity | Write shape |
     |---|---|---|---|
     | `datahub_sync` | execution-request mirror | `detail.execution_request_urn` | **upserted** — at most one row per URN per source, so repeated syncs and status transitions are idempotent (no per-sync event growth) |
     | `passive_observation` | `Operation` observation | (source, `detail.dataset_urn`, `occurred_at`, `detail.source`) | **appended** — booked once for that instant, never again |
     | `last_ingested_observation` | `lastIngested` observation | the same four-term tuple | **appended**, same rule |

     The normative statement of the observed-ingestion identity tuple and why each term is
     load-bearing is
     [DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync);
     the full producer set, including the inline `ACTIVE_CUSTOM_MANAGED` run record, is in the
     [Event Catalogue](#ingestioncomplete--ingestionfail-producers).

     Appending is bounded by the observed instant, not by the sweep: an unchanged observation books
     nothing on the next sweep. The guarantee is "at least one event over the dataset's lifetime",
     not one per hour, and there is **no cap of one event per dataset per sweep** — every new
     qualifying instant since the last sweep is booked in that sweep, which is what makes two
     consecutive sweeps over an unchanged estate report zero. The `Operation` read is itself bounded
     to a few of the most recent aspects per dataset, so a dataset receiving more qualifying
     Operations than that between two sweeps loses the oldest — a bounded loss, not a growing lag.
   - **The dedup read binds a constant number of parameters, independent of estate size.** The
     observation identity is per-instant, so the naive prefetch matches `occurred_at` against the
     set of instants observed this sweep — one bind parameter per instant. The PostgreSQL wire
     protocol caps a statement at 32,767 parameters, so that shape fails outright on a source with
     more mapped datasets than the cap, and it fails on the **first** sweep, which books the whole
     historical backlog. The prefetch therefore bounds `occurred_at` by a **range** (`BETWEEN` the
     minimum and maximum instant of the batch) and intersects the exact tuples in memory: eight
     parameters whatever the estate holds.
   - **`occurred_at` is bounded on both sides for every producer, and an out-of-range value is
     rejected rather than clamped.**
     - Mirror: `startTimeMs`, falling back to `requestedAt` (the always-present request time on the
       execution-request input). Both are remote, writer-supplied values and both pass the same
       bounds as an observed instant; an execution neither field can date is **not mirrored**.
     - Observation: the observed millisecond timestamp must be a positive integer resolving to a
       representable instant no later than a small skew allowance past now. A value that is absent,
       zero, non-numeric, negative, out of range, or **future-dated** books nothing and is logged.

     Neither producer ever falls back to `now()`, and neither clamps. The consequences that make
     both bounds mandatory — a `now()` fallback breaking dedup so a dataset accrues one event per
     sweep forever, and one future-dated value permanently poisoning `ingestion-freshness` — are
     stated with the identity rule in
     [DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync).
   - **Status → event** (mirror only executions that reached a real ingestion outcome):

     | DataHub status | DataSpoke event |
     |---|---|
     | `SUCCESS`, `SUCCEEDED` (cross-version) | `INGESTION.COMPLETE` |
     | `FAILURE`, `TIMEOUT`, `ABORTED`, `ROLLBACK_FAILED` | `INGESTION.FAIL` |
     | `RUNNING`, `ROLLING_BACK`, `UP_FOR_RETRY`, *no result* | **not mirrored** (in-progress / pending) |
     | `CANCELLED`, `DUPLICATE`, `ROLLED_BACK` | **not mirrored** (not an ingestion outcome) |

   - **Source `latest_run` = latest terminal *run* outcome**, over run-level producers only. Two
     predicates, both required: an **event-type whitelist** (`INGESTION.COMPLETE` /
     `INGESTION.FAIL`), so `SOURCE_CREATE`/`SOURCE_UPDATE`/`SOURCE_DELETE` and any future non-run
     `INGESTION.*` cannot be read as a run; and a **`detail.source` blacklist** of the observation
     producers, so a newer per-dataset `COMPLETE` cannot outrank an older run `FAIL`. The blacklist
     must treat an **absent** `detail.source` as run-level — the inline `ACTIVE_CUSTOM_MANAGED`
     record carries no `source` key, and a bare `NOT IN` over SQL `NULL` drops exactly the events
     `latest_run` exists to report. In-progress and pending runs produce no event yet, mirroring
     DataHub's "Pending…", so `attr/ingestion.latest_run` reflects the most recent real outcome,
     not a transient or spurious failure. The per-source `event/…` timeline is deliberately **not**
     filtered this way: it shows every producer.

     **A `PASSIVE` source reports no `latest_run`, by construction.** Neither run-level producer
     covers that mode — the inline record is written only by an `ACTIVE_CUSTOM_MANAGED` run and the
     mirror only by a `DATAHUB_MANAGED` execution — so a passive source's only
     `INGESTION.COMPLETE`s are per-dataset observations, and `attr/ingestion.latest_run` is `null`
     for it. That is the intended reading and not a missing signal: DataSpoke does not orchestrate
     a passive pipeline and therefore never learns its run outcome, only that datasets received
     data. A passive source's recency is read from its datasets' observation events (its
     `event/…` timeline, and `ingestion-freshness` tier 1), never from a run outcome.

   The mirror runs for
   every `DATAHUB_MANAGED` row including wrappers, since a registered source's runs are recorded on
   its wrapper. **The regular source aggregates events across itself and its linked wrappers**: the
   per-source event endpoint and the per-dataset latest-run aggregation union the parent's own events
   with its wrappers' events (each carrying the derived `wrapper` flag — see below), so the run a user
   triggered surfaces on the regular source they look at, not on the hidden wrapper.
6. **Unmanaged bucket**: datasets in DataHub linked to no source (served by
   `GET /spoke/ingestion/unmanaged`).

**Sweep summary.** `sync()` returns a counter dict consumed by the activity endpoint and the DAG
log. Most counters report **state changes**, not rows examined: `datasets_mapped`,
`pipeline_links`, `events_mirrored`, `last_ingested_observed`, `sources_removed` and the
`registry_*` counters increment only
on an insert, a removal or a genuine transition (for `pipeline_links`, a new link or a `matched` →
`pipeline_name` upgrade — a re-confirmation of an existing `pipeline_name` row still refreshes its
`last_seen_at` but does not count). A second consecutive sweep over an unchanged estate returns zero
for all of those. `attrs_synced` is the exception and counts rows refreshed, not rows changed: it
reports how much of the estate the attribute read actually covered, which is the question a narrowed
filter raises.

The run-and-observation-events step's three sub-passes split across two counters:
`events_mirrored` covers the first two — the
execution-request mirror and the `Operation` observation — while `last_ingested_observed` covers the
third. It stays a counter of its own rather than folding into `events_mirrored`: the two have
different identity rules and different failure units — per-dataset best-effort inserts versus a
single estate-wide read that degrades wholesale. The **first sweep of a fresh deployment books the whole observable backlog**,
one event per mapped dataset DataHub can date, so a large first reading is historical catch-up
rather than a run storm; the reading collapses to zero on the next sweep over an unchanged estate.

Three counters are steady-state readings instead: `sources_synced` reports how many
`DATAHUB_MANAGED` rows were mirrored, counting inserts and updates alike, so an unchanged estate
reports the same non-zero value on every sweep; `sources_zero_coverage` and
`sources_pattern_degraded` each report a **condition** — respectively, sources that matched nothing
despite derivable patterns, and sources whose selection patterns could not be evaluated this sweep —
so each stays non-zero for as long as the affected sources do.

**Health side effect.** Because the sweep is the one scheduled process that exercises the GMS
metadata API end to end, it reports the `datahub-api` peripheral health as a side effect of
running: `ok` on completion, `error` carrying the message on failure — which is then re-raised,
so the activity endpoint still answers with a retryable failure. This is a side effect of the
sweep, not a step of the pipeline above. See [§Health reporting](#health-reporting).

Three rules keep the signal honest:

- **The `error` branch catches broadly** — any failure that escapes the sweep, not only
  `DataHubUnavailableError`. That exception covers retry-exhausted transport faults and an open
  circuit; an authentication or authorization failure (a rotated or revoked PAT) takes the
  client's fail-fast path and escapes as a raw SDK exception, as does a `GraphError` returned in
  an HTTP 200 body. Catching narrowly would leave the row serving `ok` against a stale
  `last_ok_at` through a dead credential — the one fault an operator most needs to see next to
  the token that caused it. The accepted trade-off: a non-GMS failure escaping the sweep (a
  database error, say) also flips the row. Over-reporting is preferable to a signal that reads
  `ok` through a revoked credential.
- **`ok` asserts only that the sweep's source-definition enumeration completed**, not that every
  GMS call inside it succeeded. Three of its GMS reads are best-effort
  (§[Best-Effort Operations](#best-effort-operations)) — the per-source run-history polls, the
  estate-wide `lastIngested` read, and the estate-wide dataset attribute read. A skipped source,
  an observation sub-pass that books nothing because that read failed, or an attribute read that
  refreshed nothing does not flip the row; a reader detects the last of those from
  `attrs_synced_at` standing still, not from `datahub-api`. The exception is an interface
  violation on the estate-wide `lastIngested` read,
  which is not a fault of the remote system and escapes to the `error` branch like any other
  unhandled failure.
- **The `error` report is committed independently of the sweep's transaction.** Written inside
  it, the re-raise rolls the report back and leaves `api_health` pinned to the last `ok`
  exactly when it is wrong. Which database that independent write lands on is governed by
  [§Shared Services](#shared-services-srcshared) (PostgreSQL row).

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
DataSpoke's own extractor records its runs inline (see run pipeline above). `DATAHUB_MANAGED` runs
are mirrored from `listExecutionRequests` by the `datahub-sync-hourly` DAG, and the same DAG's
observation sub-passes supply the per-dataset `COMPLETE`s for every mode — the sweep's
**run and observation events** step above holds the status→event mapping, the per-producer
identity rules, and the `occurred_at` bounds.

The two **run-level** producers stamp `occurred_at` at **opposite ends of the same run**, and a
consumer reading the ordered feed has to expect it. The inline `ACTIVE_CUSTOM_MANAGED` record is
written at run *completion*, so it sorts **after** the observations that run produced. The
`DATAHUB_MANAGED` mirror uses `startTimeMs`, so it sorts **before** them. Neither ordering is a
defect and neither may be assumed by the other's consumers: "the newest event for this source" is
not "the newest run", which is why `latest_run` filters on producer rather than taking the head of
the feed.

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

- `PUT`/`PATCH` validates the body against the conf shape (`description ≤ 2,000` chars;
  `variables` a list of `{name, description}` objects, ≤ 200 entries, each `name`
  matching `[a-z][a-z0-9_]{0,99}` and unique within the rule, each `description`
  required but ≤ 200 chars with the empty string allowed). The dataset must already
  exist in DataHub or the request is rejected with `422 DATASET_NOT_IN_DATAHUB`. On
  success the service:
  1. upserts the row in `validation_configs` (`dataset_urn` PK). A `PUT` against
     an absent slot simply creates it (`201`); there is no deleted/frozen state and
     no resurrection concept,
  2. emits `assertionInfo` to DataHub at the deterministic URN
     (`urn:li:assertion:<datahub_guid({"platform": "dataspoke-validation", "entity": dataset_urn})>`)
     with `type = CUSTOM`, `source.type = EXTERNAL`,
     `customAssertion.type = "DATASPOKE_VALIDATION"`,
     `customAssertion.entity = <dataset_urn>`, and
     `customAssertion.logic = "<comma-joined declared variable names>"`.
- `DELETE` performs a **hard delete with cascade**, in a single transaction: it deletes
  the dataset's `validation_results` rows, deletes the dataset's validation events
  (`VALIDATION.*` only — other-feature events for the same dataset are untouched), deletes
  the `validation_configs` row, then hard-deletes the DataHub assertion **entity** (no
  `status.removed` tombstone). It records **no** event — the cascade wipes the dataset's
  validation events. Returns `204`. Afterwards `GET`/`PATCH` return `404 CONFIG_NOT_FOUND`
  and a fresh `PUT` re-creates the conf and the assertion under the same URN.
- A DataHub error during `assertionInfo` emission or the assertion hard-delete surfaces as `502` or `503`
  per the DataHub error envelope — config save and DataHub assertion lifecycle are
  coupled by design because DataHub is the SSOT for assertion definitions.

**Result ingest and query** — `POST/GET /attr/validation/result`.

- `POST` validates `data_time` (RFC 3339 → `422 INVALID_PARAMETER` if not),
  `score ∈ [0.0, 1.0]` (else `422 INVALID_SCORE`), and `variables` keys ⊆ the set of
  the conf's declared variable **names** (else `422 UNKNOWN_VARIABLE` listing the
  offending names).
  Missing declared keys are accepted silently — partial coverage is a legitimate signal.
  On success the service:
  1. inserts the row in `validation_results` (`dataset_urn`, `data_time`, `score`,
     `variables` JSONB, `ingestion_time = now()`),
  2. emits `assertionRunEvent` to DataHub with `timestampMillis = data_time` (epoch ms,
     UTC), `runId = uuid4()`, `result.type = SUCCESS` if `score == 1.0` else `FAILURE`,
     `result.actualAggValue = score`, `result.nativeResults` populated as
     `Map<string,string>` with `repr(float)` of each variable plus `"score"` itself,
     `runtimeContext.ingestion_time = now()`. The acting corpuser URN is the DataHub
     peripheral's configured `service_corpuser_urn` (default `urn:li:corpuser:dataspoke`),
     not a hardcoded constant.
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

**Cross-dataset list view** — `list_configs` (`GET /spoke/validation`). Each row aggregates
a dataset's `attr/validation/*` (conf `description` + `variable_count` + latest result
`data_time`/`score`). The `coverage` param (`covered` \| `uncovered` \| `both`, default
`covered`) selects the row set, kept SQL-paginated in all branches:

- `covered` — datasets that hold a `validation_configs` slot, joined to their latest result
  (current behavior, unchanged).
- `uncovered` — registered URNs (`dataset_registry`) `NOT IN (SELECT validation_configs.dataset_urn)`,
  the same registry-difference shape as `/ingestion/unmanaged`; no result join, conf fields null.
- `both` — `dataset_registry LEFT JOIN validation_configs`, conf fields null for the uncovered
  rows.

In `uncovered`/`both` the ordering is tiebroken by `dataset_urn` (null `updated_at` last) so
paging stays deterministic regardless of the requested `sort`.

Uncovered rows carry null `description`, null `variable_count`, null `latest_data_time`, and
null `latest_score`.

**Multi-rule scope-out.** Teams that need multiple distinct checks per dataset (separate
freshness / volume / field assertions, per-column validators, multi-team ownership) use
DataHub's native assertion APIs directly; DataSpoke is the opinionated single-rule
shortcut for the 80% case, not the only path.

### Metadata Generation Service (`src/backend/metagen/`)

**Covers**: MANIFESTO §2.1 Metadata Generation (UC4). Behavioural narrative —
including the conf-collection / per-dataset-boundary split, item / candidate
model, and approval lifecycle — lives in
[USE_CASE §UC4](../USE_CASE_en.md#uc4-metadata-generation). DataHub aspect
write rules are catalogued in
[DATAHUB_INTEGRATION §Editable vs Non-Editable Description Aspects](../DATAHUB_INTEGRATION.md#editable-vs-non-editable-description-aspects).
This section describes the implementation only.

**Conf collection** at `/spoke/metagen/conf` — many named confs can coexist, each
with its own scope, schedule, and budget. Unlike the UC3 ontology (one all-connected
artifact, hence one singleton conf), metadata writers can legitimately be many: teams
run different documentation policies over different dataset groups, and the shared
UC3 ontology — which every conf reads — holds cross-conf consistency. Per-conf fields:

| Field | Purpose |
|-------|---------|
| `name` | Unique conf name; `409 METAGEN_CONF_EXISTS` on create collision. |
| `is_enabled` | Master switch — enabled confs run on their `schedule_tier` and participate in the scheduled fan-out. |
| `schedule_tier` | `hourly` / `daily` / `weekly` re-generation cadence; null = on-demand only. |
| `dataset_filter` | Scope filter — a SQL `WHERE` clause over `dataset_registry` ([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar)); the empty string means all registered datasets. Malformed text is rejected at POST/PUT/PATCH (`422 INVALID_DATASET_FILTER`, `422 INVALID_DATASET_URN` for a malformed URN literal); literal URNs matching no registered dataset are reported in the run-complete event's `unresolved_urns`. Same grammar as UC3 `ontogen/attr/conf.dataset_filter` and UC5 `metric/{metric_id}/attr/conf.dataset_filter`. |
| `result_limit` | Integer ∈ `[1, 20]`, default `3`. Maximum candidate count per `(conf, item)` at any time. |
| `overwrite_pending` | Boolean, default `true`. When this conf already holds `result_limit` non-rejected candidates on an item that has no `approved` candidate, controls whether a new run evicts the conf's oldest `llm_approved` candidate (`true`) or skips the item (`false`). |

Each conf is a row in `metagen_config` (collection table keyed by `id UUID`; see
[BACKEND_SCHEMA §metagen_config](BACKEND_SCHEMA.md#metagen_config)).
`DELETE /spoke/metagen/conf/{conf_id}` is a hard delete that retains every item,
candidate (any status), and candidate embedding the conf produced. The FK `metagen_candidates.conf_id` (`ON DELETE SET
NULL`, nullable) orphans all of the conf's candidates by nulling their `conf_id`;
they become parentless results forever with no re-linking. Read paths are
null-safe, so orphaned candidates surface without a producing conf.

`GET /spoke/metagen/conf` attaches two DB-derived rollup fields to each listed
conf, computed from cheap grouped queries keyed by the page's conf ids (no live
DataHub call): `dataset_affected_count` (`COUNT(DISTINCT dataset_urn)` over
`metagen_candidates` for the conf — distinct datasets already holding a candidate
it produced) and `last_run_at` (newest `RUN_COMPLETE`/`RUN_FAILED` event time from
`events`, `null` when the conf has never run).

**Per-dataset boundary** at `/spoke/common/data/{urn}/attr/metagen/boundary`,
stored in `metagen_boundary` and shared across all confs. A row with
`is_enabled=true` opts the dataset in; missing row or `is_enabled=false` is opt-out.
The `allowed` array restricts which element kinds any conf's generator may write on
this dataset. Baseline values: `dataset.description`, `column.description`.
`GET` returns a `null` body with `200 OK` when the boundary row has never been
written; clients distinguish the unset state by `null` rather than by a `404`.

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

**Scheduled fan-out**. The Airflow tier DAGs (`metagen-{hourly,daily,weekly}`)
call the internal activity `POST /internal/activities/metagen/run {tier}`. That
activity enumerates every `is_enabled=true` conf whose `schedule_tier` matches the
fired `tier` and runs each one under its own per-conf lock. A conf already running
(its lock held) is skipped for this tick, not retried inline. A manual
`POST /spoke/metagen/conf/{conf_id}/method/run` drives exactly one conf.

**Generation Pipeline** (per conf — the unit of a run):

1. Enumerate **in-scope datasets** for this conf — the datasets the conf's
   `dataset_filter` resolves to **intersected** with the set of datasets
   that have a `metagen_boundary` row with `is_enabled=true`. Boundary-less or
   boundary-disabled datasets are excluded regardless of the conf's
   `dataset_filter`. Literal `dataset_urn` values matching no registered dataset are
   accumulated for the run-complete event's `unresolved_urns`. If the in-scope set is empty, the run
   still completes successfully and emits `METAGEN.RUN_COMPLETE` with all
   counts at zero so reviewers and ops dashboards see every scheduled
   tick.
2. **Clear this conf's `rejected` candidates** across the in-scope datasets so the
   per-`(conf, item)` budget frees up.
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
   `(dataset_urn, column.<fieldPath>.description)` per column. Items are shared
   across confs (keyed by `(dataset_urn, item_id)`). Drop items whose kind is
   outside the dataset's `metagen_boundary.allowed`. Drop items that currently
   have an `approved` candidate **from any conf** — the reviewer has expressed a
   settled preference, so every conf pauses on this item until the approval is
   moved to a different sibling.
5. **Producer-Reviewer Adversarial Debate** generates candidates per
   surviving (dataset, item) pair. See
   [BACKEND_LLM §Metagen Adversarial Debate](BACKEND_LLM.md#metagen-adversarial-debate).
   The producer emits candidate `value`s; the reviewer evaluates each
   against ontology context and existing approved descriptions; only
   candidates with reviewer outcome `accept` and
   `confidence_score >= METAGEN_CONFIDENCE_THRESHOLD` persist.
6. **Apply per-`(conf, item)` budget** — for each (dataset, item) whose surviving
   candidate count for **this conf** exceeds the slack (`result_limit -
   non_rejected_count` counted over this conf's candidates on the item), either
   evict this conf's oldest `llm_approved` candidate (FIFO by `created_at`, when
   `overwrite_pending=true`) or drop the new candidate (when
   `overwrite_pending=false`). Other confs' candidates on the same item are
   untouched.
7. **Persist** the accepted candidates as `metagen_candidates` rows with the
   producing `conf_id` and `status='llm_approved'`. Refresh
   `metagen_candidate_embeddings` for these newly `llm_approved` candidates (and again
   when a candidate is later promoted to `approved` via review) that will inform
   the next run's Reviewer RAG (the anchor pool is global per `kind`, conf-agnostic).

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
  on the same item back to `llm_approved` (the sibling may belong to a
  **different conf**), then emit the new value to the corresponding editable
  DataHub aspect and emit `METAGEN.CANDIDATE_APPROVE`. Approval is mutable —
  the reviewer can switch which sibling is approved at any time, and the
  partial unique index `UNIQUE (dataset_urn, item_id) WHERE status='approved'`
  keeps "at most one approved per item, globally across confs" a hard
  invariant. Generation runs (any conf) skip items that currently have an
  `approved` candidate, so accumulating siblings only happens before the first
  approval (or after the user demotes the current approval by approving a
  different sibling).
- `verdict: "reject"` → flip the candidate's status to `rejected` and emit
  `METAGEN.CANDIDATE_REJECT`. The row is deleted at the start of the next
  run. Reject is valid on both `llm_approved` and `approved` candidates:
  - Rejecting an `llm_approved` candidate writes nothing to DataHub — the
    candidate had never been emitted.
  - Rejecting an `approved` candidate additionally **clears the editable
    DataHub description it had written** so the dataset falls back to its
    non-editable description. The clear is merge-preserving — it reads the
    existing aspect, nulls only the relevant description field, and re-emits,
    leaving co-located editable metadata intact. By item kind:
    `dataset.description` → clears `EditableDatasetProperties.description`,
    preserving any editable `name` and audit stamps; `column.description` →
    nulls only that field's `editableSchemaFieldInfo.description`, retaining
    the field entry so its `globalTags`/`glossaryTerms` and sibling fields
    survive. The clear is best-effort, mirroring the approve-time emit; the
    status flip and event are not rolled back if the DataHub write fails.

Sibling `llm_approved` candidates on the same item are not auto-touched on
approval — they remain visible as read-only history and are eligible for
later approval.

**Concurrency**. Generation runs are serialised **per conf** by a Redis lock
`metagen:running:{conf_id}`. A duplicate `conf/{conf_id}/method/run` while that
conf is in flight returns `409 METAGEN_RUNNING`; distinct confs run concurrently.
The scheduled fan-out skips a conf whose lock is already held.

**Disabled-config rejection**. `conf/{conf_id}/method/run` with the conf's
`is_enabled=false` and `dry_run=false` raises `409 METAGEN_DISABLED`. Dry-run is
permitted regardless of `is_enabled`.

**Boundary guard**. Candidate review against a dataset whose
`metagen_boundary` is absent or `is_enabled=false` returns
`422 METAGEN_DATASET_NOT_IN_BOUNDARY`.

**Uncovered view** (`GET /spoke/metagen/uncovered`). Computes which registered
datasets no conf documents, the metagen analogue of UC1's ingestion unmanaged
bucket. Over `dataset_registry` rows with `datahub_registered=true`:

- **Default** (`include_disallowed=false`): a dataset is `uncovered` with
  `reason="no_conf_match"` when it matches **no** `is_enabled=true` conf's
  `dataset_filter`.
- **`include_disallowed=true`**: additionally includes datasets that **do** match
  some enabled conf's `dataset_filter` but whose boundary blocks generation —
  missing, `is_enabled=false`, or empty `allowed` — with
  `reason="boundary_blocked"`. A dataset matched and writable by at least one
  enabled conf is never listed.

The view is paginated and read-only; it never triggers generation.

**Covered-datasets view** (`GET /spoke/metagen/conf/{conf_id}/dataset`). The
per-conf inverse of the uncovered view: lists the datasets this conf's
`dataset_filter` matches. It pushes the compiled filter clause
(`src/backend/_dataset_filter.py`) into its own paginated query over
`dataset_registry` and left-joins each matched dataset's `metagen_boundary`, so
sorting and paging happen in SQL. Each row carries `dataset_urn`,
`is_enabled`, `allowed`, `blocked` (bool), and `reason`.

- A dataset is `blocked` when its boundary is missing, `is_enabled=false`, or has
  empty `allowed` — the same `boundary_blocked` reason vocabulary as the uncovered
  view. Writable datasets carry `blocked=false`.
- **Default** (`include_disallowed=false`): only writable (non-blocked) covered
  datasets are returned.
- **`include_disallowed=true`**: also includes boundary-blocked covered datasets.

`404 METAGEN_CONF_NOT_FOUND` when the conf is absent. The view is paginated
(sortable by `dataset_urn`, default `dataset_urn_asc`) and read-only.

**Per-dataset rollup view** (`GET /spoke/metagen/dataset`,
`list_dataset_summaries`). Aggregates the global item queue into one row per
dataset: groups `metagen_items` by `dataset_urn` for `item_count` and
`last_modified_at` (max item `created_at`), derives candidate-level
`approved_count`/`rejected_count`/`candidate_count` from the joined
`metagen_candidates`, and left-joins `metagen_boundary` for `is_enabled`/`allowed`
(`is_enabled=false`, `allowed=[]` when no boundary). The `dataset_urn` filter is a
text match; a `conf_id` filter (validated UUID, `404 metagen_conf` when absent)
restricts rows to datasets holding a candidate from that conf and scopes the
candidate counts to that conf's candidates. Paginated, sortable by
`last_modified_at` (default `last_modified_at_desc`), read-only.

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
| `dataset_filter` | Scope filter — a SQL `WHERE` clause over `dataset_registry` ([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar)); the empty string means all registered datasets. Malformed text is rejected at PUT/PATCH (`422 INVALID_DATASET_FILTER`, `422 INVALID_DATASET_URN` for a malformed URN literal); literal URNs matching no registered dataset are reported in the run-complete event's `unresolved_urns`. Same grammar as UC4 per-conf `metagen/conf.dataset_filter` and UC5 `metric/{metric_id}/attr/conf.dataset_filter`. |
| `default_run_prompt` | Optional Markdown string used as the one-shot prompt for runs without an explicit body — i.e., the periodic Airflow DAG and manual `POST /method/run` calls with no body. Null disables the default. |

UC3 inputs are sourced entirely from DataHub-resident metadata (the proofread
boundary shared with UC4).

The conf is a single row in `ontogen_config` (singleton table; see
[BACKEND_SCHEMA §ontogen_config](BACKEND_SCHEMA.md#ontogen_config)).

**Seeds** at `/spoke/ontogen/attr/seed/{seed_id}` are human-authored Markdown
documents (prompts, domain hints, naming conventions) that the inference run consumes
alongside the data sources. The endpoint accepts and returns raw Markdown
(`Content-Type: text/markdown`); only `seed_id`, `is_enabled`, and timestamps are
managed out-of-band. A seed is created **disabled** (`is_enabled = false`, consistent
with the conf/metric factory-default convention) and does not participate in inference
until enabled via `PATCH .../attr/seed/{seed_id}/attr/enabled` (JSON `{is_enabled}`);
disabling is reversible and retains the seed. `DELETE` is a hard delete. `list_seeds`
returns **all** seeds (enabled and disabled) with their `is_enabled` state. Stored in
`ontogen_seeds` (see [BACKEND_SCHEMA §ontogen_seeds](BACKEND_SCHEMA.md#ontogen_seeds)).

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
2. Resolve `dataset_filter` against `dataset_registry` (one SQL query, no DataHub
   search). Literal `dataset_urn` values matching no registered dataset are skipped and
   accumulated for the run-complete event's `unresolved_urns`. The empty filter means all
   registered datasets.
3. Fetch DataHub evidence per in-scope dataset (the proofread boundary shared
   with UC4): `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
   `editableSchemaMetadata`, `glossaryTerms`, and `documentInfo.contents.text` on
   `document` entities whose `relatedAssets` reference an in-scope dataset
   (Markdown body, capped per dataset). DataSpoke writes the editable aspects
   only after a UC4 reviewer approves a candidate; unreviewed candidates stay in
   `metagen_candidates` with `status='llm_approved'`, so *presence* in DataHub is
   the approval signal — UC3 reads DataHub directly with no JOIN against
   `metagen_candidates`.
4. Load enabled seeds (`ontogen_seeds.is_enabled = true`). Resolve the one-shot prompt:
   if the `POST /method/run` request carries a non-empty `text/markdown` body, use
   that body; otherwise fall back to `ontogen_config.default_run_prompt` (used by both
   the periodic Airflow DAG and bodyless manual calls). The one-shot prompt is
   appended after the seeds and is not stored.
5. LLM proposes nodes per dataset. For each candidate, look up the closest existing
   node via `node_embeddings` (cosine similarity, threshold
   `ONTOLOGY_CONFIDENCE_THRESHOLD` — node reuse shares the confidence threshold, with no
   separate constant); if a match exists, reuse the existing node ID.
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
DataHub aspects (`DatasetProperties`, `SchemaMetadata`), the mirrored dataset attributes in
`dataset_registry` that `dataset_filter` resolves against, and DataSpoke event /
validation-result rows. The metrics layer therefore has no source credentials and no
SQL access to production databases. Unsupported `metric_type` or an unknown `metrics[]`
`name` returns `422 INVALID_PARAMETER`.

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

**List `last_run_at`**: `list_metrics` (→ `GET /spoke/governance/metric`) augments each row with
`last_run_at`, the `occurred_at` of the latest `METRIC.RUN_COMPLETE` event for the metric (`null`
when it has never completed a run). It is resolved in a page-bounded batch lookup: one grouped
query (`SELECT entity_id, MAX(occurred_at) ... WHERE entity_type='metric' AND
event_type='METRIC.RUN_COMPLETE' AND entity_id IN (page ids) GROUP BY entity_id`) returns the
newest run per metric on the page; the `events(entity_type, entity_id, occurred_at DESC)` index
serves it without a per-row N+1. `last_run_at` is a list-row-only field (carried by the
`MetricDefinitionListItem` schema); single-GET, `attr/conf`, create, replace, and patch
responses use the bare `MetricDefinitionResponse` and do not expose it.

**Mode**: `active` runs the built-in measurer matching `metric_type`. `passive` is
rejected in the route handler with `501 NOT_IMPLEMENTED` — placeholder for ingesting
results emitted by an external system, deferred to a future release.

**Measurement window** (`ingestion-freshness`, `validation-score`): the window is
`metric_conf.time_window_sec`, applied uniformly to every dataset in the run. It is a declared
SLO the governance lead owns, not a quantity derived from a per-dataset fact such as an owning
source's registered schedule, a sync-loop cadence, or a dataset's observed validation
inter-arrival gap — those state how often something is *expected* to happen, which is a
different question from how recent the evidence must be to count. Each run records the window
it applied in the breakdown's `detail.time_window_sec`.

- `ingestion-freshness`: a dataset counts toward `ingested_in_time` when its resolved
  ingestion evidence (below) is no older than `time_window_sec` at measurement time.
- `validation-score`: the score counted is the latest result whose `data_time` is inside
  `time_window_sec`; a dataset with no result in the window contributes `0.0`.

Both measurers stay pure-aggregation and DataSpoke-DB-side: they read `events`,
`validation_results`, `ingestion_source`, and `ingestion_source_dataset` only — no DataHub call.

**Ingestion evidence** (`ingestion-freshness`): every `INGESTION.*` event is booked on a source
(`entity_type="ingestion_source"`, `entity_id=source_id` — see the
[Event Catalogue](#event-catalogue)) and never on the dataset, so the measurer resolves each
dataset's **owning source** first. It then reads that source's feed in **two tiers of
evidence**, per-dataset first and source-level as fallback.

| Tier | Evidence | Applies to |
|---|---|---|
| 1 (preferred) | `max(occurred_at)` over the observation events the owning source booked **for that dataset** — `detail.dataset_urn = <urn>`, `detail.source ∈ {passive_observation, last_ingested_observation}` | any dataset DataHub reports an ingestion trace for |
| 2 (fallback) | `max(occurred_at)` over **every** `INGESTION.COMPLETE` booked on the owning source — no producer filter, **excluding dry runs** | datasets with no observation evidence yet |

Tier 1 exists because a run-level `COMPLETE` is a claim about a *run*, not about a dataset, and
four independent facts make it unsound as a per-dataset claim:

1. **A dry run emits nothing by definition**, yet a dry run without errors still books
   `INGESTION.COMPLETE` (carrying `detail.dry_run = true`) — only a *real* run whose emitted set
   is empty is coerced to failure.
2. **Partial emission still reads `COMPLETE`.** `discovered − emitted > 0` on a real run signals
   per-table emission failures, and those tables were not ingested.
3. **A `DATAHUB_MANAGED` `SUCCESS` is not a per-table claim** — the status mapping does not
   inspect the run's structured report.
4. **The mapping is broader than the run.** A `matched` mapping is recipe-*pattern* derived, so a
   dataset the source merely *could* cover inherits its freshness having never been ingested.

Tier 2 keeps cases 2–4 by construction — an event booked on a source genuinely cannot say which
dataset it touched — so it applies only where nothing better exists. It is **source-grained, not
producer-filtered**: any `COMPLETE` on the owning source qualifies, so a sibling dataset's
observation can stand in for a dataset that has none of its own. That is the same
approximation the four cases describe, and it is the reason tier 1 is preferred wherever it
exists. Because per-dataset evidence exists for every mode, the measurement is never worse than a
purely source-grained one, and exact wherever DataHub can date the dataset. The **dry-run
exclusion is required on tier 2 regardless**, or case 1 survives untouched in the fallback path; a
producer that carries no `dry_run` key at all (the mirror and both observation producers) is
included, since only the inline `ACTIVE_CUSTOM_MANAGED` record ever sets it.

**`latest_run` and freshness read the same feed differently, deliberately.**
`attr/ingestion.latest_run` answers *"what was the last run outcome"*, so it reads run-level
producers only and does **not** filter dry runs: a dry run is a real run outcome the operator
asked for, and its `detail.dry_run` flag is already on the event for the reader to act on.
Freshness answers *"when was this dataset last ingested"*, so it prefers per-dataset evidence,
excludes dry runs (which ingested nothing), and applies **no** producer blacklist in its fallback —
an observation is exactly the kind of recency evidence it wants, even a sibling's. Filtering dry
runs out of `latest_run` would hide a run the operator triggered; admitting them into freshness
would mark an untouched estate fresh.

A producer blacklist on tier 2 would not merely narrow the fallback, it would **empty** it for
`PASSIVE`: that mode books no run-level event at all, so its only `INGESTION.COMPLETE` rows are
observations. Blacklisting them would leave every `PASSIVE` dataset without its own observation
with no evidence of any kind, reading permanently stale. Tier 2 is therefore source-grained and
producer-agnostic by design, not by omission.

**Owning source** is what `IngestionService.reverse_lookup` returns — or, over a whole
dataset list at once, its batched single-winner sibling `reverse_lookup_batch`, which the
measurer calls and which resolves the identical rule in two queries rather than one round
trip per URN. Either way the resolution runs in two steps. First a
sort over the dataset's covering sources — a dataset may be covered by several: `derivation`
rank `emitted` > `pipeline_name` > `matched`; at equal rank a regular parent beats its CLI
wrapper; remaining ties go to the most recent `last_seen_at`. Then, **if the sort winner is
itself a wrapper it resolves up to its regular parent** — a wrapper is never the owning
source. The second step is not the tie-break restated: it also fires when a wrapper claims a
dataset at a *higher* derivation rank than its parent, where the tie-break never runs. That
the owning source is always regular is what makes the wrapper-run union below well-defined.

This is the resolution the per-dataset event timeline also uses, so freshness and the
timeline agree on which source owns a dataset by construction. Freshness is explicitly
**not** "the most recent event across all covering sources": that is non-deterministic where
the priority rule is not, and it would let a source that merely recipe-matches a table
(`matched`) mask the staleness of the pipeline that actually writes it. The owning source's
**CLI-wrapper runs count as its own** — DataHub books a managed source's executions on an
auto-created wrapper rather than on the registered source, so a source's events are the union
of its own and its wrappers', the same union `GET /spoke/ingestion/sources/{id}/event` serves
(see [Querying Events](#querying-events)).

**Measurers** (`src/backend/metrics/measurers/`): one async function per built-in
`metric_type`, registered via the measurer registry. Each measurer receives the resolved
dataset URN list, `metric_conf`, a `DataHubClient`, and an `AsyncSession`, and returns
`(values, verdicts)`. `values` keys are exactly those listed in
[USE_CASE §UC5 — Built-in active metric types](../USE_CASE_en.md#built-in-active-metric-types);
the service filters the dict to the names declared by `attr/conf.metrics[]` before
persisting.

**Verdict contract.** `verdicts` covers **every** dataset in scope, not only the failing
ones — one entry per dataset carrying `urn`, `met: bool`, `evidence_at: datetime | None`,
and a type-specific `detail`. Full coverage is what makes "in scope but never evaluated"
(`unknown`) distinguishable from "evaluated and passing": a failures-only return cannot
express the difference. The failures-only `breakdown` below is **derived** from the
verdicts, so the stored `metric_results.breakdown` payload is unchanged and the two can
never disagree. `evidence_at` per type: `ingestion-freshness` → the resolved ingestion
evidence time; `validation-score` → the counted result's `data_time`; `doc-health` →
`None`, since a documentation state carries no timestamp.

**Per-dataset verdict store** (`metric_dataset_results`, keyed `(metric_id, dataset_urn)`)
holds the **latest** verdict only. A non-dry run replaces the metric's rows wholesale
inside the result transaction, so the store always reflects exactly one run. A **dry run
persists nothing** — the standing metrics invariant — leaving the previous run's verdicts
readable. Deleting a metric definition clears its verdicts.

`GET /spoke/governance/metric/{metric_id}/dataset` serves the store joined to the metric's
current scope: it pushes the compiled filter clause into a paginated query over
`dataset_registry` and left-joins the verdict rows, so a dataset in scope with no verdict
reads `met = "unknown"`. Resolving scope from the same registry the run resolved it from is
what keeps the run's verdicts and the endpoint's dataset list from disagreeing. The response
envelope carries `attrs_synced_at` (aggregation defined in
[API §Metric](../API.md#metric-spokegovernancemetric)) because a filter that matches nothing
and a filter whose attributes have not yet synced are otherwise indistinguishable.

**`doc-health`** sources table description from `DatasetPropertiesClass.description` (or
`EditableDatasetPropertiesClass.description` when present) and column descriptions from
`SchemaMetadataClass.fields[*].description` (overlaid by `EditableSchemaMetadataClass`
when present). A dataset scores `1.0` iff the resolved table description is non-empty
and every column has a non-empty description; otherwise `0.0`.

**Dataset resolution**: UC3 ontogen, UC4 metagen, and UC5 metrics share one
`dataset_filter` grammar ([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar))
and one resolver, `src/backend/_dataset_filter.py`. Resolution is two stages:

| Stage | Module | Output |
|---|---|---|
| Parse | `src/shared/dataset_filter.py` | An AST, or a syntax error carrying the offending character position (surfaced as `422 INVALID_DATASET_FILTER`). Also exposes the filter's literal `dataset_urn` values and a canonical formatter |
| Compile + run | `src/backend/_dataset_filter.py` | A SQLAlchemy boolean expression over `dataset_registry`, run as one query restricted to `datahub_registered = true`. An empty filter is the bare registered set |

Two properties are load-bearing. **Every literal compiles to a bound parameter** and the
column set is the grammar's own whitelist, so user filter text never reaches the database as
SQL text — the parser is the only thing between an operator's input and a query. And the
resolver **materialises no URN list where the caller can page in SQL**: it also exports the
compiled clause on its own, so per-conf and per-metric dataset views push the filter into
their own paginated query rather than slicing a resolved list in Python.

`dataset_urn` literals that match no registered dataset are accumulated into the run-complete
event's `unresolved_urns`, preserving that field's meaning. Because the registry is refreshed
by the sync sweep, a filter's scope is at most one sweep interval stale — a newly created or
newly tagged dataset enters scope on the next sweep.

**Breakdown format**: Every measurement result includes a `breakdown` JSONB with a
unified shape:

```
{"dataset_count": <total scanned>, "datasets": [{"urn": "...", "detail": {...}}]}
```

`datasets[]` lists **only failed datasets** — membership in the list is itself
the classification. It is the `met = false` subset of the run's verdicts. A dataset is
failed when:

- `ingestion-freshness`: the resolved ingestion evidence (tier 1 or tier 2 — see
  **Ingestion evidence** above) is older than `metric_conf.time_window_sec`, or absent on both
  tiers
- `validation-score`: latest validation `score` inside the window is `< 1.0`
  (or no result inside the window)
- `doc-health`: documentation score is `< 1.0` (table description missing OR any
  column description missing)

`detail` is optional, type-specific metadata. `ingestion-freshness` and `validation-score`
record the window applied at run time in `time_window_sec` — a metric's `metric_conf` can
change between runs, so a past result stays interpretable only if it carries its own window —
alongside `last_event_at` (freshness) or `latest_data_time` + `score` (validation-score).
`ingestion-freshness` additionally names
**which tier supplied `last_event_at`** in `evidence_tier` (`"observation"` for tier 1,
`"source_level"` for tier 2, `null` when neither tier produced evidence) — the two tiers make
different claims, so without it a stale verdict is not diagnosable. Tier 2's label names the
*grain*, not a producer: it is the newest `COMPLETE` on the owning source whatever wrote it. `dataset_count` is the total scanned
(matching `dataset_filter`),
not the number of failed entries; `len(datasets) == failed count` is implied. The
breakdown lets time-range queries on `attr/result` answer per-dataset historical
questions without re-running the metric.

**Factory defaults**: On API startup, an idempotent bootstrap inserts one
`metric_definitions` row for each built-in `metric_type` if absent. Defaults are
`mode="active"`, `is_enabled=false`, `schedule_tier="daily"`, `dataset_filter=""`,
a `metrics` descriptor per emitted key (each with a distinct color and an `idx` in emission
order), and type-appropriate `metric_conf` (`{"time_window_sec": 172800}` for the first two,
`{}` for `doc-health`). Seeds ship disabled so scheduled DAG runs are a no-op until the
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
    emitted by PUT, PATCH, DELETE on a configuration resource. `VALIDATION` is the
    exception: it emits `CONFIG_CREATE`/`CONFIG_UPDATE` but no `CONFIG_DELETE`, because
    deleting a validation conf hard-deletes the dataset's validation events as part of
    its cascade.
  - *Action*: domain-specific operations beyond CRUD (pipeline runs, approvals,
    state transitions).

### Event Catalogue

Config-lifecycle actions (`CONFIG_CREATE`, `CONFIG_UPDATE`, `CONFIG_DELETE`) are emitted
by every domain that owns a config — `METRIC`,
`ONTOGEN` (singleton), `METAGEN` (per conf, `entity_id=conf_id`). `INGESTION` names its
config-lifecycle events `SOURCE_CREATE`/`SOURCE_UPDATE`/`SOURCE_DELETE` instead, since its
configuration resource is a source. `VALIDATION` emits
`CONFIG_CREATE`/`CONFIG_UPDATE` but **no** `CONFIG_DELETE`: deleting a validation conf
hard-deletes the dataset's validation events as part of its cascade, so recording a
delete event would be self-defeating. Domain-specific actions:

| Domain (`entity_type`) | Action | Trigger |
|---|---|---|
| `INGESTION` (`ingestion_source`, `entity_id=source_id`) | `COMPLETE` / `FAIL` | An ingestion run completes, or the sync sweep observes that a dataset was ingested. Always booked on a source, never on the dataset; projected onto a dataset's timeline via reverse-lookup plus the `detail.dataset_urn` predicate (see [Querying Events](#querying-events)). Four producers, discriminated by `detail.source` — see [producers and `detail` vocabulary](#ingestioncomplete--ingestionfail-producers) below |
| `VALIDATION` (`dataset`) | `RESULT_RECORDED` | `POST attr/validation/result` succeeds (one event per accepted result) |
| `METAGEN` (`metagen`, `entity_id=conf_id`) | `RUN_COMPLETE` / `RUN_FAILED` | per-conf generation run end; `RUN_COMPLETE` recorded for both dry-run and non-dry-run, `dry_run` flag in detail. Detail keys: `run_id` (uuid4), `conf_id`, `conf_name`, `unresolved_urns` (list, same shape as METRIC), `counts` (dict — `items_considered`, `candidates_added`, `candidates_evicted`, `rejected_cleared` on real-run; `items_considered`, `candidates_proposed` on dry-run), `dry_run`, `producer_iterations`, `debate_outcome` (`accept` / `turns_exhausted` / `cycle_detected`) |
| `METAGEN` (`dataset`) | `CANDIDATE_APPROVE` / `CANDIDATE_REJECT` | `POST attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` with `verdict: "approve"\|"reject"`. Detail keys: `item_id`, `candidate_id`, `reason` |
| `METRIC` (`metric`) | `RUN_COMPLETE` | `POST method/run` succeeds. Detail keys: `run_id`, `metric_id`, `values` (dict[str,float] — the persisted result), `dry_run`, `unresolved_urns` (list — literal `dataset_urn` values in `dataset_filter` that matched no registered dataset), `breakdown_summary` (`{dataset_count, affected_count}`) |
| `ONTOGEN` (`ontogen`) | `SEED_CREATE` / `SEED_UPDATE` / `SEED_DELETE` | seed CRUD on `attr/seed/{seed_id}` |
| `ONTOGEN` (`ontogen`) | `RUN_COMPLETE` / `RUN_FAILED` | re-inference run end; `RUN_COMPLETE` recorded for both dry-run and non-dry-run, `dry_run` flag in detail. Detail keys: `run_id` (uuid4), `unresolved_urns` (list, same shape as METRIC), `counts` (dict — `nodes_added/edges_added/triples_added` on real-run, `nodes_proposed/edges_proposed/triples_proposed` on dry-run), `dry_run`, `producer_iterations` (inference-loop turns the Producer took), `producer_errors_dropped` (validator-rejected row count), `debate_outcome` (`accept` / `turns_exhausted` / `cycle_detected`) |
| `NODE` / `EDGE` / `TRIPLE` (`node` / `edge` / `triple`) | `APPROVE` / `REJECT` | `POST ontogen/result/{type}/{id}/method/review` |
| `AUTH` (`user`, `entity_id=user_id`) | `GOOGLE_UNBOUND` | `DELETE /admin/users/{id}/google` releases a binding ([AUTH §Admin unbind](AUTH.md#admin-unbind)). The route ends every session and removes an authentication method, so the event is the record that it happened; the request log carries no authenticated principal. Detail keys: `session_epoch` (the new value). Same no-secrets shape as the bind event — no `sub`, no hash. An idempotent call on an already-unbound row writes nothing and emits nothing. |
| `AUTH` (`user`, `entity_id=user_id`) | `GOOGLE_LINK_CREDENTIAL_RESET` | A Google identity binds onto an existing row matched by email, invalidating that row's credentials in the same transaction ([AUTH §Credential reset on link](AUTH.md#credential-reset-on-link)). Exactly one event per bind: the branch reaches only unbound rows, which `ck_users_auth_method` guarantees carry a password, so every bind clears at least that. Detail keys: `api_tokens_revoked` (int), `reset_tokens_deleted` (int), `session_epoch` (the new value). |
| `AUTH` (`user`, `entity_id=user_id` of the token's owner) | `API_TOKEN_REVOKED` | An admin revokes a token they do not own via `DELETE /admin/users/{id}/api-tokens/{token_id}` ([AUTH §Admin revoke audit](AUTH.md#admin-revoke-audit)). Setting `revoked_at` is the whole of what ends a token's life, so the write is the security event. Booked on the owner rather than the acting admin, so every credential a user loses lands on one timeline. The self-service `DELETE /auth/api-tokens/{id}` emits nothing. Detail keys: `token_id`, `owner_user_id`. No token name, hash, or prefix — same no-secrets shape as the other `AUTH` events. |

#### `INGESTION.COMPLETE` / `INGESTION.FAIL` producers

`detail.source` is the normative producer discriminator: every consumer that must tell a run apart
from an observation reads it, and it is the fourth term of the observation identity tuple.

| `detail.source` | Producer | Modes | Grain | Outcomes | Identity |
|---|---|---|---|---|---|
| *(key absent)* | inline run record written by `POST sources/{id}/method/run` | `ACTIVE_CUSTOM_MANAGED` | per run | `COMPLETE` + `FAIL` | run-local |
| `datahub_sync` | execution-request mirror, sweep run/observation-events step | `DATAHUB_MANAGED` | per run | `COMPLETE` + `FAIL` | `detail.execution_request_urn`, upserted |
| `passive_observation` | `Operation`-aspect observation, sweep run/observation-events step | `PASSIVE` | per dataset | `COMPLETE` only | (source, `detail.dataset_urn`, `occurred_at`, `detail.source`) |
| `last_ingested_observation` | `Dataset.lastIngested` observation, sweep run/observation-events step | all | per dataset | `COMPLETE` only | the same four-term tuple |

`detail` keys per producer:

| Producer | Keys |
|---|---|
| inline run record | `run_id`, `platform`, `dry_run`, `discovered_urns` (dataset URNs passing the recipe's selection patterns — the "would emit" plan, present on dry-run and real runs), `discovered_urns_count`, `emitted_urns` (dataset URNs written to DataHub; empty on dry-run), `emitted_urns_count`, `errors`, `warnings`; `emitted_urns ⊆ discovered_urns` |
| `datahub_sync` | `source`, `execution_request_urn` (the identity key, not merely informational), `duration_ms` |
| `passive_observation` | `source`, `dataset_urn`, `operation_type` (the qualifying `Operation.operationType`) |
| `last_ingested_observation` | `source`, `dataset_urn` |

Two invariants hold across the four, and consumers rest on them:

- **No run-level producer writes a scalar `detail.dataset_urn`.** The mirror carries no dataset
  link at all, and the inline record carries dataset URN *lists* (`discovered_urns` /
  `emitted_urns`) under different keys. That is what lets the per-dataset timeline admit run-level
  rows through an `IS NULL` disjunct while excluding a sibling dataset's observations — an
  equality-only predicate would delete precisely the run and `FAIL` rows from every timeline.
- **`detail.source` is absent, not null, on the inline record.** A consumer's producer filter must
  therefore treat a missing key as run-level; `detail->>'source'` on a missing key is SQL `NULL`,
  and a bare `NOT IN` silently drops those rows.

### Querying Events

- **Per-dataset timeline** (`GET .../data/{urn}/event`): the complete dataset feed.
  Unions the `entity_type="dataset"` events (validation + metagen) with the covering
  source's ingestion runs and its observations *for this dataset*, resolved by reverse-lookup
  plus the `detail.dataset_urn` predicate — see the
  [unified timeline aggregation](#dataset-service-srcbackenddataset) on the Dataset Service.
  Supports the repeatable `event_major_type` prefix filter (`INGESTION`/`VALIDATION`/`METAGEN`).
- **Domain-level endpoint** (`GET .../event`): filters by `event_type` prefix
  (e.g., `INGESTION.%`) to return only that domain's events.

For the per-source ingestion event endpoint (`GET /spoke/ingestion/sources/{id}/event`), the base
query unions the source's own `events` with those of its linked wrapper rows
(`entity_id IN (source_id, *child_wrapper_ids)`), ordered newest-first; count and pagination run off
the same base. Each returned row carries a **derived** `wrapper: bool` — `true` when the event's
`entity_id` is a wrapper rather than the regular source. The flag is computed at read time and is not
stored on the `events` row.

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
| `datahub-sync-hourly` | `datahub_sync_hourly.py` | Airflow schedule | `0 */2 * * *` |
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

`datahub-sync-hourly` runs on a 2-hour crontab; the `-hourly` suffix in its `dag_id`,
filename, and tags is a retained identifier, not a cadence claim. Because the sweep also
refreshes the dataset attributes every `dataset_filter` resolves against, its cadence is
the upper bound on filter-scope staleness across UC3, UC4, and UC5.

> **Tier-DAG selection**: For features with a `schedule_tier` field on a conf
> **collection** (`ingestion`, `metrics`, `metagen`), the periodic DAG that runs
> at a given tier fetches only the configs whose `schedule_tier` matches the DAG's
> tier and runs each. For `metagen`, the tier DAG calls
> `POST /internal/activities/metagen/run {tier}`, which fans out across every
> enabled conf at that tier, each under its own `metagen:running:{conf_id}` lock.
> For the singleton-conf `ontogen`, only the tier listed on the singleton conf
> runs at that tier (the other two tier DAGs short-circuit when triggered).
> When the singleton conf's tier matches but its `is_enabled` is `false`, the
> `ontogen` activity **skips** (returns `{status: "skipped", reason: "disabled"}`)
> rather than failing — a disabled conf is a no-op, not an error.

### Schedule Control

Every periodic DAG ships `is_paused_upon_creation=True`; nothing unpauses it
automatically. Operators control schedules through `GET`/`PATCH /admin/dags`,
which proxies Airflow's per-DAG `is_paused` flag. Airflow is the SSOT for paused
state — DataSpoke stores no copy (no DB column, no runtime-config field). The
routes expose six **groups**, each backed by a fixed list of member DAGs (the
group→DAG map is the single source of truth, owned by the admin DAG-control
service):

| `group` | Member DAGs |
|---------|-------------|
| `datahub_sync` | `datahub-sync-hourly` |
| `auth_role_sync` | `auth-role-sync-daily` |
| `ingestion_active` | `ingestion-active-hourly`, `ingestion-active-daily`, `ingestion-active-weekly` |
| `ontogen` | `ontogen-hourly`, `ontogen-daily`, `ontogen-weekly` |
| `metagen` | `metagen-hourly`, `metagen-daily`, `metagen-weekly` |
| `metrics` | `metrics-hourly`, `metrics-daily`, `metrics-weekly` |

`GET` reads paused state for all member DAGs in a single Airflow call and folds
each group: `paused` is `true` only when **all** members are paused; `mixed` is
`true` when members disagree. `PATCH /admin/dags/{group}` sets `is_paused` on
every member DAG of the group. An unknown group raises `404 DAG_GROUP_NOT_FOUND`;
an Airflow transport failure raises `503 AIRFLOW_UNAVAILABLE`. The on-demand
`metrics` DAG is not group-controllable. Paused state
is independent of conf-level enablement: pausing a group stops its schedule
entirely, while leaving it unpaused still skips disabled confs at run time.

### DataHub Sync

`POST /internal/admin/datahub/sync` reconciles `dataset_registry.datahub_registered` against
the live DataHub URN set. Accepts an optional `dataset_urns` list in the body
(null/omitted = full sweep). Flips the flag bidirectionally: sets it true when a URN is
found in DataHub, false when it has disappeared. Returns counts
`{checked, flipped_true, flipped_false, unchanged, not_found}`. This endpoint is the
**on-demand / scoped** path (e.g. validation's per-dataset precision check). Scheduled
full-estate reconciliation runs **every two hours** as part of the `datahub-sync-hourly` sweep
(the DAG drives it via `POST /internal/activities/ingestion/sync`, i.e.
`IngestionService.sync()` — not this admin endpoint), which
enumerates DataHub once and reconciles `dataset_registry` — inserting newly-seen URNs and
soft-flagging `datahub_registered` true/false — alongside the ingestion source→dataset mapping.
An empty (but successful) enumeration is treated as "no signal" and skips the deregister pass
so a transient zero-result search cannot mass-deregister the registry.

### DAG Verification

`POST /internal/admin/dags/verify` checks that every DAG ID in `ALL_DAG_IDS`
(see [DAG Catalogue](#dag-catalogue)) is registered with the in-cluster Airflow
deployment. Returns `{found, missing, total_expected}`. Used as a post-deploy
smoke check by the test fixture `tests/integration/conftest.py::airflow_client`.

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
| `ingestion` | `ingestion:running:{source_id}` | 1 hour |
| `ontogen` | `ontogen:running:singleton` | 1 hour |
| `metagen` | `metagen:running:{conf_id}` | 1 hour |
| `metrics` | `metrics:running:{metric_id}` | 1 hour |

**Airflow DAG run conf-based dedup** (for Airflow-orchestrated DAGs):

| DAG | Conf Key |
|-----|----------|
| `metrics` | `metrics-{metric_id}` |

If a duplicate is detected, the API returns `409 Conflict` with the appropriate `*_RUNNING`
error code (`METAGEN_RUNNING`, `METRIC_RUNNING`, `ONTOGEN_RUNNING`, …). The conf-based
dedup is enforced by `AirflowClient.check_no_duplicate()`, which queries running DAG runs
and rejects when a run with a matching `conf` key/value already exists.

**Airflow `max_active_runs`** caps concurrent runs per DAG independently of the locks
above: `ingestion-active-*` = 5, `metrics-*` = 2, and `metagen-*`/`ontogen-*`/
`datahub-sync-hourly`/`auth-role-sync-daily` = 1. Tier DAGs enumerate their work via
the `list-active` activity endpoints (`/internal/activities/{ingestion,metrics}/list-active`,
plus metagen's tier parameter) and fan out with Airflow `expand()`.

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
[DATAHUB_INTEGRATION §Event Subscription](../DATAHUB_INTEGRATION.md#event-subscription-not-used-by-baseline)
— extensions can register their own handlers without modifying baseline code.

The consumer runs as `python -m src.shared.datahub.consumer` in a separate Deployment
(`dataspoke-event-consumer`) when enabled. Uses `confluent-kafka` with manual offset
commit; deserialization failures are logged and skipped, handler failures leave the offset
uncommitted for redelivery.

**Execution model.** The consumer ships no image of its own — the Deployment runs the
**API image** with a `command:` override naming the module. The API image already carries
`src/` and the resolved virtualenv, and its Dockerfile uses `CMD` rather than `ENTRYPOINT`,
so the override is a clean substitution. One image is therefore built, tagged, scanned, and
promoted for both workloads, and the consumer can never drift to a different revision of
the shared code than the API it shares a database with.

### Kafka connection

The consumer reads its whole connection from `peripheral_config.datahub` — brokers plus the
security tuple defined in [API.md](../API.md#datahub-kafka-security) — and re-reads it every
few seconds while polling. A change to any element ends the inner poll loop, closes the
client, and rebuilds it. An unconfigured peripheral parks the process in a retry sleep
rather than crash-looping, recording no fault; a `peripheral_config` read that fails
outright — the database unreachable, or its schema not yet migrated — keeps the process
alive on the same retry sleep and reports the fault on the `datahub` `peripheral_health`
row on a best-effort basis. That row lives in the same database, so it surfaces only once
the database does. Consequently the entire credential-based configuration surface is live
and UI-driven — protocol, mechanism, username, and password all take effect without a
redeploy. `AWS_MSK_IAM` is the one exception: selecting it is a DB-plane change like any
other, but it authenticates with an identity the chart attaches at install time, so a
deployment whose ServiceAccount carries no IAM role cannot be fixed from the admin API
(see [HELM_CHART §Event-consumer identity and RBAC](HELM_CHART.md#event-consumer-identity-and-rbac)).

The tuple maps onto `confluent-kafka` client properties
([librdkafka configuration](https://github.com/confluentinc/librdkafka/blob/master/CONFIGURATION.md)):

| Peripheral field | Client property |
|---|---|
| `kafka_brokers` | `bootstrap.servers` |
| `kafka_security_protocol` | `security.protocol` |
| `kafka_sasl_mechanism` (`PLAIN`, `SCRAM-SHA-*`) | `sasl.mechanism` |
| `kafka_sasl_username` / the `kafka_sasl_password` Secret key | `sasl.username` / `sasl.password` |
| `kafka_sasl_mechanism = AWS_MSK_IAM` | `sasl.mechanism=OAUTHBEARER` plus a token-refresh callback; `security.protocol` passes through as the stored `SASL_SSL` |

`AWS_MSK_IAM` is not a librdkafka mechanism. AWS implements it as OAUTHBEARER whose token is
an SigV4-signed payload minted by
[`aws-msk-iam-sasl-signer-python`](https://github.com/aws/aws-msk-iam-sasl-signer-python)
from whatever credentials the process resolves — on EKS, the pod's IRSA-projected role. The
library is baked into the API image because it must be present before any DB-plane
configuration can select the mechanism; it is the one part of Kafka security that belongs to
the build plane rather than the DB plane.

The signer requires a region. It comes from `kafka_aws_region` when set, otherwise from the
broker hostname, which for MSK encodes it (`…kafka.<region>.amazonaws.com`, and the
`kafka-serverless` form likewise). The
derivation **anchors to the end of the host**, so a suffix-extended lookalike does not match.
When neither source resolves, the consumer fails loudly and reports the reason rather than
guessing a region and producing an opaque authentication failure.

**The consumer re-validates the protocol/mechanism combination when it builds a client**,
instead of trusting the stored row to satisfy the API's rules. `peripheral_config` is a
plain table that direct SQL or dev seeding can write behind the API, and the same
re-check-on-read convention already guards the display-link fields this table serves to
`/spoke/common/peripheral-links`. A row that fails re-validation is treated as a
configuration error and reported on the `datahub` `peripheral_health` row — the consumer does not
attempt the connection. This matters most for `AWS_MSK_IAM`, where the broker-host and
protocol constraints in [API.md](../API.md#datahub-kafka-security) are what keep the pod's
IAM identity from being pointed somewhere it was never granted for.

`kafka_sasl_password_version` exists because a rotated password is invisible in the DB row —
the value lives in the Secret. The API increments the counter whenever it writes the Secret,
which turns a rotation into an ordinary DB-plane change the poll loop already detects.

---

## Health reporting

DataSpoke reaches DataHub over two independent transports. Each has its own `peripheral_health`
row, written by the process that exercises that transport:

| Row | Plane | Reporter | Meaning |
|---|---|---|---|
| `datahub` | Event stream (Kafka MCL topics) | the DataHub event consumer | `ok` once subscribed and polling; `error` with the message on any fault it reports — connection, authentication, configuration, or a failed read of its own configuration |
| `datahub-api` | Metadata API (GMS REST / GraphQL) | the `datahub-sync-hourly` sync + mapping sweep ([Ingestion Service](#ingestion-service-srcbackendingestion)) | `ok` on a completed sweep; `error` on any failure that escapes it |

`GET /admin/peripherals/datahub` returns the first as `health` and the second as `api_health`.
On either row `unknown` covers both "never reported" and "no reporter deployed". **Both
reporters are opt-in, so `unknown` is the ordinary reading on a stock install**: the event
consumer is not deployed by chart default, and the sweep runs from a scheduled DAG that ships
paused, so `datahub-api` stays `unknown` until an operator unpauses the `datahub_sync` group
(see [§Schedule Control](#schedule-control)). Neither row reads `unknown` as a fault.

**A reporter's own write failure is swallowed** — reporting never changes the outcome of the
operation being reported — and logged at `ERROR` with `exc_info=True`. The level is deliberate:
`peripheral_health` is itself the operator-facing fault surface, so a lost write leaves the row
at its prior value, where a stale or `unknown` reading is indistinguishable from the "no reporter
deployed" case above — the surface silently loses the fault it exists to expose. The log record
is then the only evidence that a deployed reporter is running and failing. The
[best-effort operations](#best-effort-operations) logged at WARNING sit outside that surface:
their failure degrades a single operation, not the operator's view of the system. That section
carries one exception, the `api_tokens.last_used_at` stamp, which logs at `ERROR` — see its row
for why. Like `last_error` below, this binds every reporter writing the table, not only the two
DataHub rows.

The two planes need a persisted row for different reasons. The **event stream** has no other
HTTP surface at all: a bad mechanism or an unauthorized IAM role leaves a consumer that logs
warnings nobody reads, so without the row the fault is unobservable. The **metadata API** is
already probed live by `GET /ready` ([API §System](../API.md#system)), but that is a
point-in-time boolean for kubelet and ingress probes — no history, no failure message, no
operator context. `peripheral_health` is instead the persisted, operator-facing record
(`last_error`, `last_ok_at`) rendered beside the configuration that caused the fault.

`last_error` is bounded and credential-free. This binds every reporter writing the table —
`langfuse` and `smtp` as much as the two DataHub rows — because it is a property of the column,
not of one plane. The read is Admin-only rather than a 502/503 body, so
[DATAHUB_INTEGRATION §Resilience Conventions](../DATAHUB_INTEGRATION.md#resilience-conventions)
rule 7 does not apply literally, but the same discipline holds: no credentials, no stack
traces, and a length bound, so a persisted message cannot become a disclosure or log-forging
surface.

**Two rows, not one.** The planes use separate transports and credentials and fail
independently: Kafka can be unreachable while GMS serves fine, and the reverse. A single shared
row would let the consumer and the sweep overwrite each other's verdict, so an operator would
read whichever reporter wrote last rather than either plane's health — strictly worse than an
honest `unknown`.

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
| `ConflictError` | 409 | `DUPLICATE_CONFIG`, `INGESTION_RUNNING`, `INGESTION_SOURCE_READONLY`, `INGESTION_RUN_NOT_APPLICABLE`, `METAGEN_RUNNING`, `METRIC_RUNNING`, `ONTOGEN_RUNNING`, `METAGEN_DISABLED`, `METRIC_DISABLED`, `ONTOGEN_DISABLED` |
| `DataHubUnavailableError` | 502 | `DATAHUB_UNAVAILABLE` |
| `PeripheralNotConfiguredError` | 503 | `PERIPHERAL_NOT_CONFIGURED` |
| `StorageUnavailableError` | 503 | `STORAGE_UNAVAILABLE` |
| `ValidationError` (Pydantic), `RequestValidationError` (FastAPI routing-layer validation) | 422 | `INVALID_PARAMETER`, `INVALID_DATASET_URN` |
| `PreconditionFailedError` | 422 | `DATASET_NOT_IN_DATAHUB`, `ONTOGEN_TRIPLE_DEPENDENCY_PENDING`, `UNKNOWN_VARIABLE`, `INVALID_SCORE` |

Error response format matches [API](../API.md#error-catalogue). Exception hierarchy is
defined in `src/shared/exceptions.py`.

### Best-Effort Operations

Non-critical operations execute best-effort -- if they fail, the primary operation's local
state stays durable; the caller may still receive an error (see each row's Fallback).
Failures of the operations listed below are logged at WARNING with `exc_info=True`; a
reporter's failure to write its own `peripheral_health` row falls outside this set and is
logged at `ERROR` (see [§Health reporting](#health-reporting)). One listed row takes the
same exception: the `api_tokens.last_used_at` stamp is logged at `ERROR` with
`exc_info=True`, because nothing reads that column in band, so the log record is the only
trace of a lost stamp — what a reader may then conclude from the column is stated in
[AUTH §Audit and `last_used_at`](AUTH.md#audit-and-last_used_at).

| Operation | Service | Fallback |
|-----------|---------|----------|
| `assertionRunEvent` emission | ValidationService | Row stays in `validation_results` (local store remains the historical-baseline cache); caller receives `502/503` so the pipeline can decide whether to retry |
| pgvector similarity search | MetagenService | Reviewer proceeds without prior-approved-candidate RAG; debate quality drops but the run completes |
| DataHub run-history poll | IngestionService (sync sweep) | Skip the affected source for this tick; retry next tick |
| Estate-wide `lastIngested` read and its per-dataset observation inserts | IngestionService (sync sweep) | The sub-pass books nothing this tick and reports `last_ingested_observed = 0`; the other two sub-passes, the rest of the sweep, and the `datahub-api` health row are untouched; retry next tick |
| Estate-wide dataset attribute read | IngestionService (sync sweep) | No row is blanked — stored attributes and their `attrs_synced_at` stand, so every `dataset_filter` keeps resolving against the last good sweep; `attrs_synced` reports what did land; the `datahub-api` health row is **not** flipped, so a filter's staleness is read from `attrs_synced_at` rather than from peripheral health; retry next tick |
| `api_tokens.last_used_at` throttled stamp | PAT authentication | The column keeps its prior value; authentication succeeds and the request proceeds. Logged at `ERROR` per the exception in the lead-in above, not at WARNING |

**Interface violations are exempt from best-effort, on the estate-wide `lastIngested` read.** An
`AttributeError` or `TypeError` raised by that client call is a fault in DataSpoke's own call
shape — a renamed or removed method — not a fault of the remote system, because the read is a
fixed-shape traversal of a GraphQL response in which every element is shape-checked. Those are
logged at `ERROR` and **re-raised**; only transport, protocol and database faults degrade it to its
fallback. The split matters because a swallowed interface error reports `last_ingested_observed = 0`
forever, indistinguishable from an estate with nothing observable, and a duck-typed test double
missing the method passes green with the sub-pass never executing.

The exemption stops there. The per-dataset `Operation` read is **not** exempt: it deserialises a
writer-supplied remote aspect through the acryl-datahub SDK, which raises `AttributeError` on a
malformed stored payload, so an error of that type is not evidence of a call-shape fault and one
corrupted aspect would abort the sweep for every source. Every failure of that read skips the
dataset.

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
| `EMBEDDING_DIMENSION` | 1536 | Vector dimension (matches LLM model) |
| `ONTOLOGY_CONFIDENCE_THRESHOLD` | 0.7 | Ontogen: below this -> row persists as `llm_pending` |
| `METAGEN_CONFIDENCE_THRESHOLD` | 0.7 | Metagen: below this -> candidate is dropped (metagen has no `llm_pending`). Default only — the live value is the runtime-tunable `runtime_config.metagen_confidence_threshold` (`PATCH /admin/conf`), not a static constant |

---

## Authentication & User Account Management

The identity, lifecycle, and DataHub-projection doctrine live in
[AUTH](AUTH.md). The route catalogue and JWT claim shape live in
[API §Authentication & Authorization](../API.md#authentication--authorization).
DataHub-side primitives (corpuser/corpGroup/role aspects, GraphQL mutations,
`hard_delete_entity`) live in
[DATAHUB_INTEGRATION §User & Role Management](../DATAHUB_INTEGRATION.md#user--role-management).
This section captures the service-layer composition only.

### Service Modules (`src/backend/auth/`)

| Module | Responsibility |
|--------|---------------|
| `users.py` | DataSpoke user repository — create / read / update name / update password / hard delete; reads and writes `users.role`. bcrypt via the `bcrypt` library at cost factor 12. Binds a Google `sub` onto an existing row, and in the same transaction clears `password_hash`, revokes the user's active `api_tokens`, deletes their unused `password_reset_tokens`, and increments `session_epoch` ([AUTH §Credential reset on link](AUTH.md#credential-reset-on-link)). UNIQUE(email) → `409 EMAIL_ALREADY_REGISTERED`; UNIQUE(google_sub) → `GOOGLE_ACCOUNT_LINKED_ELSEWHERE`, rolling the whole reset back with it. Also the admin unbind — clears `google_sub` and increments `session_epoch`, refusing a row with no `password_hash` (`409 GOOGLE_IS_ONLY_AUTH_METHOD`) per [AUTH §Admin unbind](AUTH.md#admin-unbind). |
| `tokens.py` | JWT issue / refresh / revoke. Refresh-token revocation list in Redis under `revoked_refresh:{sha256[:16]}`. Access-token claims are `sub`, `email`, `exp`, `iat`, `ses` (the issuing session epoch); role is **not** in the JWT (read from `users.role` per request, on the read that also resolves the epoch). |
| `api_tokens.py` | Long-lived opaque API token CRUD. Mint generates `dsk_<token_urlsafe(32)>`, stores SHA-256 hash in `api_tokens.token_hash`, snapshots `users.role` into `role_snapshot`. Enforces 10-token-per-user cap (`409 TOKEN_LIMIT_EXCEEDED`). On lookup: computes `effective_role = min(role_snapshot, users.role)`; stamps `last_used_at` best-effort on a session of its own ([§Privilege Enforcement](#privilege-enforcement)). Revoke sets `revoked_at = now()`. Three list scopes: own, one user's, and the deployment-wide admin inventory. The two admin scopes join `users` for the owner email and express their filters (`user_id`, `include_revoked`), ordering, and paging in SQL; the self scope has neither filter, and sorts and slices its result in Python — defensible against the 10-active-token cap that bounds it ([AUTH §API Tokens](AUTH.md#api-tokens)). |
| `oauth_google.py` | Google OAuth handler via `authlib.integrations.starlette_client`. State cookie (random opaque, HMAC-signed with `DATASPOKE_OAUTH_STATE_SECRET`) + ID-token `nonce` validation. On callback: resolve by Google `sub`; else by email, which binds only onto an **unbound** row (`google_sub IS NULL`) and drives the [credential reset](AUTH.md#credential-reset-on-link) plus its `AUTH.GOOGLE_LINK_CREDENTIAL_RESET` event in the bind transaction, refreshing `name` from the Google claim, logs in without writing when the row under the lock already carries this same `sub` (a raced or retried callback), and refuses a row carrying a different `sub` with `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT`; else create. |
| `reset.py` | Password-reset token issuance (256-bit `secrets.token_urlsafe`, SHA-256 hashed into `password_reset_tokens`) and confirm. Email transport via `aiosmtplib` driven by the SMTP peripheral (below). |
| `privilege.py` | The `require_role(...)` FastAPI dependency family. Reads caller's role from `users.role` (or `min(role_snapshot, users.role)` for API tokens). Method × tier matrix enforcement per [AUTH §Privilege Model](AUTH.md#privilege-model). |

The errors these modules raise on the Google-OAuth path never reach a client as an
error envelope: the two `/auth/google/*` routes are browser-navigation endpoints
and translate any raised error into a 302 to the frontend's `/oauth-error` page
([AUTH §Callback failure
surface](AUTH.md#callback-failure-surface)). The service layer is unaware of the
translation — it raises the same exception types as every other route.

### DataHub Projection (`src/backend/datahub/users.py`)

Single module wrapping the SDK + GraphQL primitives catalogued in
[DATAHUB_INTEGRATION §User & Role Management](../DATAHUB_INTEGRATION.md#user--role-management).
It has no corpuser-create primitive — corpusers come from DataHub's OIDC JIT
provisioning ([AUTH §DataHub Projection Semantics](AUTH.md#datahub-projection-semantics)):

- `corpuser_exists(corpuser_urn)` — existence probe matching DataHub `RoleService`'s `exists()` predicate (entity key plus `Status`; soft-deleted counts as absent). Guards the reconciliation loop's projection writes only; the role write-through on `PATCH /admin/users/{id}/role` calls `propagate_role` unguarded and best-effort. See the silent-skip rationale in [DATAHUB_INTEGRATION §Corpuser provenance](../DATAHUB_INTEGRATION.md#corpuser-provenance).
- `corpuser_urn(email)` — derives `urn:li:corpuser:<email>` from the **lowercased** email; `users.email` is `CITEXT` (case-preserving) while the URN is case-sensitive.
- `read_native_group_membership(corpuser_urn)` — SDK `get_aspect(corpuser_urn, NativeGroupMembershipClass)`, returning the user's current group URNs.
- `ensure_marker_group_exists()` — idempotent `emit_mcp(corpGroupInfo + Status)` using the group name read from `runtime_config.auth_datahub_corp_group`. Called once per reconciliation pass, before the per-user loop.
- `add_user_to_marker_group(corpuser_urn)` — GraphQL `addGroupMembers`.
- `propagate_role(corpuser_urn, role)` — GraphQL `batchAssignRole`. Called on the admin role-change write-through and by the reconciliation pass, both gated on the user's row carrying a `google_sub` ([AUTH §Identity-binding requirement](AUTH.md#identity-binding-requirement)). DataHub-side is a projection; DataSpoke `users.role` is the SSOT.
- `read_role(corpuser_urn)` — SDK `get_aspect(corpuser_urn, RoleMembershipClass)` (atomic single-role per DataHub `RoleService`); the `IsMemberOfRole` GraphQL relationship index is **not** used because it lags MCL→ES indexing. **Used only by the nightly reconciliation DAG**, not on the request hot path.
- `hard_delete_corpuser(corpuser_urn)` — SDK `hard_delete_entity`.

The module never writes `corpUserInfo` or `corpUserCredentials`.

### Registration Composition

`POST /auth/register` is a DataSpoke-local transaction with no DataHub step:

1. `users.create()` (DataSpoke DB) with `role = 'Reader'`.
2. `tokens.issue()` → 200 with access JWT + refresh cookie.

Registration therefore succeeds regardless of DataHub availability or
configuration. Role and marker-group membership reach DataHub through the
nightly reconciliation pass once the user's corpuser exists
([AUTH §Projection contract](AUTH.md#projection-contract)).

The Google OAuth callback and `POST /internal/admin/bootstrap` use the same
composition — local row plus token issuance — differing only in the role
assigned and in how the identity is resolved.

### Role-Change Composition

`PATCH /admin/users/{id}/role` orchestrates a two-step write where DataSpoke
is SSOT:

1. `users.update_role(user_id, new_role)` — DataSpoke `users.role` updated.
2. `datahub.users.propagate_role(corpuser_urn, new_role)` — DataHub projection, **skipped entirely when the row has no `google_sub`**. Effectively a no-op when the user is bound but has no corpuser yet; the nightly pass projects the role once one exists.

If step 2 fails, the API returns `200` to the admin caller (DataSpoke-side
state is correct), logs a warning, and relies on the nightly
`auth-role-sync-daily` DAG to reassert the role on DataHub. No compensating
action on the DataSpoke side — divergence is by definition DataSpoke-correct.

### Credential-Reset Composition

The Google-callback email branch binds and invalidates in **one** DataSpoke
transaction, with no DataHub step
([AUTH §Credential reset on link](AUTH.md#credential-reset-on-link)):

1. `users.bind_google_sub(user_id, sub)` — sets `google_sub`, clears
   `password_hash`, increments `session_epoch`.
2. `api_tokens` revoke-all for the user (`revoked_at = now()` where
   `revoked_at IS NULL`).
3. Delete the user's unused `password_reset_tokens` rows.
4. Record the `AUTH.GOOGLE_LINK_CREDENTIAL_RESET` event.

All four commit together or not at all: a partial reset would leave a live
re-entry path on a row that has already changed hands. Token issuance follows
the commit, so the session the callback returns reads the new epoch.

**Ordering against concurrent credential-creating writes.** Step 1 takes the
`users` row lock, and the four self-service writes that mint a credential —
the `password` field of `PATCH /auth/me`, `POST /auth/api-tokens`,
`POST /auth/password/reset/confirm`, and `POST /auth/password/reset/request` —
take that same lock before re-validating their own authorisation against the
row they just read
([AUTH §Serialization of credential-creating
writes](AUTH.md#serialization-of-credential-creating-writes)). None of them can
therefore commit ahead of the reset, and none can commit a credential
authorised by state the reset superseded — the two JWT-authorised writes fail
the `ses` re-comparison, the confirm path finds its reset-token row gone, and
the request path observes the epoch move and returns `204` without writing a
token row.

### Privilege Enforcement

The `require_role` dependency family in `src/backend/auth/privilege.py`
implements the [Privilege Model](AUTH.md#privilege-model) matrix:

- `require_authenticated` — JWT decode or API-token lookup; populates
  `request.state.user` and `request.state.effective_role`.
- `require_writer` — used on `/spoke/*` write methods (POST /
  PUT / PATCH / DELETE). Rejects with `403 READ_ONLY_ROLE` if
  `effective_role == "Reader"`.
- `require_admin` — used on `/admin/*`. Rejects with `403 FORBIDDEN` if
  `effective_role != "Admin"`.

GET / HEAD / OPTIONS on `/spoke/*` use `require_authenticated`
only. `/auth/*` writes use `require_authenticated` only (the method gate is
exempt — self-scoped writes).

The `effective_role` is computed once per request, from a user read that also
carries the session epoch:

- JWT-authenticated request: `SELECT role, session_epoch FROM users WHERE id =
  sub` (one DB round trip, shares the request's DB session). A token whose
  `ses` claim is absent or unequal to `session_epoch` is rejected
  `401 UNAUTHORIZED` before any role gate runs — the epoch check costs no
  additional query because the role read already fetches the row
  ([AUTH §Session epoch](AUTH.md#session-epoch)).
- API-token-authenticated request: `SELECT t.role_snapshot, u.role FROM
  api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?`,
  then `effective_role = min(t.role_snapshot, u.role)` with ordering
  `Admin > Editor > Reader`. Returns `401 INVALID_API_TOKEN` /
  `401 TOKEN_REVOKED` / `401 TOKEN_EXPIRED` on the token state checks. This
  branch runs no epoch check — an API token carries no `ses`, and a credential
  reset revokes the rows themselves.

`last_used_at` is stamped by a separate `UPDATE`, issued and committed on its
own session after the token-state checks have passed. The stamp is best-effort —
a failure to write it never fails the authentication it follows, and is logged at
`ERROR` (see [§Best-Effort Operations](#best-effort-operations)). The throttle it
carries and what a reader may conclude from the column live in
[AUTH §Audit and `last_used_at`](AUTH.md#audit-and-last_used_at).

### Deletion Composition

`DELETE /admin/users/{id}` runs the
[projection retraction sequence](AUTH.md#projection-retraction-sequence):

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
