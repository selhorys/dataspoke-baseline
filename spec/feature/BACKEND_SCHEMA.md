# DataSpoke Backend — Data Contracts

> This document specifies the storage contracts shared across all DataSpoke
> backend processes (API server, Airflow activity endpoints, event consumers):
> PostgreSQL tables (including pgvector embeddings) and related indexes.
> The same PostgreSQL instance also has the Apache AGE extension installed.
> The Ontology Generation service materialises `ontogen_triples` as graph edges in
> AGE for traversal queries; the relational tables remain the source of truth.
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

- **Creation**: lazy, via `ensure_dataset_registered()` on ingestion/validation config upsert.
- **Updates**: `mark_registered()` called from `IngestionService.run()` on successful non-dry-run;
  `mark_unregistered()` reserved for DataHub sync.
- **DataHub sync**: bidirectional reconciliation against DataHub via
  `POST /internal/admin/datahub/sync` (manual/scripted) and the `datahub-sync-daily` Airflow DAG.
- **SSOT**: DataHub is authoritative for dataset existence;
  the registry caches state for the validation precondition gate.

#### `validation_configs`

Stores per-dataset validation configuration (assertion rules + schedule).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Config identifier |
| `dataset_urn` | `TEXT` UNIQUE | Target dataset URN |
| `rules` | `JSONB` | JSON list of assertion rules (DataHub Open Assertions Spec compatible, extended with `rule_id`, `partition`, `order`, `ml_validation`) |
| `is_enabled` | `BOOLEAN` | Enable Airflow tier-based periodic execution (default false) |
| `schedule_tier` | `TEXT` NULL | Schedule tier — `hourly`, `daily`, or `weekly` (required when `is_enabled=true`) |
| `owner` | `TEXT` | Owner user ID |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `validation_results`

Per-rule, per-partition results from validation runs.
Also reported to DataHub as `assertionRunEvent`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `dataset_urn` | `TEXT` | Target dataset |
| `rule_id` | `TEXT` | Rule identifier from config |
| `partition` | `JSONB` | Target partition (e.g., `{"load_date": "2025-03-10"}`) |
| `values` | `JSONB` | Computed values for the partition (e.g., `{"row_count": 48230, "null_rate": 0.003}`) |
| `validation` | `JSONB` NULL | ML validation verdicts per target (e.g., `{"null_rate": true}`) |
| `assertion_result` | `TEXT` | `SUCCESS`, `FAILURE`, or `ERROR` |
| `issues` | `JSONB` | Array of rule-specific issue objects |
| `run_id` | `UUID` | Airflow DAG run ID |
| `measured_at` | `TIMESTAMPTZ` | Measurement timestamp |

#### `metagen_configs`

Stores per-dataset metadata generation configuration (UC4).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Config identifier |
| `dataset_urn` | `TEXT` UNIQUE | Target dataset URN |
| `targets` | `JSONB` | List of target documentation fields. Baseline values: `dataset.description`, `column.description`, `cross_data.md` |
| `code_refs` | `JSONB` NULL | GitHub repo/file references for code analysis |
| `is_enabled` | `BOOLEAN` | Enable scheduled execution via Airflow |
| `schedule_tier` | `TEXT` NULL | Schedule tier for periodic runs — `hourly`, `daily`, or `weekly` (required when `is_enabled=true`) |
| `status` | `TEXT` | `draft` |
| `owner` | `TEXT` | Owner user ID |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metagen_results`

Historical metadata generation proposals, pending review. Each row represents one
generation run for one dataset.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `dataset_urn` | `TEXT` | Target dataset |
| `proposals` | `JSONB` | Per-field proposals keyed by target — `dataset.description`, `column.description.{fieldPath}`, `cross_data.md` (the latter holds an ordered list of `{action_id, action: create\|modify\|split\|retitle, ...}` items; `action_id` is the stable string used to reference an individual action via `cross_data.md.<action_id>` in PATCH `fields`) |
| `field_status` | `JSONB` | Per-field review status — keyed identically to `proposals`, value is `pending` / `approved` / `rejected` / `edited`. Field-level review (a single PATCH may approve a subset) updates only the listed entries. |
| `run_id` | `UUID` | Airflow DAG run ID |
| `generated_at` | `TIMESTAMPTZ` | |
| `last_reviewed_at` | `TIMESTAMPTZ` NULL | Last PATCH timestamp |

#### `ontogen_config`

Singleton row holding the Ontology Generation conf (UC3).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK (=1) | Singleton row |
| `is_enabled` | `BOOLEAN` | Master switch for the inference DAG |
| `schedule_tier` | `TEXT` NULL | `hourly`, `daily`, or `weekly` re-inference cadence (required when `is_enabled=true`) |
| `dataset_filter` | `JSONB` | Optional scope filter — `{"tags": [...], "glossary_terms": [...], "dataset_urns": [...]}`; OR-ed across dimensions; `{}` = all. Same shape as `metric_definitions.measurement_query.dataset_filter` |
| `max_manual_queries_per_dataset` | `INTEGER` | Per-dataset cap on `source = MANUAL` Query entities fed to the LLM. CHECK ≥ 0; default `20`; `0` disables |
| `max_system_queries_per_dataset` | `INTEGER` | Per-dataset cap on `source = SYSTEM` Query entities (multi-asset joins only). CHECK ≥ 0; default `10`; `0` disables |
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
| `status` | `TEXT` | `approved`, `pending_review`, `rejected` |
| `glossary_term_urn` | `TEXT` NULL | DataHub glossary term URN attached on approval; `null` while pending |
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
| `status` | `TEXT` | `approved`, `pending` (pending if confidence < `ONTOLOGY_CONFIDENCE_THRESHOLD`) |
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
| `status` | `TEXT` | `approved`, `pending_review`, `rejected` |
| `evidence` | `JSONB` NULL | Snapshot of LLM evidence |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `ontogen_triples`

`(subject_node, edge, object_node)` facts. Triples are also materialised in Apache AGE
for graph queries; this relational table is the source of truth for review status. A
triple may only be approved when both endpoint nodes (FK to `ontogen_nodes`) and the
edge (FK to `ontogen_edges`) are themselves `approved`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Triple identifier — composite slug `{subject_node_id}__{edge_id}__{object_node_id}` (e.g. `order_line__references__book`); enforced to equal the concatenation of the three FK columns below |
| `subject_node_id` | `TEXT` FK → `ontogen_nodes(id)` | Subject node |
| `edge_id` | `TEXT` FK → `ontogen_edges(id)` | Predicate edge |
| `object_node_id` | `TEXT` FK → `ontogen_nodes(id)` | Object node |
| `confidence_score` | `REAL` | LLM inference confidence |
| `status` | `TEXT` | `approved`, `pending_review`, `rejected` |
| `evidence` | `JSONB` NULL | Snapshot of LLM evidence |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metric_definitions`

Governance metric definitions.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Metric identifier (slug, e.g. `ingestion-freshness`, `validation-score`) |
| `title` | `TEXT` | Display title |
| `description` | `TEXT` | What this metric measures |
| `theme` | `TEXT` | Category: `quality`, `governance`, `freshness` |
| `measurement_query` | `JSONB` | `{"aggregation": "pct_fresh"\|"pct_rules_passing"\|..., "dataset_filter": {"tags": [...], "glossary_terms": [...], "dataset_urns": [...]}}`; `dataset_filter` dimensions OR-ed; `{}` = all datasets |
| `is_enabled` | `BOOLEAN` | Whether scheduled measurement is enabled |
| `schedule_tier` | `TEXT` NULL | Schedule tier for scheduled measurement — `hourly`, `daily`, or `weekly` (required when `is_enabled=true`) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metric_results`

Timeseries of metric measurements.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `metric_id` | `TEXT` FK | Metric definition |
| `value` | `REAL` | Measured numeric value |
| `breakdown` | `JSONB` NULL | Measurement breakdown: dataset list and per-type details |
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
| `event_type` | `TEXT` | Uppercase, dot-delimited `{DOMAIN}.{ACTION}` (e.g., `INGESTION.COMPLETE`, `METRIC.RUN_COMPLETE`, `NODE.APPROVE`, `TRIPLE.APPROVE`, `METAGEN.APPROVE`, `ONTOGEN.RUN_COMPLETE`). Full catalogue in [BACKEND §Event Catalogue](BACKEND.md#event-catalogue). |
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

#### `overview_config`

Singleton configuration for the multi-perspective overview visualization.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK (=1) | Singleton row |
| `layout` | `TEXT` | Graph layout algorithm (`force`, `hierarchical`, `radial`) |
| `color_by` | `TEXT` | Node coloring dimension (`quality_score`, `freshness`, `platform`) |
| `filters` | `JSONB` | Active filters (platforms, departments, tags) |
| `updated_at` | `TIMESTAMPTZ` | |

### Indexes

| Table | Index | Purpose |
|-------|-------|---------|
| `validation_results` | `(dataset_urn, measured_at DESC)` | Time-range queries on results |
| `metagen_results` | `(dataset_urn, generated_at DESC)` | Time-range queries on results |
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
- On-demand: rebuilt as part of an `ontogen` manual run
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
| `status` | `TEXT` | Cached node status (`approved`, `pending_review`, `rejected`) — reuse lookups filter to `approved` |
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
- On-demand: rebuilt as part of an `ontogen` manual run when name or description
  changed for an approved node

### Graph (Apache AGE, reserved)

The `age` extension is installed and preloaded (`shared_preload_libraries = 'age'`),
and `ag_catalog` usage is granted to the application role. The Ontology Generation
service materialises `ontogen_triples` as `(subject_node)-[edge]->(object_node)` edges
in an AGE graph for cross-node graph traversal queries (used by the governance
overview's ontology-graph view). The relational `ontogen_triples` table remains the
source of truth for review status; AGE is the read-side replica for graph-shaped
queries.
