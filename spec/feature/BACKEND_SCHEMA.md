# DataSpoke Backend — Data Contracts

> This document specifies the storage contracts shared across all DataSpoke
> backend processes (API server, Airflow activity endpoints, event consumers):
> PostgreSQL tables (including pgvector embeddings) and related indexes.
> The same PostgreSQL instance also has the Apache AGE extension installed for
> future graph workloads; no AGE tables are defined yet.
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
| `mode` | `TEXT` | `active` (DataSpoke runs the extractor) or `passive` (external pipeline ingests; DataSpoke mirrors run history via the hourly `datahub-ingestion-status-sync` DAG) |
| `platform` | `TEXT` | DataHub platform name (`postgres`, `kafka`, `mysql`, `bigquery`, etc.) |
| `locator` | `JSONB` | Infrastructure location (e.g., `{"host", "port"}` for RDBMS) |
| `identifier` | `JSONB` | Dataset identifier within the infra (e.g., `{"database", "schema_name", "table"}`) |
| `auth` | `JSONB` NULL | Access credentials (e.g., `{"username", "secret_ref"}`); null for ambient auth or passive mode |
| `is_active` | `BOOLEAN` | Enable scheduled execution via Airflow (active mode) or scheduled status sync (passive mode) |
| `schedule_tier` | `TEXT` NULL | Schedule tier for active mode — `hourly`, `daily`, or `weekly` (required when `mode='active'` and `is_active=true`); null for passive mode |
| `workflow_dag_id` | `TEXT` NULL | Airflow DAG ID of the assigned periodic DAG (active mode only) |
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
| `is_active` | `BOOLEAN` | Enable cron-triggered periodic execution (default false) |
| `schedule_tier` | `TEXT` NULL | Schedule tier — `hourly`, `daily`, or `weekly` (required when `is_active=true`) |
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
| `is_active` | `BOOLEAN` | Enable scheduled execution via Airflow |
| `schedule_tier` | `TEXT` NULL | Schedule tier for periodic runs — `hourly`, `daily`, or `weekly` (required when `is_active=true`) |
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
| `proposals` | `JSONB` | Per-field proposals keyed by target — `dataset.description`, `column.description.{fieldPath}`, `cross_data.md` (the latter holds an ordered list of `{action: create\|modify\|split\|retitle, ...}` items) |
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
| `sources` | `JSONB` | Input sources — at minimum `["datahub_aspects"]`; optional `sql_logs`, `github_repos`, `external_docs` |
| `dataset_filter` | `JSONB` | Optional scope filter — `{"tags": [...], "glossary_terms": [...]}`; same shape as `metric_definitions.measurement_query.dataset_filter` |
| `updated_at` | `TIMESTAMPTZ` | |

#### `concepts`

Single-level peer concepts. Concepts are not nested — there is no parent/child
hierarchy in the baseline ontology.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Concept identifier (slug, e.g. `book`, `customer`, `order_line`) |
| `name` | `TEXT` UNIQUE | Concept display name |
| `description` | `TEXT` | LLM-generated concept description |
| `confidence_score` | `REAL` | LLM classification confidence (0.0–1.0) |
| `status` | `TEXT` | `approved`, `pending_review`, `rejected` |
| `glossary_term_urn` | `TEXT` NULL | DataHub glossary term URN attached on approval; `null` while pending |
| `evidence` | `JSONB` NULL | Snapshot of LLM evidence (signals from each input source) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `dataset_concept_map`

Maps datasets to peer concepts with confidence scores.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `concept_id` | `TEXT` PK, FK | Concept |
| `confidence_score` | `REAL` | LLM classification confidence (0.0–1.0) |
| `status` | `TEXT` | `approved`, `pending` (pending if confidence < `ONTOLOGY_CONFIDENCE_THRESHOLD`) |
| `is_primary` | `BOOLEAN` | True for the primary (authoritative) member dataset of the concept |
| `created_at` | `TIMESTAMPTZ` | |

#### `concept_relationships`

Cross-concept relationships (edges in the ontology graph). Relationships are also
materialised in Apache AGE for graph queries; this relational table is the source of
truth for review status.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Relationship identifier |
| `concept_a` | `TEXT` FK | Source concept |
| `concept_b` | `TEXT` FK | Target concept |
| `relationship_type` | `TEXT` | LLM-inferred edge label (e.g. `references`, `placed_by`) |
| `confidence_score` | `REAL` | LLM inference confidence |
| `status` | `TEXT` | `approved`, `pending`, `rejected` |
| `created_at` | `TIMESTAMPTZ` | |

#### `metric_definitions`

Governance metric definitions.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Metric identifier (slug, e.g. `ingestion-freshness`, `validation-score`) |
| `title` | `TEXT` | Display title |
| `description` | `TEXT` | What this metric measures |
| `theme` | `TEXT` | Category: `quality`, `governance`, `freshness` |
| `measurement_query` | `JSONB` | `{"aggregation": "pct_fresh"\|"pct_rules_passing"\|..., "dataset_filter": {"tags": [...], "glossary_terms": [...]}}` |
| `is_active` | `BOOLEAN` | Whether scheduled measurement is active |
| `schedule_tier` | `TEXT` NULL | Schedule tier for scheduled measurement — `hourly`, `daily`, or `weekly` (required when `is_active=true`) |
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
| `entity_type` | `TEXT` | `dataset`, `metric`, `concept`, `ontogen` (singleton conf) — classifies the entity, not the feature domain |
| `entity_id` | `TEXT` | URN or metric/concept ID; for `entity_type='ontogen'` the literal string `singleton` |
| `event_type` | `TEXT` | Uppercase, dot-delimited `{DOMAIN}.{ACTION}` (e.g., `INGESTION.COMPLETE`, `METRIC.RUN_COMPLETE`, `CONCEPT.APPROVE`, `METAGEN.APPROVE`, `ONTOGEN.RUN_COMPLETE`). Full catalogue in [BACKEND §Event Catalogue](BACKEND.md#event-catalogue). |
| `status` | `TEXT` | `success`, `failure`, `warning` |
| `detail` | `JSONB` | Event-specific payload |
| `occurred_at` | `TIMESTAMPTZ` | Event timestamp |

**Filtering convention**: `entity_type` identifies what the entity *is* (a
dataset, a metric, a concept, the ontogen singleton). Ingestion, validation,
and metadata generation are *attributes* of a dataset, so their events use
`entity_type=dataset`. The dataset-level event endpoint
(`GET .../data/{urn}/event`) filters by `entity_type=dataset` to return all
event types for that dataset. Sub-resource event endpoints (e.g.,
`.../event/ingestion`, `.../event/metagen`) additionally filter by `event_type`
prefix (e.g., `INGESTION.%`, `METAGEN.%`) to return only domain-specific events.
The Ontology Generation singleton uses `entity_type=ontogen` and `entity_id='singleton'`
for run-level events surfaced at `/spoke/common/ontogen/event`; per-concept events use
`entity_type=concept` and the concept ID.

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
| `dataset_concept_map` | `(concept_id)` | Concept-to-datasets lookup |
| `concept_relationships` | `(concept_a)`, `(concept_b)` | Edge lookup in either direction |

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
- Scheduled: refreshed by the `ontogen-periodic-*` tier DAG when it re-runs UC3 inference
  on the configured `schedule_tier`
- On-demand: rebuilt as part of an `ontogen` manual run
- Optional event-driven extension (not enabled in baseline): Kafka MCL events for
  `datasetProperties` / `schemaMetadata` / `globalTags` changes — see
  [DATAHUB_INTEGRATION §Event Subscription](../DATAHUB_INTEGRATION.md#event-subscription-optional-not-used-by-baseline)

**Access wrapper**: `src/shared/vector/client.py` exposes `PgVectorManager`
(session-factory backed) returning `VectorHit` dataclasses. Collection name is
whitelisted against `EMBEDDING_COLLECTION` to prevent arbitrary table access.

### Graph (Apache AGE, reserved)

The `age` extension is installed and preloaded (`shared_preload_libraries = 'age'`),
and `ag_catalog` usage is granted to the application role. The Ontology Generation
service materialises `concept_relationships` as edges in an AGE graph for cross-concept
graph traversal queries (used by the governance overview's ontology-graph view). The
relational `concept_relationships` table remains the source of truth for review status;
AGE is the read-side replica for graph-shaped queries.
