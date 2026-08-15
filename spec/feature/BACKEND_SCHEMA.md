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
in `migrations/`. The squashed `001_initial_schema` migration enables three
extensions: `vector` (pgvector embeddings), `age` (Apache AGE graph,
preloaded), and `citext` (case-insensitive text — used by `users.email`).

### Tables

#### `users`

DataSpoke-managed user identities. Rows are local; the DataHub corpuser at
`urn:li:corpuser:<email>` is provisioned by DataHub's OIDC JIT on the person's
first DataHub login, and DataSpoke projects role and marker-group membership
onto it — see [AUTH §DataHub Projection Semantics](AUTH.md#datahub-projection-semantics).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | DataSpoke-internal user identifier |
| `email` | `CITEXT` UNIQUE NOT NULL | Email address. `citext` for case-insensitive uniqueness. Drives the DataHub corpuser URN |
| `name` | `TEXT` NOT NULL | Display name |
| `password_hash` | `TEXT` NULL | bcrypt hash; null when the user authenticates exclusively via Google OAuth |
| `google_sub` | `TEXT` UNIQUE NULL | Google account `sub` claim; null when the user has not linked a Google account |
| `role` | `TEXT` NOT NULL DEFAULT `'Reader'` | Privilege level — one of `'Admin'`, `'Editor'`, `'Reader'`. DataSpoke is SSOT; propagated to DataHub via `batchAssignRole`. Gates routes per [AUTH §Privilege Model](AUTH.md#privilege-model). |
| `session_epoch` | `INTEGER` NOT NULL DEFAULT `0` | Per-user JWT generation counter. Access and refresh JWTs carry it as the `ses` claim; a token whose `ses` is absent or unequal to this value is rejected `401`. Incremented by the two writes that change the row's Google binding: the credential reset when an identity binds onto the row, and the admin unbind when one is released — see [AUTH §Session epoch](AUTH.md#session-epoch). |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

Constraints:
- `ck_users_auth_method`: `CHECK (password_hash IS NOT NULL OR google_sub IS NOT NULL)` — at least one authentication method must always be set. Clearing `password_hash` on a Google bind satisfies it because `google_sub` is set in the same statement.
- `CHECK (role IN ('Admin', 'Editor', 'Reader'))` — enum guard.

Deletion is hard delete (no `deleted_at` column) — DataSpoke removes the row
and hard-deletes the DataHub corpuser via `hard_delete_entity`.

#### `api_tokens`

Long-lived personal access tokens minted by users for non-interactive
clients (CI jobs, AI agents). See [AUTH §API Tokens](AUTH.md#api-tokens).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Token identifier (returned to the user for revocation; not the token itself) |
| `user_id` | `UUID` FK → `users(id)` ON DELETE CASCADE | Owner |
| `name` | `TEXT` NOT NULL | User-supplied label (e.g., "ci-jenkins", "personal-laptop") |
| `token_hash` | `CHAR(64)` UNIQUE NOT NULL | SHA-256 hex of the opaque token (`dsk_<...>`). The raw token is never stored. |
| `role_snapshot` | `TEXT` NOT NULL | Owner's `users.role` at mint time. Effective privilege = `min(role_snapshot, users.role)`. `CHECK` same vocabulary as `users.role`. |
| `created_at` | `TIMESTAMPTZ` | |
| `last_used_at` | `TIMESTAMPTZ` NULL | Updated per use (throttled to per-minute granularity to avoid DB pressure). Null until first use. The write is best-effort; what a reader may conclude from a stale or null value is stated in [AUTH §Audit and `last_used_at`](AUTH.md#audit-and-last_used_at). |
| `expires_at` | `TIMESTAMPTZ` NULL | Optional expiry; null = no expiry |
| `revoked_at` | `TIMESTAMPTZ` NULL | Set when the user or an admin revokes the token, and set on every one of a user's active tokens by the credential reset that runs when a Google identity binds onto their row ([AUTH §Credential reset on link](AUTH.md#credential-reset-on-link)). Once non-null, the token authenticates no further requests. |

A token is valid iff `revoked_at IS NULL AND (expires_at IS NULL OR
expires_at > now())` and the raw token hashes to a matching row. Per-user
cap of 10 active tokens (`revoked_at IS NULL`) — mint beyond cap returns
`409 TOKEN_LIMIT_EXCEEDED`.

#### `password_reset_tokens`

Single-use tokens for the `/auth/password/reset/*` flow.

| Column | Type | Description |
|--------|------|-------------|
| `token_hash` | `CHAR(64)` PK | SHA-256 hex of the raw token sent by email. The raw token is never stored |
| `user_id` | `UUID` FK → `users(id)` ON DELETE CASCADE | The user the token resets |
| `expires_at` | `TIMESTAMPTZ` NOT NULL | 15 minutes after issue |
| `used_at` | `TIMESTAMPTZ` NULL | Set when the token is consumed; null on issue |
| `created_at` | `TIMESTAMPTZ` | |

A token is valid iff `used_at IS NULL AND expires_at > now()` and the raw
token hashes to a matching row. Expired and consumed rows are cleaned up by a
periodic Airflow housekeeping DAG (no synchronous-delete invariant). The one
synchronous deleter is the credential reset that runs when a Google identity
binds onto a row: it deletes that user's unused rows inside the bind
transaction, because a pending reset link is a live re-entry path
([AUTH §Credential reset on link](AUTH.md#credential-reset-on-link)).

#### `ingestion_source`

Stores per-source ingestion configuration — one row per data source / recipe (not
per dataset). A single source produces many datasets, mirroring how DataHub models
ingestion. SSOT split: DataSpoke owns **registration** (this row: recipe, schedule,
scope); DataHub owns **results** (runs, observed datasets — synced down).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Source identifier |
| `mode` | `TEXT` | `DATAHUB_MANAGED` (DataHub's own recipe + cron; DataHub is SSOT, DataSpoke syncs the definition down), `ACTIVE_CUSTOM_MANAGED` (DataSpoke's pluggable extractor crawls + emits on a tier schedule), or `PASSIVE` (ingested outside DataHub/DataSpoke; DataSpoke records the registration and syncs results) |
| `name` | `TEXT` | Human-readable source name. For `DATAHUB_MANAGED`, mirrors the DataHub source name |
| `platform` | `TEXT` | DataHub platform name (`postgres`, `kafka`, `mysql`, `bigquery`, etc.) — the `recipe.source.type` |
| `recipe` | `JSONB` | DataHub-compatible recipe `{source: {type, config}}`. `config` is byte-compatible with DataHub's source recipe (e.g. `host_port`, `database`, `schema_pattern.allow/deny`, `env`). For `PASSIVE`, `config` carries only the declared scope as an `AllowDenyPattern`-shaped filter (same vocabulary, no connectivity/auth). Secrets referenced as `${name__key}` (resolved from K8s Secret `dataspoke-source-cred-<name>` key `<key>`); plaintext never stored |
| `schedule` | `TEXT` NULL | Cron expression — the recipe-standard `schedule` field exposed verbatim in the API. For `DATAHUB_MANAGED`, mirrored from DataHub's schedule. For `ACTIVE_CUSTOM_MANAGED`, must map to one of the three allowed tiers; `NULL` means manual-only (runs only on `…/method/run`, not on any tier DAG). Null for `PASSIVE` |
| `schedule_tier` | `TEXT` NULL | **Internal, derived — never exposed in the API.** The tier (`hourly`/`daily`/`weekly`) computed from `schedule`, cached so the Airflow tier DAG can `WHERE schedule_tier = …`. Null when `schedule` is null or for `PASSIVE` |
| `datahub_source_urn` | `TEXT` NULL | The `dataHubIngestionSource` URN for `DATAHUB_MANAGED` (sync key; also the `systemMetadata.pipelineName` match value for the optional observed-mapping enrichment). For `ACTIVE_CUSTOM_MANAGED`, the `pipeline_name` DataSpoke's extractor stamps. Null for `PASSIVE` |
| `parent_source_id` | `UUID` NULL FK → `ingestion_source(id)` ON DELETE CASCADE | **Internal — never exposed in the API.** Self-referential link from a DataHub CLI wrapper source to its registered parent (resolved at sync time from the wrapper's `recipe.pipeline_name` matched against the registered parent's `datahub_source_urn` — the wrapper's display name is cosmetic and never used for linking; see [DATAHUB_INTEGRATION §Ingestion Source Sync](../DATAHUB_INTEGRATION.md#ingestion-source-sync)). A row is a **wrapper** iff this is non-null and a **regular** source iff null; the list view hides wrappers. Indexed (`ix_ingestion_source_parent`) for the per-source event union |
| `status` | `TEXT` | `OK` / `ERROR` (last sync or run health) |
| `created_at` | `TIMESTAMPTZ` | Creation timestamp |
| `updated_at` | `TIMESTAMPTZ` | Last modification |

The API request/response body mirrors the UC1 recipe YAML 1:1 in JSON, using
DataHub-recipe-standard wording only — `{mode, name, schedule, recipe:{source:{type,config}}}`
— plus read-only management fields (`id`, `status`, `created_at`, `updated_at`,
and `datahub_source_urn` for `DATAHUB_MANAGED`). `schedule_tier` and `parent_source_id` are internal
and never appear in the API. The per-source event endpoint exposes a **derived** (not stored)
`wrapper: bool` on each event row — `true` when the event originated on a linked wrapper rather than
the source itself. See [API §Ingestion](../API.md#ingestion-spokeingestion).

- **Editability**: `DATAHUB_MANAGED` rows are read-only in DataSpoke (DataHub is SSOT — edits return `409 INGESTION_SOURCE_READONLY`); they are created/updated only by the sync sweep. `ACTIVE_CUSTOM_MANAGED` and `PASSIVE` are user-managed via the API.

#### `ingestion_source_dataset`

Source→dataset mapping — which datasets each source covers. Rebuilt by the sync
sweep (see [`BACKEND.md §Ingestion Service`](BACKEND.md)). Answers "which recipe
covers which data?"; datasets present in DataHub but absent from this table for
every source form the **unmanaged bucket**.

| Column | Type | Description |
|--------|------|-------------|
| `source_id` | `UUID` FK → `ingestion_source(id)` ON DELETE CASCADE | Owning source |
| `dataset_urn` | `TEXT` | A dataset the source covers |
| `derivation` | `TEXT` | How the link was established: `matched` (recipe filter / declared allow-deny evaluated against the dataset set), `emitted` (`ACTIVE_CUSTOM_MANAGED` extractor's own run output), or `pipeline_name` (observed via `systemMetadata.pipelineName`, optional enrichment for the two MANAGED modes; also inherited by a wrapper source from its registered parent via `parent_source_id` — see [BACKEND §Sync sweep](BACKEND.md#ingestion-service-srcbackendingestion)) |
| `first_seen_at` | `TIMESTAMPTZ` | First sweep that linked this pair |
| `last_seen_at` | `TIMESTAMPTZ` | Most recent sweep confirming the link |

- **PK**: `(source_id, dataset_urn)`.
- `derivation` is named to avoid collision with DataHub's dataset URN fabric (`origin`/`FabricType`, which is also the `dataset_registry.origin` column a `dataset_filter` reads). The API additionally exposes a derived **`authority`** confidence level: `medium` for `matched` rows (declared/derived coverage — what the recipe says it covers, an explicit approximation since DataHub exposes no native source→dataset reverse lookup) and `high` for `emitted` / `pipeline_name` rows (observed and authoritative). `authority` is a pure function of `derivation`, so it is derived at the API layer rather than stored.

#### `dataset_registry`

Mirrors the DataHub dataset estate: one row per known dataset URN with a
`datahub_registered` flag plus the attributes `dataset_filter` is evaluated against. Feeds
the validation precondition gate, the ingestion **unmanaged bucket**
(`GET /spoke/ingestion/unmanaged` = rows with `datahub_registered=true` and no
`ingestion_source_dataset` mapping), and scope resolution for UC3 ontogen, UC4 metagen, and
UC5 metrics. Presence is not a "validation-configured" marker — validation-config existence
lives in `validation_configs`.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `datahub_registered` | `BOOLEAN` | `true` when the dataset exists in DataHub |
| `origin` | `TEXT` NULL | The URN's third segment — a DataHub `FabricType` value (`PROD`/`DEV`/…). Parsed from `dataset_urn`, not fetched |
| `platform_urn` | `TEXT` NULL | The URN's first segment — `urn:li:dataPlatform:…`. Parsed from `dataset_urn`, not fetched |
| `tag_urns` | `TEXT[]` NOT NULL DEFAULT `'{}'` | DataHub tag URNs on the dataset |
| `glossary_term_urns` | `TEXT[]` NOT NULL DEFAULT `'{}'` | DataHub glossary-term URNs on the dataset |
| `is_primary` | `BOOLEAN` NOT NULL DEFAULT `true` | `true` when the dataset is the primary member of its DataHub `siblings` set, or has no siblings. Not null: absent sibling information means primary, so a never-swept row is counted once rather than dropped. Distinct from `dataset_node_map.is_primary`, which marks the authoritative dataset of an ontology node — a `dataset_filter` resolves column names only against `dataset_registry` |
| `attrs_synced_at` | `TIMESTAMPTZ` NULL | When the attribute columns above were last refreshed; `null` until the first attribute sweep reaches the row. Surfaced on `GET /spoke/governance/metric/{metric_id}/dataset` |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

- **Indexes**: GIN on `tag_urns` and `glossary_term_urns` (array containment is the array-column predicate's access path), btree on `origin` and `platform_urn`, and a partial btree on `is_primary WHERE NOT is_primary` — the column is `true` registry-wide by default, so only the `false` side is selective enough for an index to be used.
- **Creation / reconcile**: bulk, by the `datahub-sync-hourly` sweep — it enumerates DataHub once and upserts every URN (insert new rows `datahub_registered=true`; soft-flag absent rows `false`; an empty enumeration is skipped as "no signal"). Additionally lazy, via `ensure_dataset_registered()` on validation-config upsert, which probes DataHub on-demand for per-dataset precision (the precondition gate `422 DATASET_NOT_IN_DATAHUB` reads the flag).
- **Attribute sync**: the same sweep refreshes the attribute columns, upserting per dataset and never deleting-then-inserting — a dataset the attribute read missed keeps its prior attributes, so a partial sweep cannot silently narrow every `dataset_filter` in the system. See [BACKEND §Sync + mapping sweep](BACKEND.md#ingestion-service-srcbackendingestion) and [DATAHUB_INTEGRATION §Dataset attribute sync](../DATAHUB_INTEGRATION.md#dataset-attribute-sync).
- **DataHub sync**: the scheduled full reconcile is the `datahub-sync-hourly` sweep; `POST /internal/admin/datahub/sync` provides on-demand scoped reconcile.
- **SSOT**: DataHub is authoritative for dataset existence and attributes; the registry mirrors both, refreshed per sweep with on-demand reconcile for validation.

#### `validation_configs`

Stores the single validation slot per dataset (passive result-store model — see
[`spec/feature/VALIDATION.md`](VALIDATION.md)). One row per dataset.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Target dataset URN (unique — at most one validation slot per dataset) |
| `description` | `TEXT` | Free-form description (≤ 2,000 chars; surfaced in DataHub assertion detail UI) |
| `variables` | `JSONB` | Declared variables the pipeline will report — a JSONB array of `{name, description}` objects. `name` matches `[a-z][a-z0-9_]{0,99}` and is unique within the row; `description` is ≤ 200 chars (empty allowed). `CHECK jsonb_array_length(variables) BETWEEN 1 AND 200`. Variable **names** are joined as `customAssertion.logic` on DataHub emit |
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
| `variables` | `JSONB` | Map of variable name → numeric value. Keys must be a subset of the `validation_configs.variables` **names** (validated at the service layer; `422 UNKNOWN_VARIABLE` on violation) |
| `ingestion_time` | `TIMESTAMPTZ` | Server-side `now()` when the row was accepted (audit trail; preserved separately from `data_time`) |

`validation_results` rows are deleted by the conf `DELETE` cascade: deleting a dataset's
`validation_configs` row removes that dataset's `validation_results` (and its validation
events) in the same service-level transaction. There is no FK cascade — the service
issues the deletes explicitly keyed on `dataset_urn`.

Indexes: `(dataset_urn, data_time DESC)` to serve the historical-baseline GET.

Multiple rows may share `(dataset_urn, data_time)` — append-only matches DataHub's
timeseries aspect semantics. The GET endpoint collapses duplicates with last-write-wins
per distinct `data_time`.

#### `metagen_config`

One row per Metadata Generation conf (UC4) — a managed collection, not a
singleton. Many confs can coexist, each with its own scope and budget.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Conf identifier |
| `name` | `TEXT` UNIQUE NOT NULL | Human-readable conf name (`409 METAGEN_CONF_EXISTS` on create collision) |
| `is_enabled` | `BOOLEAN` | Master switch — enabled confs run on their `schedule_tier` and are eligible for scheduled fan-out |
| `schedule_tier` | `TEXT` NULL | `hourly`, `daily`, or `weekly` re-generation cadence. When null, no periodic DAG runs; manual `POST /conf/{conf_id}/method/run` is unaffected |
| `dataset_filter` | `TEXT` | Scope filter — a SQL `WHERE` clause over `dataset_registry` ([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar)); `''` = all registered datasets. Same grammar as `ontogen_config.dataset_filter` and `metric_definitions.dataset_filter` |
| `result_limit` | `INTEGER` | Max non-rejected candidates per `(conf_id, item)` (range `[1, 20]`, default `3`) |
| `overwrite_pending` | `BOOLEAN` | When this conf's per-item budget is full and the item has no `approved` candidate, true = evict oldest `llm_approved` candidate of this conf; false = skip the item (default true) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `metagen_boundary`

Per-dataset opt-in boundary for UC4 metagen, shared across all confs. Absence of
a row, or a row with `is_enabled=false`, means the dataset is excluded from every
conf regardless of any conf's `dataset_filter`. `allowed` caps which element kinds
any conf may write on this dataset.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Target dataset URN |
| `is_enabled` | `BOOLEAN` | When true, this dataset participates in global metagen |
| `allowed` | `TEXT[]` | Element kinds the global generator may write — subset of `{"dataset.description", "column.description"}` |
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

One row per generated candidate value. Candidates accumulate per
`(conf_id, item)` across runs up to that conf's `result_limit`; `rejected` rows
are deleted at the start of the next run.

| Column | Type | Description |
|--------|------|-------------|
| `candidate_id` | `UUID` PK | Candidate identifier |
| `conf_id` | `UUID` FK → `metagen_config(id)` NULL | The conf that produced this candidate. `ON DELETE SET NULL`: deleting the conf orphans every candidate it produced (any status) by nulling `conf_id`, retaining each as a parentless result with no re-linking |
| `dataset_urn` | `TEXT` | Target dataset URN |
| `item_id` | `TEXT` | Item this candidate belongs to (FK `(dataset_urn, item_id)` → `metagen_items`) |
| `run_id` | `UUID` | The metagen run that produced this candidate |
| `value` | `TEXT` | Markdown proposal (≤ 16 KiB) |
| `confidence_score` | `REAL` | Producer-Reviewer debate confidence (`[0.0, 1.0]`) |
| `status` | `TEXT` | `llm_approved` (debate-accepted, awaiting human), `approved` (human accepted, emitted to DataHub), `rejected` (human rejected, deleted next run) |
| `evidence` | `JSONB` | Debate transcript plus per-item Reviewer verdicts |
| `created_at` | `TIMESTAMPTZ` | |
| `reviewed_at` | `TIMESTAMPTZ` NULL | Human review timestamp |
| `reviewer_id` | `TEXT` NULL | User ID of the reviewer |

Indexes: `(conf_id, dataset_urn, item_id, status, created_at)` for per-conf FIFO
eviction queries and per-`(conf_id, item)` budget checks; `(run_id)` for
run-scoped cleanup.

A partial unique index `UNIQUE (dataset_urn, item_id) WHERE status='approved'`
enforces the invariant that an item has at most one `approved` candidate at any
time — **globally across all confs**. Approving a candidate un-approves the
previously-approved sibling (which may belong to a different conf) in the same
transaction (see [BACKEND §Metadata Generation Service](BACKEND.md#metadata-generation-service-srcbackendmetagen)).

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
| `dataset_filter` | `TEXT` | Scope filter — a SQL `WHERE` clause over `dataset_registry` ([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar)); `''` = all registered datasets. Same grammar as `metagen_config.dataset_filter` and `metric_definitions.dataset_filter` |
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
| `is_enabled` | `BOOLEAN` | Defaults `false` (created disabled). Only `is_enabled = true` seeds steer inference; toggled via `PATCH .../attr/seed/{seed_id}/attr/enabled`. `DELETE` is a hard delete (row removed) |
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
| `run_id` | `UUID` NULL | The inference run that produced this row, written **only on insert** (never overwritten on reuse/update). `NULL` for seeded rows. Identifies the run's Langfuse session (`session_id = run_id`) holding the debate transcript — see [BACKEND_LLM §Evidence](BACKEND_LLM.md#evidence--the-runs-langfuse-session) |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

#### `dataset_node_map`

Maps datasets to nodes with confidence scores.

| Column | Type | Description |
|--------|------|-------------|
| `dataset_urn` | `TEXT` PK | Dataset URN |
| `node_id` | `TEXT` PK, FK → `ontogen_nodes(id)` | Node |
| `confidence_score` | `REAL` | LLM inference confidence (0.0–1.0) |
| `status` | `TEXT` | `llm_pending`, `llm_approved`, `approved`, `rejected` — same vocabulary as `ontogen_nodes`; cascaded from the parent node row on human review |
| `is_primary` | `BOOLEAN` | True for the primary (authoritative) member dataset of the node — distinct from `dataset_registry.is_primary`, the DataHub sibling-leadership mirror a `dataset_filter` reads |
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
| `run_id` | `UUID` NULL | The inference run that produced this row; same semantics as `ontogen_nodes.run_id` (insert-only, `NULL` for seeded rows) |
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
| `run_id` | `UUID` NULL | The inference run that produced this row; same semantics as `ontogen_nodes.run_id` (insert-only, `NULL` for seeded rows) |
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
| `metrics` | `JSONB` | Series descriptors — a list of `{name, color, idx}` objects. `name` is one of the type's emitted `values` keys, `color` a `#RRGGBB` hex string, `idx` a positive integer display order; `name` and `idx` are each unique within the row. Determines which keys the metric persists and how the dashboard chart draws them |
| `metric_conf` | `JSONB` | Type-specific config — `{"time_window_sec": <int>}` for `ingestion-freshness` / `validation-score` (the measurement window applied to every dataset the metric scans; range enforced at the write boundary, not by a column constraint ([API §Metric](../API.md#metric-spokegovernancemetric)); factory default `172800`, see BACKEND §Metrics Service); `{}` for `doc-health` |
| `dataset_filter` | `TEXT` | Scope filter — a SQL `WHERE` clause over `dataset_registry` ([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar)); `''` = all registered datasets. Same grammar as `ontogen_config.dataset_filter` and `metagen_config.dataset_filter` |
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
| `breakdown` | `JSONB` NULL | Measurement breakdown: `{dataset_count, datasets: [{urn, detail?}]}`. `datasets[]` carries only failed entries (stale / validation `<1.0` / doc-health `<1.0` depending on `metric_type`); `dataset_count` is the total scanned. Derived from the same verdicts that populate `metric_dataset_results` |
| `measured_at` | `TIMESTAMPTZ` | Measurement timestamp |

#### `metric_dataset_results`

The **latest** per-dataset verdict for each metric — one row per dataset the metric
covered on its most recent non-dry run. Unlike `metric_results`, this is not a timeseries:
a non-dry run replaces the metric's rows wholesale inside the result transaction, and a dry
run writes nothing. Backs `GET /spoke/governance/metric/{metric_id}/dataset`.

| Column | Type | Description |
|--------|------|-------------|
| `metric_id` | `TEXT` FK → `metric_definitions(id)` ON DELETE CASCADE | Owning metric |
| `dataset_urn` | `TEXT` | The evaluated dataset |
| `met` | `BOOLEAN` | Whether the dataset met the metric's criterion on that run |
| `evidence_at` | `TIMESTAMPTZ` NULL | The per-dataset evidence timestamp — resolved ingestion evidence (`ingestion-freshness`), counted result `data_time` (`validation-score`), `null` for `doc-health`, which has no per-dataset timestamp |
| `detail` | `JSONB` | Type-specific per-dataset metadata, the same payload the failing entries carry in `metric_results.breakdown` |
| `measured_at` | `TIMESTAMPTZ` | The run that produced this verdict |

- **PK**: `(metric_id, dataset_urn)`.
- A dataset in the metric's current `dataset_filter` scope with no row here is **unknown** —
  in scope but never evaluated. The endpoint resolves that by left-joining this table onto
  the filter's registry query, so scope and verdicts are read from one source and cannot
  disagree with the run's own.
- `last_check_at` on the API is `evidence_at` falling back to `measured_at`, resolved
  server-side.

#### `runtime_config`

Singleton row holding the behavioral tunables that shape LLM inference and
generation across features. Edited at runtime via `/api/v1/admin/conf` (see
[`spec/API.md` §Admin](../API.md)); seeded with factory defaults on first read.
The LLM API key is **not** a column here — it is rotated through the same
`/admin/conf` surface but stored in the `dataspoke-llm-secret` Kubernetes Secret
(see [`BACKEND_LLM.md` §LLM API key](BACKEND_LLM.md)).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK (=1) | Singleton row (`CHECK (id = 1)`) |
| `llm_provider` | `TEXT` | LLM provider (`gemini`, `openai`, `anthropic`, …); factory default `gemini` |
| `llm_model` | `TEXT` | Producer model identifier; factory default `gemini-3.5-flash` |
| `ontogen_llm_max_iterations` | `INTEGER` | Ontogen inference-loop cap [1, 20]; default 3 |
| `ontogen_debate_max_turns` | `INTEGER` | Ontogen debate turns [2, 10]; default 4 |
| `ontogen_debate_rag_k` | `INTEGER` | Ontogen debate RAG top-K [0, 20]; default 5 |
| `ontogen_debate_reviewer_model` | `TEXT` NULL | Reviewer model override; null reuses `llm_model` |
| `metagen_llm_max_iterations` | `INTEGER` | Metagen inference-loop cap [1, 20]; default 3 |
| `metagen_debate_max_turns` | `INTEGER` | Metagen debate turns [2, 10]; default 4 |
| `metagen_debate_rag_k` | `INTEGER` | Metagen debate RAG top-K [0, 20]; default 5 |
| `metagen_debate_reviewer_model` | `TEXT` NULL | Reviewer model override; null reuses `llm_model` |
| `metagen_confidence_threshold` | `FLOAT` | Metagen persistence gate [0.0, 1.0]; default 0.7 |
| `metagen_ontology_rag_node_k` | `INTEGER` | Metagen ontology-node RAG top-K [0, 20]; default 5 |
| `metagen_ontology_rag_edge_k` | `INTEGER` | Metagen ontology-edge RAG top-K [0, 20]; default 5 |
| `metagen_ontology_rag_triple_k` | `INTEGER` | Metagen RDF-triple RAG top-K [0, 20]; default 5 |
| `stub_redis_client` | `BOOLEAN` | Stub the Redis client dependency; default `false` |
| `stub_llm_client` | `BOOLEAN` | Stub the LLM client dependency; default `false` |
| `stub_pgvector_manager` | `BOOLEAN` | Stub the pgvector manager dependency; default `false` |
| `stub_notification_service` | `BOOLEAN` | Stub the notification service dependency; default `false` |
| `auth_datahub_corp_group` | `TEXT` | DataHub corpGroup naming the DataSpoke-user provenance marker; default `dataspoke-users` |
| `updated_at` | `TIMESTAMPTZ` | |

#### `events`

Unified event log for all feature domains. All events share the same top-level
structure so clients can process them generically (see
[API §Meta-Classifier Conventions](../API.md#meta-classifier-conventions)).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `UUID` PK | Event identifier |
| `entity_type` | `TEXT` | `dataset`, `ingestion_source` (ingestion runs, booked on the owning source), `metric`, `node`, `edge`, `triple`, `ontogen` (singleton conf + seeds), `metagen` (per-conf run events), `user` (auth-domain events) — classifies the entity, not the feature domain |
| `entity_id` | `TEXT` | URN or metric/node/edge/triple ID; for `entity_type='ingestion_source'` the `source_id`; for `entity_type='ontogen'` either the literal string `singleton` (conf) or a `seed:{seed_id}` form (seed events); for `entity_type='metagen'` the `conf_id`; for `entity_type='user'` the `users.id` |
| `event_type` | `TEXT` | Uppercase, dot-delimited `{DOMAIN}.{ACTION}` (e.g., `INGESTION.COMPLETE`, `METRIC.RUN_COMPLETE`, `NODE.APPROVE`, `TRIPLE.APPROVE`, `METAGEN.CANDIDATE_APPROVE`, `METAGEN.RUN_COMPLETE`, `ONTOGEN.RUN_COMPLETE`). Full catalogue in [BACKEND §Event Catalogue](BACKEND.md#event-catalogue). |
| `status` | `TEXT` | One of `success`, `ok`, `failure`, `error`, `running`, `warning`, `info` |
| `detail` | `JSONB` | Event-specific payload |
| `occurred_at` | `TIMESTAMPTZ` | Event timestamp |

**Filtering convention**: `entity_type` identifies what the entity *is* (a
dataset, an ingestion source, a metric, an ontology node / edge / triple, the
ontogen singleton). Validation results and metagen candidate reviews are
*attributes* of a dataset, so their events use `entity_type=dataset`. Ingestion
runs, however, are booked on the owning **source** (`entity_type=ingestion_source`,
`entity_id=source_id`), not on the dataset, because a run covers a source's whole
extraction rather than a single dataset. The dataset-level event endpoint
(`GET .../data/{urn}/event`) is therefore a **union**, not a single
`entity_type=dataset` filter: it returns the `entity_type=dataset` rows for the
URN combined with the covering source's ingestion runs, located by reverse-lookup
(`IngestionService.reverse_lookup(urn)` → source, then that source's aggregated
run events, each row carrying the derived `wrapper` flag). The source's rows are
narrowed to those whose `detail.dataset_urn` is this URN **or is absent**, so a
sibling dataset's per-dataset observations are excluded while run-level rows —
which carry no scalar `dataset_urn` — are kept. The merged stream is
sorted newest-first, filtered by `from`/`to` and by the repeatable
`event_major_type` prefix set (`INGESTION`/`VALIDATION`/`METAGEN`; omitted = all),
then paginated. See [BACKEND §Dataset Service](BACKEND.md#dataset-service-srcbackenddataset) for
the aggregation mechanics. Sub-resource event endpoints (e.g., `/spoke/ingestion/sources/{id}/event`,
`/spoke/metagen/conf/{conf_id}/event`) additionally filter by `event_type` prefix
(e.g., `INGESTION.%`, `METAGEN.%`) to return only domain-specific events.
The Ontology Generation singleton uses `entity_type=ontogen` and `entity_id='singleton'`
(conf) or `entity_id='seed:{seed_id}'` (seed events) for the global event log surfaced
at `/spoke/ontogen/event`; per-result events use `entity_type=node|edge|triple`
and the corresponding ID. Metadata Generation per-conf run events use
`entity_type=metagen` and `entity_id=conf_id`; `/spoke/metagen/conf/{conf_id}/event`
filters by that pair while `/spoke/metagen/event` returns the cross-conf union (all
`entity_type=metagen` rows). Metagen candidate-review events remain `entity_type=dataset`
(an attribute of the dataset).

#### `peripheral_config`

Connection settings for the peripheral subsystems, managed via
`/api/v1/admin/peripherals/*` (see
[DATAHUB_INTEGRATION.md](../DATAHUB_INTEGRATION.md) §Configuration). Absence of
a row disables the corresponding integration.

| Column | Type | Description |
|--------|------|-------------|
| `name` | `VARCHAR(32)` PK | Peripheral name; `CHECK` ∈ `datahub`, `langfuse`, `smtp` |
| `settings` | `JSONB` | Peripheral-specific **non-secret** connection settings (DataHub `gms_url`/`frontend_url`/`kafka_brokers`/`kafka_security_protocol`/`kafka_sasl_mechanism`/`kafka_sasl_username`/`kafka_aws_region`/`kafka_sasl_password_version`/`service_corpuser_urn`/`default_env`, Langfuse `host`/`public_key`/`project_id`/`environment_tag`, SMTP host/port/username). Secret fields (DataHub `token` and `kafka_sasl_password`, Langfuse `secret_key`, SMTP password) are never stored here — they live in K8s Secrets, resolved at runtime via the API's RBAC. `kafka_sasl_password_version` is an integer counter, not a credential: it changes whenever the password Secret is written, so a running consumer detects a rotation from the DB row alone. |
| `updated_at` | `TIMESTAMPTZ` | |

#### `peripheral_health`

Last observed liveness of a peripheral connection, written by the processes that
exercise that transport and read back by
`GET /api/v1/admin/peripherals/datahub`. The table exists because
`is_configured` reports only that settings are present; a wrong SASL mechanism
or an unauthorized IAM role produces a fully "configured" row that never
connects.

Rows are keyed per **transport**, not per peripheral product: `datahub` is DataHub's
event stream, reported by the event consumer, and `datahub-api` is its GMS metadata
API, reported by the `datahub-sync-hourly` sweep. The two planes fail independently, so they
never share a row — see
[BACKEND §Health reporting](BACKEND.md#health-reporting).

| Column | Type | Description |
|--------|------|-------------|
| `name` | `VARCHAR(32)` PK | Transport name; `CHECK` ∈ `datahub`, `langfuse`, `smtp`, `datahub-api` — a superset of the `peripheral_config` domain (`datahub-api` is a second transport of the `datahub` peripheral, not a peripheral of its own), with **no foreign key** to it |
| `status` | `VARCHAR(16)` | `CHECK` ∈ `unknown`, `ok`, `error`; `unknown` until a reporter writes |
| `last_error` | `TEXT` | Most recent failure message; `NULL` when never failed |
| `last_ok_at` | `TIMESTAMPTZ` | Last successful connection; `NULL` when never succeeded |
| `updated_at` | `TIMESTAMPTZ` | Last report of any status |

A row is upserted on report, so the table never grows past the transport set
and carries no history. Absence of a row and `status='unknown'` mean the same
thing to readers: nothing has reported yet.

The two tables are deliberately **independent**. A foreign key on `name` would
make the health upsert fail precisely when the `peripheral_config` row is
missing — a deleted or never-created peripheral is exactly the condition the
health table exists to report, so the constraint would suppress the signal it
is meant to carry. The overlapping `CHECK` domains keep the key spaces aligned
without coupling the writes.

### Indexes

| Table | Index | Purpose |
|-------|-------|---------|
| `validation_results` | `(dataset_urn, data_time DESC)` | Time-range queries on results (historical-baseline GET) |
| `metagen_candidates` | `(conf_id, dataset_urn, item_id, status, created_at)` | Per-conf, per-item FIFO eviction and budget checks |
| `metagen_candidates` | `UNIQUE (dataset_urn, item_id) WHERE status='approved'` | Global one-approved-per-item invariant (across all confs) |
| `metagen_candidates` | `(run_id)` | Run-scoped cleanup |
| `metric_results` | `(metric_id, measured_at DESC)` | Time-range queries on measurements |
| `dataset_registry` | GIN on `tag_urns`; GIN on `glossary_term_urns` | `'…' IN tag_urns` / `IN glossary_term_urns` predicates in `dataset_filter` |
| `dataset_registry` | `(origin)`, `(platform_urn)` | `origin` / `platform_urn` equality and `IN` predicates in `dataset_filter` |
| `dataset_registry` | `ix_dataset_registry_not_primary`: `(is_primary) WHERE NOT is_primary` | `is_primary = false` predicates in `dataset_filter`; partial because the column defaults to `true` registry-wide |
| `events` | `(entity_type, entity_id, occurred_at DESC)` | Event log queries per entity |
| `events` | `ix_events_ingestion_dataset_urn`: `(entity_id, (detail->>'dataset_urn'), occurred_at DESC) WHERE entity_type='ingestion_source'` | Per-dataset ingestion timeline (`…/data/{urn}/event`) and per-dataset observation evidence for `ingestion-freshness` |
| `events` | `ix_events_ingestion_run_level`: `(entity_id, occurred_at DESC) WHERE entity_type='ingestion_source' AND event_type IN ('INGESTION.COMPLETE','INGESTION.FAIL') AND (detail->>'source' IS NULL OR detail->>'source' NOT IN (<observation producers>))` | Latest run-outcome lookup behind `attr/ingestion.latest_run` and the source list's status column |
| `dataset_node_map` | `(node_id)` | Node-to-datasets lookup |
| `ontogen_triples` | `(subject_node_id)`, `(object_node_id)`, `(edge_id)` | Triple lookup by any participant |
| `password_reset_tokens` | `(user_id, expires_at DESC)` | Cleanup of active / expired tokens per user |
| `api_tokens` | `(user_id) WHERE revoked_at IS NULL` | Per-user active token list and cap enforcement |

#### Ingestion event indexes

Both ingestion indexes are partial on `entity_type='ingestion_source'`, and both are
shaped by the two-branch predicates their queries emit (read paths:
[BACKEND §Ingestion Service](BACKEND.md#ingestion-service-srcbackendingestion);
`detail.source` producer vocabulary: [BACKEND §Event Catalogue](BACKEND.md#event-catalogue)).
Three shape constraints are load-bearing:

- **`entity_id` must lead.** The per-dataset timeline filters
  `detail->>'dataset_urn' IS NULL OR detail->>'dataset_urn' = :urn` — the `IS NULL` branch is
  the run-level feed and is therefore estate-wide, so an index keyed on the JSONB expression
  alone is not selective enough to be chosen and buys nothing over the scan the query already
  performs. The source id is the only selective leading key, and it is present on every caller.
- **The trailing `occurred_at DESC` serves the `from`/`to` range, not the `ORDER BY`.** Because
  every caller emits the disjunction, the plan is a bitmap combination of both branches, and a
  bitmap heap scan discards index order — the sort happens regardless. The column is there so the
  time window is bounded inside the index rather than on the heap.
- **`ix_events_ingestion_run_level` is inert without extended statistics.** Without them the
  planner has no distribution for `detail->>'source'`, over-estimates the number of matching rows
  by orders of magnitude, and under `ORDER BY occurred_at DESC LIMIT 1` prefers the generic
  `(entity_type, entity_id, occurred_at DESC)` index's fast-start path — precisely on the sources
  the partial index exists for, whose feed is dominated by per-dataset observations. It is
  therefore paired with a statistics object:

  ```sql
  CREATE STATISTICS st_events_detail_source ON (detail->>'source') FROM events;
  ```

Vocabulary drift on the run-level predicate is asymmetric: **adding** an observation producer
needs no DDL, since a longer `NOT IN` list implies the shorter one, while **removing** one leaves
the index predicate no longer implied by the query and the index unusable until it is edited.

A statistics object has no declarative table-metadata form, so it exists only in the migration.
Schemas built directly from the ORM metadata (test fixtures) lack it — acceptable, because it is a
planner hint and no behaviour depends on the plan chosen.

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
- On-demand: rebuilt by a manual `POST /spoke/ontogen/method/run` (synchronous, in-process)
- Optional event-driven extension (not enabled in baseline): Kafka MCL events for
  `datasetProperties` / `schemaMetadata` / `globalTags` changes — see
  [DATAHUB_INTEGRATION §Event Subscription](../DATAHUB_INTEGRATION.md#event-subscription-not-used-by-baseline)

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
- On-demand: rebuilt by a manual `POST /spoke/ontogen/method/run` when name
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
DAG or manual `POST /spoke/ontogen/method/run` that refreshes `node_embeddings`.

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
DAG or manual `POST /spoke/ontogen/method/run` that refreshes `node_embeddings`.

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

