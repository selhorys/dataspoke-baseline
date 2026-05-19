# DataSpoke Backend — Data Contracts

> This document specifies the storage contracts shared across all DataSpoke
> backend processes (API server, Airflow activity endpoints, event consumers):
> PostgreSQL tables (including pgvector embeddings) and related indexes. The
> Apache AGE extension is also installed on the same PostgreSQL instance and
> is available as reserved graph infrastructure for future use.
>
> Companion to [BACKEND](BACKEND.md) (service logic, workflows, shared clients).
> Architecture context in [ARCHITECTURE](../ARCHITECTURE.md).

---

## Table of Contents

1. [PostgreSQL Schema](#postgresql-schema)
2. [Vector Tables (pgvector)](#vector-tables-pgvector)

---

## PostgreSQL Schema

All DataSpoke operational data lives in PostgreSQL. DataHub remains the metadata
SSOT; PostgreSQL stores configurations, run results, events, ontology graph, and
metric definitions that DataHub does not natively model.

### Schema: `dataspoke`

All tables are created in the `dataspoke` schema. Managed by Alembic migrations
in `migrations/`.

### Tables

#### `ingestion_configs`

Stores per-dataset ingestion configuration.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Config identifier |
| `dataset_urn` | `TEXT` UNIQUE | Target dataset URN |
| `mode` | `TEXT` | `active-custom` (DataSpoke's in-house extractor runs on a tier schedule) or `passive` (external pipeline ingests; DataSpoke mirrors run history via the hourly `ingestion-passive-hourly` DAG observing `DataProcessInstance` aspects in DataHub) |
| `platform` | `TEXT` | DataHub platform name (`postgres`, `kafka`, `mysql`, `bigquery`, etc.) |
| `locator` | `JSONB` NULL | Infrastructure location (e.g., `{"host", "port"}` for RDBMS); `active-custom` only — null for `passive` (passive ingestors handle their own connectivity out-of-band) |
| `identifier` | `JSONB` | Dataset identifier within the infra (e.g., `{"database", "schema_name", "table"}`) |
| `auth` | `JSONB` NULL | Access credentials (e.g., `{"username", "secret_ref"}`); `active-custom` only — null for `passive` (passive ingestors handle their own auth out-of-band) |
| `is_enabled` | `BOOLEAN` | Enable scheduled execution via Airflow (`active-custom` mode) or scheduled status sync (`passive` mode) |
| `schedule_tier` | `TEXT` NULL | Schedule tier for `active-custom` mode — `hourly`, `daily`, or `weekly` (required when `mode='active-custom'` and `is_enabled=true`); null for `passive` mode |
| `workflow_dag_id` | `TEXT` NULL | Airflow DAG ID of the assigned periodic DAG (`active-custom` mode only) |
| `status` | `TEXT` | `OK` (DAG verification succeeded), `ERROR` (verification failed) |
| `created_at` | `TIMESTAMPTZ` | Creation timestamp |
| `updated_at` | `TIMESTAMPTZ` | Last modification |

#### `dataset_registry`

Tracks dataset URNs referenced by DataSpoke configs and whether they exist in DataHub.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `datahub_registered` | `BOOLEAN` | `true` after successful (non-dry-run) ingestion to DataHub |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

- **Creation**: lazy, via `ensure_dataset_registered()` on ingestion config upsert. Validation config upsert checks the registry but does not create rows — `validation_configs` references `dataset_urn` directly, and the validation precondition gate (`422 DATASET_NOT_IN_DATAHUB`) reads from this registry.
- **Updates**: `mark_registered()` called from `IngestionService.run()` on successful non-dry-run;
  `mark_unregistered()` reserved for DataHub sync.
- **DataHub sync**: bidirectional reconciliation against DataHub via
  `POST /internal/admin/datahub/sync` (manual/scripted) and the `datahub-sync-daily` Airflow DAG.
- **SSOT**: DataHub is authoritative for dataset existence;
  the registry caches state for the validation precondition gate.

#### `validation_configs`

Stores the single validation slot per dataset (passive result-store model — see
[`spec/feature/VALIDATION.md`](VALIDATION.md)). One row per dataset.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Target dataset URN (unique — at most one validation slot per dataset) |
| `description` | `TEXT` | Free-form description (≤ 2,000 chars; surfaced in DataHub assertion detail UI) |
| `variables` | `TEXT[]` | Declared variable names the pipeline will report. Each entry matches `[a-z][a-z0-9_]{0,99}`, unique within the row, 1..200 entries. Joined as `customAssertion.logic` on DataHub emit |
| `is_removed` | `BOOLEAN` | Mirror of DataHub `status.removed` for query convenience. `true` after `DELETE`; `false` after a subsequent `PUT` resurrection |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `validation_results`

Pipeline-emitted timeseries results (one row per `POST /attr/validation/result`).
Also emitted to DataHub as `assertionRunEvent`. Append-only.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Row identifier |
| `dataset_urn` | `TEXT` | Target dataset (FK shape; matches `validation_configs.dataset_urn`) |
| `data_time` | `TIMESTAMPTZ` | Time the underlying data is for (typically the partition timestamp). Maps to `assertionRunEvent.timestampMillis` and is the timeseries axis for `GET ?from=&until=` |
| `score` | `DOUBLE PRECISION` | `0.0 ≤ score ≤ 1.0` (CHECK constraint). `1.0` = pass, `0.0` = fail; intermediate values reserved for partial-success semantics |
| `variables` | `JSONB` | Map of variable name → numeric value. Keys must be a subset of `validation_configs.variables` (validated at the service layer; `422 UNKNOWN_VARIABLE` on violation) |
| `ingestion_time` | `TIMESTAMPTZ` | Server-side `now()` when the row was accepted (audit trail; preserved separately from `data_time`) |

Indexes: `(dataset_urn, data_time DESC)` to serve the historical-baseline GET.

Multiple rows may share `(dataset_urn, data_time)` — append-only matches DataHub's
timeseries aspect semantics. The GET endpoint collapses duplicates with last-write-wins
per distinct `data_time`.

#### `metagen_config`

Singleton row holding the Metadata Generation conf (UC4).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK (=1) | Singleton row |
| `is_enabled` | `BOOLEAN` | Master switch for the metagen DAG |
| `schedule_tier` | `TEXT` NULL | `hourly`, `daily`, or `weekly` re-generation cadence. When null, no periodic DAG runs; manual `POST /method/run` is unaffected |
| `dataset_filter` | `JSONB` | Optional scope filter — `{"tags": [...], "glossary_terms": [...], "dataset_urns": [...]}`; OR-ed across dimensions; `{}` = all. Same shape as `ontogen_config.dataset_filter` and `metric_definitions.dataset_filter` (the latter adds an AND-ed `origin` dimension) |
| `result_limit` | `INTEGER` | Max non-rejected candidates per item (range `[1, 20]`, default `3`) |
| `overwrite_pending` | `BOOLEAN` | When the per-item budget is full and the item has no `approved` candidate, true = evict oldest `llm_approved` candidate; false = skip the item (default true) |
| `updated_at` | `TIMESTAMPTZ` | |

A `CHECK (id = 1)` constraint enforces singleton.

#### `metagen_boundary`

Per-dataset opt-in boundary for UC4 metagen. Absence of a row, or a row with
`is_enabled=false`, means the dataset is excluded regardless of the global
`dataset_filter`.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Target dataset URN |
| `is_enabled` | `BOOLEAN` | When true, this dataset participates in global metagen |
| `allowed` | `TEXT[]` | Element kinds the global generator may write — subset of `{"dataset.description", "column.description"}` |
| `owner` | `TEXT` NULL | Owner user ID |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metagen_items`

One row per (dataset, item slot). Materialized lazily by each run for in-scope
(dataset, allowed kind) pairs. The item carries identity only — whether it
currently has an approved candidate is derived from the sibling rows in
`metagen_candidates`.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` | Target dataset URN |
| `item_id` | `TEXT` | `dataset.description` or `column.<fieldPath>.description` |
| `kind` | `TEXT` | `dataset.description` or `column.description` |
| `field_path` | `TEXT` NULL | Schema field path (set for `column.description`; null otherwise) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

Primary key: `(dataset_urn, item_id)`.

#### `metagen_candidates`

One row per generated candidate value. Candidates accumulate per item across
runs up to `metagen_config.result_limit`; `rejected` rows are deleted at the
start of the next run.

| Column | Type | Description |
|--------|------|-------------|
| `candidate_id` | `UUID` PK | Candidate identifier |
| `dataset_urn` | `TEXT` | Target dataset URN |
| `item_id` | `TEXT` | Item this candidate belongs to (FK `(dataset_urn, item_id)` → `metagen_items`) |
| `run_id` | `UUID` | The metagen run that produced this candidate |
| `value` | `TEXT` | Markdown proposal (≤ 16 KiB) |
| `confidence_score` | `REAL` | Producer-Reviewer debate confidence (`[0.0, 1.0]`) |
| `status` | `TEXT` | `llm_approved` (debate-accepted, awaiting human), `approved` (human accepted, emitted to DataHub), `rejected` (human rejected, deleted next run) |
| `evidence` | `JSONB` | Debate transcript (same shape as ontogen `evidence`) plus per-item Reviewer verdicts |
| `created_at` | `TIMESTAMPTZ` | |
| `reviewed_at` | `TIMESTAMPTZ` NULL | Human review timestamp |
| `reviewer_id` | `TEXT` NULL | User ID of the reviewer |

Indexes: `(dataset_urn, item_id, status, created_at)` for FIFO eviction
queries and per-item budget checks; `(run_id)` for run-scoped cleanup.

A partial unique index `UNIQUE (dataset_urn, item_id) WHERE status='approved'`
enforces the invariant that an item has at most one `approved` candidate at
any time. Approving a sibling un-approves the previously-approved one in the
same transaction (see [BACKEND §Metadata Generation Service](BACKEND.md#metadata-generation-service-srcbackendmetagen)).

#### `metagen_candidate_embeddings`

Vector embeddings of `approved` candidate `value`s. Used by the Reviewer's
RAG anchor pool in subsequent runs (see
[BACKEND_LLM §Metagen Adversarial Debate](BACKEND_LLM.md#metagen-adversarial-debate)).

| Column | Type | Description |
|--------|------|-------------|
| `candidate_id` | `UUID` PK | FK to `metagen_candidates` |
| `kind` | `TEXT` | `dataset.description` or `column.description` (cached for filtered KNN) |
| `embedding` | `VECTOR` | pgvector embedding of the candidate's `value` |

HNSW index on `embedding` with cosine distance.

#### `ontogen_config`

Singleton row holding the Ontology Generation conf (UC3).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK (=1) | Singleton row |
| `is_enabled` | `BOOLEAN` | Master switch for the inference DAG |
| `schedule_tier` | `TEXT` NULL | `hourly`, `daily`, or `weekly` re-inference cadence. When null, no periodic DAG runs; manual `POST /method/run` is unaffected |
| `dataset_filter` | `JSONB` | Optional scope filter — `{"tags": [...], "glossary_terms": [...], "dataset_urns": [...]}`; OR-ed across dimensions; `{}` = all. Same shape as `metric_definitions.dataset_filter` (the latter adds an AND-ed `origin` dimension) |
| `default_run_prompt` | `TEXT` NULL | Markdown string used as the one-shot prompt for runs without an explicit body (periodic Airflow DAG; bodyless manual `POST /method/run`); null disables |
| `updated_at` | `TIMESTAMPTZ` | |

#### `ontogen_seeds`

Human-authored Markdown documents that steer the inference pipeline. The endpoint
accepts and returns raw Markdown (`Content-Type: text/markdown`); only metadata is
managed out-of-band.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Server-assigned `seed_id` |
| `body_md` | `TEXT` | Raw Markdown body |
| `status` | `TEXT` | `active` or `retired` |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `ontogen_nodes`

Subjects / objects of the ontology — business concepts rooted in one or more datasets.
There is no parent/child hierarchy.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Node identifier (slug, e.g. `book`, `customer`, `order_line`); `__` is forbidden (reserved as triple-ID separator) |
| `name` | `TEXT` UNIQUE | Node display name |
| `description` | `TEXT` | LLM-generated description |
| `confidence_score` | `REAL` | LLM inference confidence (0.0–1.0) |
| `status` | `TEXT` | `llm_pending`, `llm_approved`, `approved`, `rejected` — `llm_pending` is the LLM-created default; `llm_approved` is set when the Adversarial Debate ends with `outcome=accept` and confidence ≥ `ONTOLOGY_CONFIDENCE_THRESHOLD`; `approved` and `rejected` are written only by the human review endpoint |
| `evidence` | `JSONB` NULL | Snapshot of LLM evidence (signals from each input source) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `dataset_node_map`

Maps datasets to nodes with confidence scores.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `node_id` | `TEXT` PK, FK → `ontogen_nodes(id)` | Node |
| `confidence_score` | `REAL` | LLM classification confidence (0.0–1.0) |
| `status` | `TEXT` | `llm_pending`, `llm_approved`, `approved`, `rejected` — same vocabulary as `ontogen_nodes`; cascaded from the parent node row on human review |
| `is_primary` | `BOOLEAN` | True for the primary (authoritative) member dataset of the node |
| `created_at` | `TIMESTAMPTZ` | |

#### `ontogen_edges`

Predicates / relationship types — the verb vocabulary used by triples. An edge stands
on its own and is reused across many triples.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Edge identifier (slug, e.g. `references`, `placed_by`); `__` is forbidden (reserved as triple-ID separator) |
| `label` | `TEXT` UNIQUE | Edge display label |
| `semantics` | `TEXT` NULL | LLM-generated short semantics description |
| `confidence_score` | `REAL` | LLM inference confidence (0.0–1.0) |
| `status` | `TEXT` | `llm_pending`, `llm_approved`, `approved`, `rejected` — same semantics as `ontogen_nodes.status` |
| `evidence` | `JSONB` NULL | Snapshot of LLM evidence |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `ontogen_triples`

`(subject_node, edge, object_node)` facts. A triple may only be approved when both
endpoint nodes (FK to `ontogen_nodes`) and the edge (FK to `ontogen_edges`) are
themselves `approved`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Triple identifier — composite slug `{subject_node_id}__{edge_id}__{object_node_id}` (e.g. `order_line__references__book`); enforced to equal the concatenation of the three FK columns below |
| `subject_node_id` | `TEXT` FK → `ontogen_nodes(id)` | Subject node |
| `edge_id` | `TEXT` FK → `ontogen_edges(id)` | Predicate edge |
| `object_node_id` | `TEXT` FK → `ontogen_nodes(id)` | Object node |
| `confidence_score` | `REAL` | LLM inference confidence |
| `status` | `TEXT` | `llm_pending`, `llm_approved`, `approved`, `rejected` — same semantics as `ontogen_nodes.status`. Human approval is gated on all three component rows (`subject_node_id`, `edge_id`, `object_node_id`) being `approved`; an LLM-approved component does NOT satisfy the gate |
| `evidence` | `JSONB` NULL | Snapshot of LLM evidence |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metric_definitions`

Governance metric definitions.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Metric identifier (slug, e.g. `ingestion-freshness`, `validation-score`, `doc-health`) |
| `mode` | `TEXT` | `active` (built-in measurer runs the computation) or `passive` (reserved — PUT rejected with `501 NOT_IMPLEMENTED` in this release) |
| `metric_type` | `TEXT` | One of `ingestion-freshness`, `validation-score`, `doc-health` |
| `title` | `TEXT` | Display title |
| `description` | `TEXT` | What this metric measures |
| `metrics` | `JSONB` | List of `values` keys the metric persists — subset of the type's emitted keys (e.g. `["total", "ingested_in_time"]`) |
| `metric_conf` | `JSONB` | Type-specific config — `{"time_window_sec": <int>}` for `ingestion-freshness` / `validation-score`; `{}` for `doc-health` |
| `dataset_filter` | `JSONB` | `{"origin": "...", "tags": [...], "glossary_terms": [...], "dataset_urns": [...]}`. `origin` is a DataHub `FabricType` value (`PROD`/`DEV`/`CORP`/`EI`/`STG`/`NON_PROD`/…) — passed through to DataHub; the other three dimensions OR-ed among themselves and AND-ed with `origin`; `{}` = all datasets |
| `is_enabled` | `BOOLEAN` | Whether scheduled measurement is enabled |
| `schedule_tier` | `TEXT` NULL | Schedule tier for scheduled measurement — `hourly`, `daily`, or `weekly` (null = on-demand only) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metric_results`

Timeseries of metric measurements.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `metric_id` | `TEXT` FK | Metric definition |
| `values` | `JSONB` | Measured values — dict of named floats, e.g. `{"total": 142.0, "ingested_in_time": 87.0}` |
| `breakdown` | `JSONB` NULL | Measurement breakdown: `{dataset_count, datasets: [{urn, detail?}]}`. `datasets[]` carries only failed entries (stale / validation `<1.0` / doc-health `<1.0` depending on `metric_type`); `dataset_count` is the total scanned |
| `measured_at` | `TIMESTAMPTZ` | Measurement timestamp |

#### `events`

Unified event log for all feature domains. All events share the same top-level
structure so clients can process them generically (see
[API §Meta-Classifier Conventions](../API.md#meta-classifier-conventions)).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Event identifier |
| `entity_type` | `TEXT` | `dataset`, `metric`, `node`, `edge`, `triple`, `ontogen` (singleton conf + seeds) — classifies the entity, not the feature domain |
| `entity_id` | `TEXT` | URN or metric/node/edge/triple ID; for `entity_type='ontogen'` either the literal string `singleton` (conf) or a `seed:{seed_id}` form (seed events) |
| `event_type` | `TEXT` | Uppercase, dot-delimited `{DOMAIN}.{ACTION}` (e.g., `INGESTION.COMPLETE`, `METRIC.RUN_COMPLETE`, `NODE.APPROVE`, `TRIPLE.APPROVE`, `METAGEN.CANDIDATE_APPROVE`, `METAGEN.RUN_COMPLETE`, `ONTOGEN.RUN_COMPLETE`). Full catalogue in [BACKEND §Event Catalogue](BACKEND.md#event-catalogue). |
| `status` | `TEXT` | `success`, `failure`, `warning` |
| `detail` | `JSONB` | Event-specific payload |
| `occurred_at` | `TIMESTAMPTZ` | Event timestamp |

**Filtering convention**: `entity_type` identifies what the entity *is* (a
dataset, a metric, an ontology node / edge / triple, the ontogen singleton).
Ingestion, validation, and metadata generation are *attributes* of a dataset, so
their events use `entity_type=dataset`. The dataset-level event endpoint
(`GET .../data/{urn}/event`) filters by `entity_type=dataset` to return all
event types for that dataset. Sub-resource event endpoints (e.g.,
`.../event/ingestion`, `.../event/metagen`) additionally filter by `event_type`
prefix (e.g., `INGESTION.%`, `METAGEN.%`) to return only domain-specific events.
The Ontology Generation singleton uses `entity_type=ontogen` and `entity_id='singleton'`
(conf) or `entity_id='seed:{seed_id}'` (seed events) for the global event log surfaced
at `/spoke/common/ontogen/event`; per-result events use `entity_type=node|edge|triple`
and the corresponding ID.

#### `department_mapping`

Maps DataHub ownership URNs to organizational departments (used by metrics
aggregation when an HR API is unavailable).

| Column | Type | Description |
|--------|------|-------------|
| `owner_urn` | `TEXT` PK | DataHub owner URN |
| `department` | `TEXT` | Department name |
| `updated_at` | `TIMESTAMPTZ` | |

### Indexes

| Table | Index | Purpose |
|-------|-------|---------|
| `validation_results` | `(dataset_urn, data_time DESC)` | Time-range queries on results (historical-baseline GET) |
| `metagen_candidates` | `(dataset_urn, item_id, status, created_at)` | Per-item FIFO eviction and budget checks |
| `metagen_candidates` | `(run_id)` | Run-scoped cleanup |
| `metric_results` | `(metric_id, measured_at DESC)` | Time-range queries on measurements |
| `events` | `(entity_type, entity_id, occurred_at DESC)` | Event log queries per entity |
| `dataset_node_map` | `(node_id)` | Node-to-datasets lookup |
| `ontogen_triples` | `(subject_node_id)`, `(object_node_id)`, `(edge_id)` | Triple lookup by any participant |

---

## Vector Tables (pgvector)

Vector similarity search is backed by the `vector` extension (pgvector) on the
same PostgreSQL instance as the operational tables. No separate vector database
is deployed.

### `dataset_embeddings`

Primary table for natural language search and similarity matching. Lives in the
`dataspoke` schema. Created by Alembic migration `001_initial_schema`.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `embedding` | `vector(EMBEDDING_DIMENSION)` NOT NULL | Embedding vector; dimension fixed at table creation time (provider-determined, e.g. 1536) |
| `platform` | `TEXT` | Data platform (`oracle`, `postgres`, etc.) |
| `tags` | `JSONB` | DataHub tag URNs |
| `owners` | `JSONB` | Owner URNs |
| `quality_score` | `REAL` NULL | Best-effort cached quality score |
| `has_pii` | `BOOLEAN` | PII classification flag |
| `updated_at` | `TIMESTAMPTZ` NOT NULL | Last sync timestamp |

**Index**: `dataset_embeddings_embedding_hnsw_idx` — HNSW over `embedding` with
`vector_cosine_ops`. Similarity query expression:
`GREATEST(0.0, 1.0 - (embedding <=> :query_vector::vector))`.

**Embedding input**: Concatenation of dataset name, description, field names +
descriptions, tags, and lineage context. Processed through the LLM embedding
endpoint.

**Sync triggers**:
- Scheduled: refreshed by the matching `ontogen-{hourly,daily,weekly}` tier DAG when it
  re-runs UC3 inference on the configured `schedule_tier`
- On-demand: rebuilt by a manual `POST /spoke/common/ontogen/method/run` (synchronous, in-process)
- Optional event-driven extension (not enabled in baseline): Kafka MCL events for
  `datasetProperties` / `schemaMetadata` / `globalTags` changes — see
  [DATAHUB_INTEGRATION §Event Subscription](../DATAHUB_INTEGRATION.md#event-subscription-optional-not-used-by-baseline)

**Access wrapper**: `src/shared/vector/client.py` exposes `PgVectorManager`
(session-factory backed) returning `VectorHit` dataclasses. Collection name is
whitelisted against `EMBEDDING_COLLECTION` to prevent arbitrary table access.

### `node_embeddings`

Embeddings over ontology nodes for similarity recall. Used by Ontology Generation
inference to detect when a candidate node duplicates an already-approved node so the
existing node ID can be reused (incremental inference; see
[BACKEND §Inference Pipeline](BACKEND.md#ontology-generation-service-srcbackendontogen)).
Lives in the `dataspoke` schema.

| Column | Type | Description |
|--------|------|-------------|
| `node_id` | `TEXT` PK FK → `ontogen_nodes(id)` | Node identifier |
| `embedding` | `vector(EMBEDDING_DIMENSION)` NOT NULL | Embedding vector; dimension fixed at table creation time (provider-determined, e.g. 1536) |
| `name` | `TEXT` | Cached node name (denormalised for query convenience) |
| `status` | `TEXT` | Cached node status (`llm_pending`, `llm_approved`, `approved`, `rejected`) — RAG-anchor lookups filter to `status IN ('approved','llm_approved')`; reuse lookups accept all non-`rejected` statuses |
| `updated_at` | `TIMESTAMPTZ` NOT NULL | Last embedding refresh |

**Index**: `node_embeddings_embedding_hnsw_idx` — HNSW over `embedding` with
`vector_cosine_ops`. Similarity query expression:
`GREATEST(0.0, 1.0 - (embedding <=> :query_vector::vector))`.

**Embedding input**: Concatenation of node name, description, and the
schemas / descriptions of its member datasets. Processed through the LLM embedding
endpoint.

**Sync triggers**:
- Refreshed by the matching `ontogen-{hourly,daily,weekly}` tier DAG: every approved node whose
  `node_embeddings.updated_at` precedes `ontogen_nodes.updated_at` is re-embedded
- On-demand: rebuilt by a manual `POST /spoke/common/ontogen/method/run` when name
  or description changed for an approved node

### `edge_embeddings`

Embeddings over ontology edges (predicates) for similarity recall. Used by the
Adversarial Debate Reviewer to sample RAG anchors over approved edges (see
[BACKEND_LLM §RAG anchors](BACKEND_LLM.md#rag-anchors)). Lives in the
`dataspoke` schema.

| Column | Type | Description |
|--------|------|-------------|
| `edge_id` | `TEXT` PK FK → `ontogen_edges(id)` | Edge identifier |
| `embedding` | `vector(EMBEDDING_DIMENSION)` NOT NULL | Embedding vector; dimension fixed at table creation time |
| `label` | `TEXT` | Cached edge display label (denormalised for query convenience) |
| `status` | `TEXT` | Cached edge status; RAG-anchor lookups join through `ontogen_edges` and filter to `status IN ('approved','llm_approved')` |
| `updated_at` | `TIMESTAMPTZ` NOT NULL | Last embedding refresh |

**Index**: `edge_embeddings_embedding_hnsw_idx` — HNSW over `embedding` with
`vector_cosine_ops`.

**Embedding input**: Concatenation of edge label and semantics. Processed through
the LLM embedding endpoint.

**Sync triggers**: Refreshed by the same `ontogen-{hourly,daily,weekly}` tier
DAG or manual `POST /spoke/common/ontogen/method/run` that refreshes `node_embeddings`.

### `triple_embeddings`

Embeddings over ontology triples for similarity recall. Used by the Adversarial
Debate Reviewer to sample RAG anchors over approved triples. Lives in the
`dataspoke` schema.

| Column | Type | Description |
|--------|------|-------------|
| `triple_id` | `TEXT` PK FK → `ontogen_triples(id)` | Triple identifier |
| `embedding` | `vector(EMBEDDING_DIMENSION)` NOT NULL | Embedding vector; dimension fixed at table creation time |
| `status` | `TEXT` | Cached triple status; RAG-anchor lookups join through `ontogen_triples` and filter to `status IN ('approved','llm_approved')` |
| `updated_at` | `TIMESTAMPTZ` NOT NULL | Last embedding refresh |

**Index**: `triple_embeddings_embedding_hnsw_idx` — HNSW over `embedding` with
`vector_cosine_ops`.

**Embedding input**: Composite text of subject node (name + description), edge
(label + semantics), and object node (name + description). Processed through
the LLM embedding endpoint.

**Sync triggers**: Refreshed by the same `ontogen-{hourly,daily,weekly}` tier
DAG or manual `POST /spoke/common/ontogen/method/run` that refreshes `node_embeddings`.

### `metagen_candidate_embeddings`

Embeddings over `approved` UC4 metagen candidate `value`s. Used by the
Metagen Adversarial Debate Reviewer to sample RAG anchors of prior
human-approved descriptions of the same kind (see
[BACKEND_LLM §Metagen Adversarial Debate](BACKEND_LLM.md#metagen-adversarial-debate)).
Lives in the `dataspoke` schema.

| Column | Type | Description |
|--------|------|-------------|
| `candidate_id` | `UUID` PK FK → `metagen_candidates(candidate_id)` | Candidate identifier |
| `embedding` | `vector(EMBEDDING_DIMENSION)` NOT NULL | Embedding vector; dimension fixed at table creation time |
| `kind` | `TEXT` | Cached item kind (`dataset.description` or `column.description`) for filtered KNN |
| `updated_at` | `TIMESTAMPTZ` NOT NULL | Last embedding refresh |

**Index**: `metagen_candidate_embeddings_embedding_hnsw_idx` — HNSW over
`embedding` with `vector_cosine_ops`.

**Embedding input**: The candidate's Markdown `value`. Processed through the
LLM embedding endpoint.

**Sync triggers**: Inserted at the moment a candidate flips to
`status='approved'` (synchronous with the DataHub emit). Deleted only when
the candidate row is deleted (which baseline metagen never does for
approved rows).

### Graph (Apache AGE, reserved)

The `age` extension is installed and preloaded (`shared_preload_libraries = 'age'`),
and `ag_catalog` usage is granted to the application role. This is reserved graph
infrastructure available to any service that opts in via the shared `AgeGraph`
client (see [BACKEND §Shared Services](BACKEND.md#shared-services-srcshared)).

