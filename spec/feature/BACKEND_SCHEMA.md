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
| `platform` | `TEXT` | DataHub platform name (`postgres`, `mysql`, `bigquery`, etc.) |
| `locator` | `JSONB` | Infrastructure location (e.g., `{"host", "port"}` for RDBMS) |
| `identifier` | `JSONB` | Dataset identifier within the infra (e.g., `{"database", "schema_name", "table"}`) |
| `auth` | `JSONB` NULL | Access credentials (e.g., `{"username", "secret_ref"}`); null for ambient auth |
| `is_active` | `BOOLEAN` | Enable scheduled execution via Airflow |
| `schedule_tier` | `TEXT` NULL | Schedule tier — `hourly`, `daily`, or `weekly` (required when `is_active=true`) |
| `enrichment_sources` | `JSONB` NULL | External enrichment source configs (TBD) |
| `custom_extractors` | `JSONB` NULL | Custom extractor plugin configs (TBD) |
| `workflow_dag_id` | `TEXT` NULL | Airflow DAG ID of the assigned periodic DAG |
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

#### `generation_configs`

Stores per-dataset doc generation configuration.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Config identifier |
| `dataset_urn` | `TEXT` UNIQUE | Target dataset URN |
| `target_fields` | `JSONB` | Fields to generate (description, tags, deprecation) |
| `code_refs` | `JSONB` NULL | GitHub repo/file references for code analysis |
| `is_active` | `BOOLEAN` | Enable scheduled execution via Airflow |
| `schedule_tier` | `TEXT` NULL | Schedule tier for periodic runs — `hourly`, `daily`, or `weekly` (required when `is_active=true`) |
| `status` | `TEXT` | `draft` |
| `owner` | `TEXT` | Owner user ID |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `generation_results`

Historical generation results, pending approval.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `dataset_urn` | `TEXT` | Target dataset |
| `proposals` | `JSONB` | Proposed changes (field → value mappings) |
| `similar_diffs` | `JSONB` | Diff summaries against similar tables |
| `approval_status` | `TEXT` | `pending`, `approved`, `rejected` |
| `run_id` | `UUID` | Airflow DAG run ID |
| `generated_at` | `TIMESTAMPTZ` | |
| `approved_at` | `TIMESTAMPTZ` NULL | When the proposal was approved (PATCH `attr/gen/result/{id}` with `verdict: "approve"`) and written to DataHub |

#### `concept_categories`

Ontology/taxonomy concept hierarchy.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Concept identifier |
| `name` | `TEXT` UNIQUE | Concept name |
| `parent_id` | `UUID` FK NULL | Parent concept (self-referencing) |
| `description` | `TEXT` | Concept description |
| `status` | `TEXT` | `approved`, `pending`, `rejected` |
| `version` | `INTEGER` | Taxonomy version number |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `dataset_concept_map`

Maps datasets to concept categories with confidence scores.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `concept_id` | `UUID` PK, FK | Concept category |
| `confidence_score` | `REAL` | LLM classification confidence (0.0–1.0) |
| `status` | `TEXT` | `approved`, `pending` (pending if confidence < 0.7) |
| `created_at` | `TIMESTAMPTZ` | |

#### `concept_relationships`

Cross-concept relationships (edges in the ontology graph).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Relationship identifier |
| `concept_a` | `UUID` FK | Source concept |
| `concept_b` | `UUID` FK | Target concept |
| `relationship_type` | `TEXT` | `related_to`, `part_of`, `depends_on`, `overlaps_with` |
| `confidence_score` | `REAL` | LLM inference confidence |
| `created_at` | `TIMESTAMPTZ` | |

#### `metric_definitions`

Governance metric definitions.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT` PK | Metric identifier (slug, e.g. `poorly-documented-datasets`) |
| `title` | `TEXT` | Display title |
| `description` | `TEXT` | What this metric measures |
| `theme` | `TEXT` | Category: `quality`, `governance`, `freshness` |
| `measurement_query` | `JSONB` | `{"type": "poorly_documented"\|"stale_datasets", "dataset_filter": {"tags": [...], "glossary_terms": [...]}}` |
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
[API §Meta-Classifier Conventions](API.md#meta-classifier-conventions)).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Event identifier |
| `entity_type` | `TEXT` | `dataset`, `metric`, `concept` — classifies the entity, not the feature domain |
| `entity_id` | `TEXT` | URN or metric/concept ID |
| `event_type` | `TEXT` | Uppercase, dot-delimited `{DOMAIN}.{ACTION}` (e.g., `INGESTION.COMPLETE`, `METRIC.RUN_COMPLETE`, `CONCEPT.APPROVE`). Full catalogue in [BACKEND §Event Catalogue](BACKEND.md#event-catalogue). |
| `status` | `TEXT` | `success`, `failure`, `warning` |
| `detail` | `JSONB` | Event-specific payload |
| `occurred_at` | `TIMESTAMPTZ` | Event timestamp |

**Filtering convention**: `entity_type` identifies what the entity *is* (a
dataset, a metric, a concept). Ingestion, validation, and generation are
*attributes* of a dataset, so their events use `entity_type=dataset`. The
dataset-level event endpoint (`GET .../data/{urn}/event`) filters by
`entity_type=dataset` to return all event types for that dataset. Sub-resource
event endpoints (e.g., `.../event/ingestion`) additionally filter by
`event_type` prefix (e.g., `ingestion.*`) to return only domain-specific events.

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
| `generation_results` | `(dataset_urn, generated_at DESC)` | Time-range queries on results |
| `metric_results` | `(metric_id, measured_at DESC)` | Time-range queries on measurements |
| `events` | `(entity_type, entity_id, occurred_at DESC)` | Event log queries per entity |
| `dataset_concept_map` | `(concept_id)` | Concept-to-datasets lookup |
| `concept_categories` | `(parent_id)` | Hierarchy traversal |

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
- Kafka event: `datasetProperties`, `schemaMetadata`, `globalTags` changes
- Ontology rebuild: Kafka-driven incremental re-embedding when a concept changes (UC3)
- Scheduled: `ontology-sync` Airflow DAG (full re-sync, on-demand trigger)

**Access wrapper**: `src/shared/vector/client.py` exposes `PgVectorManager`
(session-factory backed) returning `VectorHit` dataclasses. Collection name is
whitelisted against `EMBEDDING_COLLECTION` to prevent arbitrary table access.

### Graph (Apache AGE, reserved)

The `age` extension is installed and preloaded (`shared_preload_libraries = 'age'`),
and `ag_catalog` usage is granted to the application role. No AGE graphs are
defined yet — ontology relationships remain in relational tables
(`concept_relationships`, `dataset_concept_map`). AGE is available for future
graph-shaped queries without additional infrastructure changes.
