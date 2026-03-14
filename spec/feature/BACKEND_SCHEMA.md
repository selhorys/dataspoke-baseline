# DataSpoke Backend — Data Contracts

> This document specifies the storage contracts shared across all DataSpoke
> backend processes (API, Temporal workers, event consumers): PostgreSQL tables,
> Qdrant vector collections, and related indexes.
>
> Companion to [BACKEND](BACKEND.md) (service logic, workflows, shared clients).
> Architecture context in [ARCHITECTURE](../ARCHITECTURE.md).

---

## Table of Contents

1. [PostgreSQL Schema](#postgresql-schema)
2. [Qdrant Collections](#qdrant-collections)

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
| `sources` | `JSONB` | Array of source configurations |
| `deep_spec_enabled` | `BOOLEAN` | Enable LLM enrichment |
| `schedule` | `TEXT` NULL | Cron expression for scheduled runs |
| `status` | `TEXT` | `active`, `paused`, `draft` |
| `owner` | `TEXT` | Owner user ID |
| `created_at` | `TIMESTAMPTZ` | Creation timestamp |
| `updated_at` | `TIMESTAMPTZ` | Last modification |

#### `validation_configs`

Stores per-dataset validation configuration.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Config identifier |
| `dataset_urn` | `TEXT` UNIQUE | Target dataset URN |
| `rules` | `JSONB` | Validation rules (thresholds, anomaly method, dimensions) |
| `schedule` | `TEXT` NULL | Cron expression for scheduled runs |
| `sla_target` | `JSONB` NULL | SLA targets (freshness hours, min quality score) |
| `status` | `TEXT` | `active`, `paused`, `draft` |
| `owner` | `TEXT` | Owner user ID |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `validation_results`

Timeseries of validation run results.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `dataset_urn` | `TEXT` | Target dataset |
| `quality_score` | `REAL` | Composite score 0–100 |
| `dimensions` | `JSONB` | Per-dimension scores |
| `issues` | `JSONB` | Array of `QualityIssue` objects |
| `anomalies` | `JSONB` | Array of `AnomalyResult` objects |
| `recommendations` | `JSONB` | Array of recommendation strings |
| `alternatives` | `JSONB` | Similar healthy dataset URNs from Qdrant |
| `run_id` | `UUID` | Temporal workflow run ID |
| `measured_at` | `TIMESTAMPTZ` | Measurement timestamp |

#### `generation_configs`

Stores per-dataset doc generation configuration.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Config identifier |
| `dataset_urn` | `TEXT` UNIQUE | Target dataset URN |
| `target_fields` | `JSONB` | Fields to generate (description, tags, deprecation) |
| `code_refs` | `JSONB` NULL | GitHub repo/file references for code analysis |
| `schedule` | `TEXT` NULL | Cron expression |
| `status` | `TEXT` | `active`, `paused`, `draft` |
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
| `run_id` | `UUID` | Temporal workflow run ID |
| `generated_at` | `TIMESTAMPTZ` | |
| `applied_at` | `TIMESTAMPTZ` NULL | When approved and applied |

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
| `theme` | `TEXT` | Category: `quality`, `completeness`, `freshness`, `governance` |
| `measurement_query` | `JSONB` | Configuration for how to compute this metric |
| `schedule` | `TEXT` NULL | Cron expression for scheduled measurement |
| `alarm_enabled` | `BOOLEAN` | Enable threshold-based alerting |
| `alarm_threshold` | `JSONB` NULL | Threshold configuration |
| `active` | `BOOLEAN` | Whether scheduled measurement is active |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metric_results`

Timeseries of metric measurements.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Result identifier |
| `metric_id` | `TEXT` FK | Metric definition |
| `value` | `REAL` | Measured numeric value |
| `breakdown` | `JSONB` NULL | Per-department or per-platform breakdown |
| `alarm_triggered` | `BOOLEAN` | Whether threshold was breached |
| `run_id` | `UUID` | Temporal workflow run ID |
| `measured_at` | `TIMESTAMPTZ` | Measurement timestamp |

#### `metric_issues`

Auto-detected metadata issues with lifecycle tracking. Generated by the Metrics
Service after each measurement run; assigned to dataset owners as action items
(UC6).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Issue identifier |
| `metric_id` | `TEXT` FK | Originating metric |
| `dataset_urn` | `TEXT` | Affected dataset |
| `issue_type` | `TEXT` | `missing_owner`, `no_description`, `stale`, `low_quality`, `no_tags`, etc. |
| `priority` | `TEXT` | `critical`, `high`, `medium` |
| `status` | `TEXT` | `open`, `in_progress`, `resolved`, `dismissed` |
| `assignee` | `TEXT` NULL | Assigned owner email (from DataHub ownership) |
| `description` | `TEXT` | Human-readable issue description |
| `estimated_fix_minutes` | `INTEGER` | Estimated time to resolve |
| `projected_score_impact` | `REAL` | Health score points gained if resolved |
| `due_date` | `TIMESTAMPTZ` NULL | Suggested due date |
| `resolved_at` | `TIMESTAMPTZ` NULL | When marked resolved |
| `created_at` | `TIMESTAMPTZ` | Detection timestamp |
| `updated_at` | `TIMESTAMPTZ` | Last status change |

**Issue lifecycle**: When a measurement run detects a gap (e.g., dataset has no
owner), the Metrics Service creates an issue with `status=open`. The
Notification Service emails the assignee with action items. When the gap is
resolved (detected in a subsequent measurement run), the issue is automatically
moved to `resolved`. Issues can also be manually `dismissed` via the API.

#### `events`

Unified event log for all feature domains. All events share the same top-level
structure so clients can process them generically (see
[API §Meta-Classifier Conventions](API.md#meta-classifier-conventions)).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Event identifier |
| `entity_type` | `TEXT` | `dataset`, `metric`, `concept` — classifies the entity, not the feature domain |
| `entity_id` | `TEXT` | URN or metric/concept ID |
| `event_type` | `TEXT` | Dot-prefixed by domain: `ingestion.completed`, `validation.completed`, `generation.completed`, `generation.applied`, `metric.run.completed`, `metric.alarm.triggered`, `concept.approved`, `concept.rejected`, etc. |
| `status` | `TEXT` | `success`, `failure`, `warning` |
| `detail` | `JSONB` | Event-specific payload |
| `occurred_at` | `TIMESTAMPTZ` | Event timestamp |

**Filtering convention**: `entity_type` identifies what the entity *is* (a
dataset, a metric, a concept). Ingestion, validation, and generation are
*attributes* of a dataset, so their events use `entity_type=dataset`. The
dataset-level event endpoint (`GET .../data/{urn}/event`) filters by
`entity_type=dataset` to return all event types for that dataset. Sub-resource
event endpoints (e.g., `.../attr/ingestion/event`) additionally filter by
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
| `metric_issues` | `(status, priority)` | Open issue dashboard queries |
| `metric_issues` | `(dataset_urn, status)` | Per-dataset issue lookup |
| `metric_issues` | `(metric_id, created_at DESC)` | Issues by metric |
| `dataset_concept_map` | `(concept_id)` | Concept-to-datasets lookup |
| `concept_categories` | `(parent_id)` | Hierarchy traversal |

---

## Qdrant Collections

### `dataset_embeddings`

Primary collection for natural language search and similarity matching.

| Field | Type | Description |
|-------|------|-------------|
| Vector | `float[]` (dimension depends on LLM model) | Embedding of dataset metadata |
| `dataset_urn` | payload string | Dataset URN |
| `platform` | payload string | Data platform (oracle, postgres, etc.) |
| `has_pii` | payload bool | PII classification flag |
| `quality_score` | payload float | Latest quality score |
| `tags` | payload string[] | DataHub tag URNs |
| `updated_at` | payload string | Last sync timestamp |

**Embedding input**: Concatenation of dataset name, description, field names +
descriptions, tags, and lineage context. Processed through the LLM embedding
endpoint.

**Sync triggers**:
- Kafka event: `datasetProperties`, `schemaMetadata`, `globalTags` changes
- Manual: `POST /spoke/common/search/method/reindex`
- Scheduled: `EmbeddingSyncWorkflow` (daily full re-sync)
