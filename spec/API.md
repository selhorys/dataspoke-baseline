# DataSpoke API

> This document is the master reference for the DataSpoke API — its route catalogue,
> authentication model, request/response conventions, middleware stack, and error
> catalogue.
>
> Conforms to [MANIFESTO](MANIFESTO_en.md) (highest authority).
> Routing model defined in [ARCHITECTURE](ARCHITECTURE.md).
> Request/response conventions derive from [API_DESIGN_PRINCIPLE](API_DESIGN_PRINCIPLE_en.md).
> DataHub integration patterns are in [DATAHUB_INTEGRATION](DATAHUB_INTEGRATION.md).
> Backend services that implement these routes are in [BACKEND](feature/BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication & Authorization](#authentication--authorization)
3. [Route Catalogue](#route-catalogue)
4. [Request & Response Conventions](#request--response-conventions)
5. [Middleware Stack](#middleware-stack)
6. [Error Catalogue](#error-catalogue)

---

## Overview

The DataSpoke API is a FastAPI (Python 3.13) service that acts as the single ingress for
all DataSpoke clients — the portal UI and external AI agents. The URI structure has two
axes:

- **Per-dataset, cross-feature** routes live under `/spoke/common/data/{dataset_urn}/…`
  — the dataset resource — with sub-resources for ingestion, validation, and metagen.
- **Cross-dataset list views and global features** live under one namespace per
  MANIFESTO §2.1 feature.

```
/api/v1/spoke/common/data                  — Dataset catalog (collection root — all registered datasets)
/api/v1/spoke/common/data/{dataset_urn}/…  — Dataset resource (per-dataset, cross-feature)
/api/v1/spoke/ingestion                    — Ingestion Control cross-dataset list
/api/v1/spoke/validation                   — Validation cross-dataset list
/api/v1/spoke/ontogen/…                    — Ontology Generation (global singleton)
/api/v1/spoke/metagen/…                    — Metadata Generation (conf collection + global review queue)
/api/v1/spoke/governance/…                 — Governance metrics
```

The API is the only **HTTP-facing** component for external clients (the portal UI and
AI agents). Backend services also access DataHub, PostgreSQL (including pgvector),
and Redis directly.
Airflow orchestrates workflows by calling internal activity endpoints on the API.

In the future, DataSpoke may also expose **redefined DataHub functions** — blended endpoints that 
proxy DataHub's basic operations (e.g., dataset creation, metadata browsing, searching) while 
simultaneously handling DataSpoke-specific data in a single call. These would appear under 
`/spoke/common/data` as creation and modification routes (e.g., `POST /spoke/common/data`). 
See [DATAHUB_INTEGRATION §Key principles](DATAHUB_INTEGRATION.md#overview) for details.

```
Browser / AI Agent
       │
       ▼  HTTPS
┌──────────────────┐
│  DataSpoke API   │  ← this document
│  (FastAPI)       │
└──────────────────┘
   │      │      │
   ▼      ▼      ▼
DataHub  Postgres  Redis / Airflow
```

### API-First Design

The FastAPI implementation in `src/api/` is the **single source of truth** for the API
contract. Pydantic schemas and route definitions auto-generate OpenAPI 3.0 documentation,
ensuring docs are always in sync with the implementation. AI agents and the frontend team
reference `src/api/routers/` for the current contract or the live ReDoc UI at `/redoc`.
This spec (`API.md`) defines the architectural route catalogue; the implementation must
conform to it.

---

## Authentication & Authorization

User identity, registration, OAuth, password reset, profile management, and the
mirror semantics between DataSpoke users and DataHub corpusers are specified in
[feature/AUTH.md](feature/AUTH.md). This section captures the JWT shape,
token lifecycles, and access-control gating that any client implementor needs.

### Authentication Mechanisms

Three distinct mechanisms cover different client types:

| Mechanism | Carrier | Lifetime | Source | Scope |
|-----------|---------|----------|--------|-------|
| **User JWT** | `Authorization: Bearer <access_token>` header | 15 min access / 7 d refresh | `POST /auth/token` (email + password) or `GET /auth/google/login` (Google OAuth) | Effective role = caller's `users.role`. See [Access Control](#access-control). |
| **Long-lived API token** | `Authorization: Bearer dsk_<...>` header (same header, different format) | User-defined (default no expiry); revocable | `POST /auth/api-tokens` (self-service) | Effective role = `min(token.role_snapshot, owner.users.role)`. Same Access Control. |
| **Internal shared-secret** | `X-Internal-Token: <secret>` header | Static — operator rotated | `DATASPOKE_INTERNAL_TOKEN` from `dataspoke-secrets` K8s Secret | `/internal/*` only |

User JWTs are the default for browser clients. Long-lived API tokens are
opaque (`dsk_` prefix, 32 random URL-safe bytes), minted by users for
non-interactive clients (CI, AI agents, third-party integrations). The
middleware accepts either form on the same `Authorization: Bearer` header
and dispatches by prefix: a bearer carrying the `dsk_` API-token prefix is
resolved via API-token hash lookup (JWT decode is skipped for it); every
other bearer is JWT-decoded. Both paths populate the same identity context
and run the same role-based gate.

API-token effective privilege is the **intersection** of the token's
mint-time snapshot and the owner's current role: demoting a user
immediately downgrades all their tokens; promoting does not auto-elevate
existing tokens (mint a new one). See [feature/AUTH.md §API Tokens](feature/AUTH.md#api-tokens)
for the full lifecycle, cap (10/user), and audit semantics.

The `X-Internal-Token` is intended for cluster-local automation (Airflow
HttpOperator tasks reaching the API via cluster DNS) and install/seed
scripts (`helm-charts/bin/post-install/seed-*.sh`); its scope (`/internal/*`)
is disjoint from `Authorization: Bearer`-gated paths and it is never accepted
on `/spoke/*` or `/admin/*` routes.

### Token Strategy

DataSpoke uses **JWT (JSON Web Tokens)** for stateless authentication.

| Token type | Lifetime | Storage |
|------------|----------|---------|
| Access token | 15 minutes | Memory / `Authorization` header |
| Refresh token | 7 days | HttpOnly cookie |

### JWT Claims

Access-token payload: `sub` (user uuid), `email`, `exp`, `iat`.
Refresh-token payload: `sub` (user uuid), `exp`, `iat`, and `type` = `"refresh"`
(the claim the refresh endpoint checks to reject access tokens — see
`INVALID_REFRESH_TOKEN`).

The JWT carries identity only — it does **not** encode role. Routes are gated
by `users.role` read per-request from the DB — see
[AUTH §Privilege Model](feature/AUTH.md#privilege-model).

### Access Control

Routes are gated by **URI prefix × HTTP method × role**. The role is `users.role`
for JWT callers, or `min(token.role_snapshot, owner.users.role)` for API-token
callers. The DB lookup is in the same request transaction as other route work
— no additional round trip.

**Prefix gate**:

| URI prefix | Gate | Notes |
|------------|------|-------|
| `/auth/…` | none (public) | login, register, password reset, OAuth callback are public; `/auth/me` and `/auth/api-tokens` require a bearer-authenticated caller; `/auth/token/refresh` and `/auth/token/revoke` take the HttpOnly refresh cookie rather than a bearer token — per-route semantics in [AUTH §Refresh & revoke](feature/AUTH.md#refresh--revoke) |
| `/spoke/…` | authenticated; method × role gate applies (see below) | all authenticated users, with method-based restriction by role |
| `/admin/…` | `users.role = 'Admin'` | Admin only |

**Method × role gate** (applies on `/spoke/*`):

| Role | GET / HEAD / OPTIONS | POST / PUT / PATCH / DELETE |
|------|---------------------|-----------------------------|
| Reader | ✓ | ✗ `403 READ_ONLY_ROLE` |
| Editor | ✓ | ✓ |
| Admin | ✓ | ✓ |

`/auth/*` routes are exempt from the method gate (self-scoped writes — any
role can change own name/password, mint own API tokens, refresh own session).

Role changes (`PATCH /admin/users/{id}/role`) write `users.role` first, then
propagate to DataHub via `batchAssignRole`. Demotion takes effect on the
caller's **next request** — both for JWT sessions and API tokens (which
re-read `users.role` on every call via the intersection check). See
[AUTH §Privilege Model](feature/AUTH.md#privilege-model) and
[AUTH §Role Drift Reconciliation](feature/AUTH.md#role-drift-reconciliation).

---

## Route Catalogue

All routes are prefixed with `/api/v1`.

> **Routing principle**: The URI structure has two axes. Per-dataset, cross-feature
> operations use the canonical `/spoke/common/data/{dataset_urn}/…` surface for
> state (`attr/<feat>/`), actions (`method/<feat>/`), and events (`event/<feat>`
> or `event` for the unified per-dataset timeline). Cross-dataset list views and
> global features live under one namespace per MANIFESTO §2.1 feature:
> `/spoke/ingestion` and `/spoke/validation` are list-view aggregators over the
> per-dataset `attr/<feat>/*` data; `/spoke/ontogen` is a full singleton-conf
> surface — one global conf, a manual run trigger, an event log, and result
> collections — because the ontology is one all-connected artifact; `/spoke/metagen`
> is a **conf collection** — many named confs, each with its own run trigger and
> event feed — feeding one global cross-dataset review queue, because metadata
> writers can legitimately be many while the shared ontology holds consistency;
> `/spoke/governance` carries the metric catalogue.

### Auth

`/auth/*` carries no prefix gate; per-route auth requirements are in
[§Access Control](#access-control). Full lifecycle semantics in
[feature/AUTH.md](feature/AUTH.md).

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/auth/register` | Create a new user account (open self-service; body `{email, name, password}`; password ≥ 10 chars). Mirrors the user into DataHub as a corpuser with Reader role. |
| `POST` | `/auth/token` | Issue tokens (body `{email, password}`). Returns `{access_token, token_type: "bearer", expires_in}` and sets the refresh token as an HttpOnly cookie scoped to path `/api/v1/auth/token` |
| `POST` | `/auth/token/refresh` | Refresh access token from the HttpOnly refresh cookie |
| `POST` | `/auth/token/revoke` | Revoke refresh token (logout) |
| `GET` | `/auth/me` | Get the current user's profile — returns `{id, email, name, has_google, role, created_at, updated_at}` (all DataSpoke `users` columns; `password_hash` is never returned) |
| `PATCH` | `/auth/me` | Update own display name and/or password (body `{name?, password?}`); returns the updated profile in the same shape as `GET /auth/me` |
| `POST` | `/auth/password/reset/request` | Send password-reset email (body `{email}`). Silent for unknown emails (no account-enumeration leak). |
| `POST` | `/auth/password/reset/confirm` | Confirm reset with token + new password (body `{token, new_password}`); returns `204` on success |
| `GET` | `/auth/google/login` | Begin Google OAuth: establish state cookie and 302 to Google consent screen |
| `GET` | `/auth/google/callback` | Google OAuth callback; on success either logs in an existing user, links Google to an existing email, or creates a fresh user with Reader role |
| `GET` | `/auth/api-tokens` | List own API tokens (content key `tokens: [{id, name, role_snapshot, created_at, last_used_at, expires_at}]` — never the raw token; paginated with the standard `offset`/`limit`/`total_count` envelope, sortable by `created_at`, default `created_at_desc`). Authenticated. |
| `POST` | `/auth/api-tokens` | Mint a new API token (body `{name, expires_at?}`). Response includes the raw token in `{token: "dsk_...", id, name, role_snapshot, created_at, expires_at}` — **only time the raw token is returned plain**. `409 TOKEN_LIMIT_EXCEEDED` if user already has 10 active tokens. Authenticated. |
| `DELETE` | `/auth/api-tokens/{id}` | Revoke own API token (sets `revoked_at = now()`). Authenticated. |

### Data Resource (`/spoke/common/data`)

The canonical resource for a dataset. The collection root `GET /spoke/common/data`
lists every registered dataset with its cross-feature coverage (ingestion + metagen);
each dataset is then an item resource at `/spoke/common/data/{dataset_urn}`. Every
per-dataset surface — ingestion, validation, metagen — is a sub-resource of
`/spoke/common/data/{dataset_urn}/`.
The three meta-classifiers group sub-resources by feature: state and configuration
live under `attr/<feature>/` (`conf`, plus `result` for validation timeseries and
`item` for the per-dataset metagen review queue), action triggers under
`method/<feature>/<action>`, and lifecycle events under `event/<feature>` (or
`event` alone for the unified per-dataset timeline). Any team that owns a dataset
can register and manage its ingestion, validation, and metagen opt-in through
this single per-dataset path.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/data` | Paginated catalog of every registered dataset (`dataset_registry`, the same base set as `/ingestion/unmanaged` and `/metagen/uncovered`). Each row carries `dataset_urn`; `ingestion` (a list of `{source_id, name, mode, platform}` for **every** source that covers the dataset — empty when none, since `ingestion_source_dataset` is keyed `(source_id, dataset_urn)` and a dataset may be covered by several sources); `validation` (`{covered}`, `true` when a validation conf exists for the dataset); and `metagen` (a list of `{conf_id, name}` for enabled metagen confs whose `dataset_filter` matches the dataset — possibly empty). Composes the per-dataset ingestion reverse-lookup (all sources), the validation-coverage set, and the metagen filter-match views over one registry page. Paginated (`offset`/`limit`/`total_count`), sortable by `dataset_urn` (`dataset_urn`/`dataset_urn_desc`, default `dataset_urn_asc`) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}` | Get dataset summary (identity, owner, tags) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}/attr` | Get dataset attributes (schema summary, ownership, tags) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/ingestion` | Reverse-lookup (read-only): the source that covers this dataset, its `mode`, and the latest run (spanning the source's own runs and those booked on its internal wrappers). Ingestion is configured per-source under `/spoke/ingestion/sources` | Ingestion Control | UC1 |
| `GET` | `/spoke/common/data/{dataset_urn}/event/ingestion` | Ingestion event reports for this dataset (success/failure notices, mirrored from its source's runs incl. internal-wrapper runs; each row carries a derived `wrapper: bool`) | Ingestion Control | UC1 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Get validation configuration (`description` + declared `variables`, each variable a `{name, description}` object) | Validation | UC2, UC5 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Create or replace validation configuration. Body `{description, variables}` where each variable is `{name, description}` (`name` matches `[a-z][a-z0-9_]{0,99}` and is unique; `description` required, ≤200 chars, empty allowed). PUT for a URN absent from DataHub returns `422 DATASET_NOT_IN_DATAHUB` | Validation | UC2, UC5 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Partially update validation configuration | Validation | UC2, UC5 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Hard-delete the validation slot — removes the conf row, cascades to delete the dataset's validation results and validation events, and hard-deletes the DataHub assertion entity. Returns `204`; afterwards the dataset reads as never-created (`GET`/`PATCH` → `404 CONFIG_NOT_FOUND`) and a fresh `PUT` creates a new conf | Validation | UC2, UC5 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Append a pipeline-emitted result `{data_time, score, variables}`. Unknown variable keys return `422 UNKNOWN_VARIABLE`; `score` outside `[0,1]` returns `422 INVALID_SCORE` | Validation | UC2, UC5 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Get historical results (timeseries on `data_time`; `?from=…&until=…&limit=…` — this endpoint names its end-bound param `until` rather than the convention table's `to`; **the sole documented deviation** from the standard pagination cap: `default limit=1000`, server cap `10000`, fixed `data_time DESC` order — see [API_DESIGN_PRINCIPLE §5](API_DESIGN_PRINCIPLE_en.md#5-url-query-segments-are-for-filtering-sorting-and-pagination)) | Validation | UC2, UC5 |
| `GET` | `/spoke/common/data/{dataset_urn}/event/validation` | Validation event reports (success/failure notices) | Validation | UC2, UC5 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/metagen/boundary` | Get per-dataset metagen boundary (`dataset_urn`, `is_enabled`, `allowed`, `created_at`, `updated_at`). Returns `200` with a `null` body when no boundary exists (unlike validation `conf`, which `404`s) | Metadata Generation | UC4 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/metagen/boundary` | Create or replace the per-dataset boundary; sets which element kinds (`dataset.description`, `column.description`) any conf's generator may write on this dataset | Metadata Generation | UC4 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/metagen/boundary` | Partially update the boundary | Metadata Generation | UC4 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/metagen/boundary` | Remove the boundary — dataset is excluded from future runs | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/metagen/item` | List items for this dataset (each row carries `item_id`, `kind`, `status`, `candidate_count`, `created_at`). Paginated, sortable by `created_at`/`updated_at` (default `created_at_desc`). The response envelope also carries a dataset-level `candidate_count` — total candidates of any status for this dataset (`COUNT(*)` over `metagen_candidates`), the same measure the per-dataset rollup reports — distinct from `total_count` (the item count) | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/metagen/item/{item_id}` | Item detail including all candidates (`candidate_id`, `conf_id`, `conf_name`, `item_id`, `dataset_urn`, `value`, `confidence_score`, `status`, `evidence`, `run_id`, `created_at`, `reviewed_at`, `reviewer_id`) | Metadata Generation | UC4 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` | Review a candidate — body `{"verdict": "approve"\|"reject", "reason": "…"}`. Approve writes the candidate `value` to the corresponding editable DataHub aspect; if a sibling on the same item was previously `approved`, it is atomically demoted to `llm_approved` so the new approval supersedes it. Reject is valid on both `llm_approved` and `approved` candidates: rejecting an `llm_approved` candidate flips it to `rejected` with no DataHub write; rejecting an `approved` candidate flips it to `rejected` **and removes the editable DataHub description it had written**. Returns `422 METAGEN_DATASET_NOT_IN_BOUNDARY` if the dataset has no `is_enabled=true` boundary | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/event/metagen` | Per-dataset metagen events (`METAGEN.CANDIDATE_APPROVE`, `METAGEN.CANDIDATE_REJECT`) | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/event` | The **complete per-dataset timeline** — a single newest-first feed that unions the covering source's ingestion runs (resolved by reverse-lookup, incl. its internal-wrapper runs) with this dataset's validation and metagen events. Each row carries a derived `wrapper: bool` (`true` for an ingestion event originating on a linked wrapper). Repeatable `event_major_type` filter (`INGESTION`/`VALIDATION`/`METAGEN`; omitted = all). Paginated (`offset`/`limit`), `from`/`to` time-range, default `occurred_at_desc` | Data Resource | UC1, UC2, UC4 |

> **Event endpoints share one envelope.** Every `event/<feature>` feed and the
> unified `event` timeline return standard `EventResponse` rows and support
> pagination (`offset`/`limit`, sortable by `occurred_at`, default
> `occurred_at_desc`) plus a `from`/`to` time-range. Each row always carries
> `wrapper: bool`; it is only meaningful for ingestion events (`true` when the
> event originates on a linked DataHub-CLI wrapper) and is `false` for
> validation and metagen events.

### Redefined DataHub Functions *(TBD)*

Future routes for blended dataset creation and modification. Example candidates:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/spoke/common/data` | Create a dataset — write core metadata to DataHub and initialize DataSpoke-side records in a single call |
| `PATCH` | `/spoke/common/data/{dataset_urn}` | Update dataset metadata — blend DataHub aspect writes with DataSpoke-specific updates |

These routes are **not yet defined**; scope and design will be specified when the feature is
planned. See [DATAHUB_INTEGRATION §Key principles](DATAHUB_INTEGRATION.md#overview).

### Ingestion (`/spoke/ingestion`)

Ingestion is modeled **per source / recipe** — one source produces many datasets, mirroring
DataHub. DataSpoke's goals: **augment** DataHub's native ingestion with a custom, forkable
extractor for sources DataHub's connectors can't cover, and **make all ingestion visible** —
which datasets each source covers, and which are ingested in an unmanaged way.

Three modes (`mode` on each source): `DATAHUB_MANAGED` (DataHub's own recipe + cron; DataHub
SSOT; **read-only** in DataSpoke — synced down via `listIngestionSources`), `ACTIVE_CUSTOM_MANAGED`
(DataSpoke's pluggable extractor crawls + emits on a tier schedule), `PASSIVE` (ingested outside
DataHub/DataSpoke; DataSpoke records the registration + a declared `AllowDenyPattern` scope and
syncs results). The recipe is stored DataHub-compatible (`recipe.source.{type,config}`); secrets
are referenced as `${name__key}` and resolved from K8s Secret `dataspoke-source-cred-<name>`.

Source→dataset mapping is rebuilt by the hourly sync DAG (not a public mutation) by evaluating
each source's filter-matcher against the DataHub dataset set, optionally enriched by
`systemMetadata.pipelineName` for the two MANAGED modes. Datasets covered by no source form the
**unmanaged bucket** (`GET /spoke/ingestion/unmanaged`). Design, sync, and aspect details: see
[BACKEND §Ingestion Service](feature/BACKEND.md#ingestion-service-srcbackendingestion)
and [DATAHUB_INTEGRATION §Ingestion Source Sync](DATAHUB_INTEGRATION.md#ingestion-source-sync).

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/ingestion/sources` | List ingestion sources (paginated, sortable by `created_at`/`updated_at`, default `created_at_desc`; filter by `mode`). DataHub-managed CLI wrapper sources are internal and never listed — `mode=DATAHUB_MANAGED` returns regular sources only | Ingestion Control | UC1 |
| `POST` | `/spoke/ingestion/sources` | Create a source (`ACTIVE_CUSTOM_MANAGED` or `PASSIVE` only; `DATAHUB_MANAGED` is synced, not created); `422 SECRET_REF_MALFORMED` for malformed `${name__key}` recipe references | Ingestion Control | UC1 |
| `GET` | `/spoke/ingestion/sources/{id}` | Get one source as JSON (recipe `${name__key}` references returned as-is; any plaintext secret value masked) | Ingestion Control | UC1 |
| `PUT` | `/spoke/ingestion/sources/{id}` | Replace a source; `409 INGESTION_SOURCE_READONLY` for `DATAHUB_MANAGED`; `422 SECRET_REF_MALFORMED` for malformed `${name__key}` recipe references | Ingestion Control | UC1 |
| `PATCH` | `/spoke/ingestion/sources/{id}` | Partially update a source; `409 INGESTION_SOURCE_READONLY` for `DATAHUB_MANAGED`; `422 SECRET_REF_MALFORMED` for malformed `${name__key}` recipe references | Ingestion Control | UC1 |
| `DELETE` | `/spoke/ingestion/sources/{id}` | Remove a source (+ cascade its dataset mappings); `409 INGESTION_SOURCE_READONLY` for `DATAHUB_MANAGED` | Ingestion Control | UC1 |
| `POST` | `/spoke/ingestion/sources/{id}/method/run` | Execute the extractor; `?dry_run=true` runs a connection check + discovery preview (connects, crawls `information_schema`, applies `schema_pattern`) that reports the datasets it would emit without writing. `ACTIVE_CUSTOM_MANAGED` only — `409 INGESTION_RUN_NOT_APPLICABLE` otherwise; concurrent runs return `409 INGESTION_RUNNING`. Both the run-response `detail` and the `INGESTION.COMPLETE`/`INGESTION.FAIL` event `detail` carry `dry_run`, `discovered_urns` / `discovered_urns_count` (dataset URNs passing the filter — the "would emit" plan, present on both dry-run and real runs), `emitted_urns` / `emitted_urns_count` (dataset URNs actually written to DataHub; empty with count `0` on a dry-run), `errors`, `warnings`; the run-response additionally surfaces `run_id` and `status` as top-level fields, and the event `detail` additionally carries `run_id` and `platform`. `emitted_urns ⊆ discovered_urns`; on a real run `discovered_urns_count − emitted_urns_count > 0` signals per-table emit failures | Ingestion Control | UC1 |
| `GET` | `/spoke/ingestion/sources/{id}/datasets` | Datasets this source covers (the mapping; each row carries `authority` + `derivation`); paginated, sortable by `dataset_urn`/`first_seen_at`/`last_seen_at` (default `dataset_urn_asc`) | Ingestion Control | UC1 |
| `GET` | `/spoke/ingestion/sources/{id}/event` | Run/event history for the source, including runs booked on its internal DataHub CLI wrapper sources (paginated, sortable by `occurred_at`, default `occurred_at_desc`). Each row carries a derived `wrapper: bool` — `true` for an event originating on a linked wrapper rather than the source itself | Ingestion Control | UC1 |
| `GET` | `/spoke/ingestion/unmanaged` | DataHub datasets (`dataset_registry.datahub_registered=true`) covered by no ingestion source (paginated, sortable by `dataset_urn`/`created_at`/`updated_at`, default `dataset_urn_asc`) — the registry is refreshed hourly by the `datahub-sync-hourly` sweep | Ingestion Control | UC1 |
| `GET` | `/spoke/ingestion/secrets` | List source-credential references available to recipes — one row per `(secret, key)` under the `dataspoke-source-cred-` prefix, as `{ref: "name__key", secret_name, key}`. **Values are never returned.** Paginated (in-memory slice + count over the enumerated K8s Secret refs) and sortable by `ref` (default `ref_asc`). Admins author the K8s Secrets out-of-band (`kubectl create secret generic dataspoke-source-cred-<name> --from-literal=<key>=… -n <dataspoke-ns>`; the source editor UI renders this authoring guide next to the reference list — DataSpoke has no secret-write API, the model is reference-only); a recipe then references one as `${name__key}`. **Requires Editor or Admin** (`403 READ_ONLY_ROLE` for Reader) — exception to the Reader-GET rule, since enumerating which credential refs exist is author-only tooling | Ingestion Control | UC1 |

**Source body shape.** The request and response bodies are JSON whose fields mirror the UC1
recipe YAML 1:1, using **DataHub-recipe-standard wording only** — no DataSpoke-specific field
names. The frontend renders/edits this JSON as YAML; the two are lossless transforms of each
other. A `GET` response (and `POST`/`PUT`/`PATCH` body) carries:

```jsonc
{
  "mode": "ACTIVE_CUSTOM_MANAGED",          // DATAHUB_MANAGED | ACTIVE_CUSTOM_MANAGED | PASSIVE
  "name": "dummy postgres example_db in catalog schema",
  "schedule": "0 0 * * *",                  // cron string; null = manual-only (no tier DAG); omit for PASSIVE
  "recipe": {                               // DataHub-compatible; recipe.source byte-compatible with a DataHub recipe
    "source": {
      "type": "postgres",
      "config": { "host_port": "…", "password": "${dummy-data-pg__password}", "schema_pattern": { "allow": ["^catalog$"] }, "env": "DEV" }
    }
  }
}
```

Responses additionally include read-only management fields outside the recipe-standard set:
`id`, `status`, `platform` (derived from `recipe.source.type`), `created_at`, `updated_at`, and
`datahub_source_urn` (for `DATAHUB_MANAGED`). DataHub CLI wrapper sources (auto-created when a
registered source is run) are internal plumbing — they are linked to their registered parent, never
listed, and their runs surface on the parent via `GET /sources/{id}/event` (with `wrapper: true`).
On `GET`, `${name__key}` references inside `recipe` are returned as-is — they are pointers to
K8s Secrets, not secret values; any plaintext secret value is **masked**. There is no
`schedule_tier`/`schedule_cron`/`is_enabled` on the wire — the tier is derived server-side from
`schedule`, and `schedule: null` is the manual-only ("paused") state. See
[`USE_CASE_en.md §UC1`](USE_CASE_en.md#uc1-ingestion-control) for the YAML form of each mode.

### Validation (`/spoke/validation`)

A cross-dataset list view of validation attributes. Each row combines dataset identity
with the validation attributes stored under `common/data/{dataset_urn}/attr/validation/*`
(`conf` — description and declared variable names — and the latest `result` —
`data_time` and `score`). Useful for quality dashboards and per-dataset overviews.

DataSpoke is a passive result store for one validation slot per dataset. Data pipelines
run the checks and POST results; DataSpoke stores them, emits DataHub assertion aspects,
and serves the historical timeseries as a baseline cache. Teams that need multiple
distinct checks per dataset use DataHub's native assertion APIs directly. Full contract:
see [`spec/feature/VALIDATION.md`](feature/VALIDATION.md). Backend service surface:
[BACKEND §Validation Service](feature/BACKEND.md#validation-service-srcbackendvalidation).
DataHub aspect mapping: [DATAHUB_INTEGRATION §Assertion Aspects](DATAHUB_INTEGRATION.md#assertion-aspects).

Per-dataset detail and result writes live on the canonical `data/{dataset_urn}` surface:
`attr/validation/{conf,result}` and `event/validation`. Pipelines POST results to
`attr/validation/result` after each partition write.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/validation` | List validation attributes across datasets — each row aggregates the per-dataset `attr/validation/*` (conf description + variable count + latest result `data_time` and `score`) (paginated, sortable by `dataset_urn`/`updated_at`, default `updated_at_desc`; filterable). The `coverage` query param (`covered` \| `uncovered` \| `both`, default `covered`) selects the row set: `covered` returns datasets that hold a validation slot (current behavior); `uncovered` returns registered datasets (`dataset_registry`) with no validation conf; `both` unions them. Uncovered rows carry null `description`, null `variable_count`, null `latest_data_time`, and null `latest_score`; in `uncovered`/`both` the ordering is tiebroken by `dataset_urn` (null `updated_at` sorts last) so paging stays deterministic | Validation | UC2, UC5 |

### Ontology Generation (`/spoke/ontogen`)

The ontology is a global artifact, so its conf, seeds, manual run trigger, and
inference-run event log are singletons rooted at `/spoke/ontogen` rather than
under any dataset URN. Inference output follows a **subject / predicate / object
triple model** with three independently reviewable result types — `node` (subject /
object), `edge` (predicate), and `triple` (`(subject_node, edge, object_node)` fact).
A triple may be human-approved only when its endpoint nodes and edge are themselves
`status='approved'` (an `llm_approved` dependency does NOT satisfy the gate); review
proceeds nodes → edges → triples.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/ontogen/attr/conf` | Get singleton operational conf (`is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) | Ontology Generation | UC3 |
| `PUT` | `/spoke/ontogen/attr/conf` | Create or replace operational conf | Ontology Generation | UC3 |
| `PATCH` | `/spoke/ontogen/attr/conf` | Partially update operational conf | Ontology Generation | UC3 |
| `DELETE` | `/spoke/ontogen/attr/conf` | Remove operational conf (effectively disables) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/attr/seed` | List seeds — returns **all** seeds (enabled and disabled) as `[{seed_id, updated_at, is_enabled, preview}]` (preview is a short Markdown snippet); the seed body is fetched per-seed below. Paginated, sortable by `created_at`/`updated_at` (default `updated_at_desc`) | Ontology Generation | UC3 |
| `POST` | `/spoke/ontogen/attr/seed` | Create an inference seed — body is a raw Markdown document (`Content-Type: text/markdown`); server assigns `seed_id`. Created **disabled** (`is_enabled = false`) — it does not participate in inference until explicitly enabled | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/attr/seed/{seed_id}` | Get seed Markdown document (`Content-Type: text/markdown`) | Ontology Generation | UC3 |
| `PATCH` | `/spoke/ontogen/attr/seed/{seed_id}` | Replace seed Markdown body (`Content-Type: text/markdown`) | Ontology Generation | UC3 |
| `PATCH` | `/spoke/ontogen/attr/seed/{seed_id}/attr/enabled` | Enable or disable a seed — body JSON `{is_enabled: bool}` (`Content-Type: application/json`). A disabled seed is retained and fully visible but excluded from the inference pipeline; reversible both ways | Ontology Generation | UC3 |
| `DELETE` | `/spoke/ontogen/attr/seed/{seed_id}` | Hard-delete a seed — the row is removed outright | Ontology Generation | UC3 |
| `POST` | `/spoke/ontogen/method/run` | Trigger a manual re-inference. Optional `Content-Type: text/markdown` body acts as a **one-shot prompt** for this run, on top of the persistent seeds (not stored). With no body — including periodic Airflow invocations — falls back to `attr/conf.default_run_prompt`. `?dry_run=true` evaluates without persisting. Concurrent runs return `409 ONTOGEN_RUNNING`. Rejected with `409 ONTOGEN_DISABLED` when the conf is disabled and `dry_run` is not true | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/event` | Global inference-run event history (e.g. `ONTOGEN.RUN_COMPLETE`, `ONTOGEN.RUN_FAILED`). Paginated, sortable by `occurred_at` (default `occurred_at_desc`) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/node` | List nodes (subjects / objects). Each row carries `confidence_score`, `status`, `created_at`, and `run_id` (uuid, nullable — the inference run that produced the row; `null` for seeded rows). Supports `?sort=created_at_asc\|created_at_desc` (default `created_at_desc`); filterable by `?status`. Per the [sort convention](API_DESIGN_PRINCIPLE_en.md#5-url-query-segments-are-for-filtering-sorting-and-pagination) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/node/{node_id}` | Get node detail (incl. member datasets) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/node/{node_id}/event` | Node-level change history | Ontology Generation | UC3 |
| `POST` | `/spoke/ontogen/result/node/{node_id}/method/review` | Review a pending node — body: `{"verdict": "approve"\|"reject", "reason": "…"}` | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/edge` | List edges (predicates). Each row carries `confidence_score`, `status`, `created_at`, and `run_id` (uuid, nullable — the inference run that produced the row; `null` for seeded rows). Supports `?sort=created_at_asc\|created_at_desc` (default `created_at_desc`); filterable by `?status`. Per the [sort convention](API_DESIGN_PRINCIPLE_en.md#5-url-query-segments-are-for-filtering-sorting-and-pagination) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/edge/{edge_id}` | Get edge detail | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/edge/{edge_id}/event` | Edge-level change history | Ontology Generation | UC3 |
| `POST` | `/spoke/ontogen/result/edge/{edge_id}/method/review` | Review a pending edge — body: `{"verdict": "approve"\|"reject", "reason": "…"}` | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/triple` | List triples — `(subject_node_id, edge_id, object_node_id)` facts. Each row carries `confidence_score`, `status`, `created_at`, and `run_id` (uuid, nullable — the inference run that produced the row; `null` for seeded rows). Supports `?sort=created_at_asc\|created_at_desc` (default `created_at_desc`); filterable by `?status`. Per the [sort convention](API_DESIGN_PRINCIPLE_en.md#5-url-query-segments-are-for-filtering-sorting-and-pagination) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/triple/{triple_id}` | Get triple detail (resolved subject node, edge, object node) | Ontology Generation | UC3 |
| `GET` | `/spoke/ontogen/result/triple/{triple_id}/event` | Triple-level change history | Ontology Generation | UC3 |
| `POST` | `/spoke/ontogen/result/triple/{triple_id}/method/review` | Review a triple — body: `{"verdict": "approve"\|"reject", "reason": "…"}`. Returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` if any of subject node, edge, or object node is not yet `status='approved'` (an `llm_approved` dependency does not satisfy the gate) | Ontology Generation | UC3 |

**Payload caps** (validated at the schema layer; cap violations return `422`):
- `attr/conf.default_run_prompt` ≤ 16,000 chars
- `attr/conf.dataset_filter.{tags,glossary_terms,dataset_urns}` ≤ 1,000 entries per dimension
- `attr/seed` Markdown body ≤ 128 KiB
- `method/run` one-shot Markdown body ≤ 128 KiB
- node / edge / triple `method/review.reason` ≤ 2,000 chars

### Metadata Generation (`/spoke/metagen`)

Metadata generation proposes documentation for **editable** DataHub description
aspects — table descriptions and column descriptions — and lets reviewers approve
one value per slot. Confs are a **managed collection**: many named confs can coexist,
each with its own `dataset_filter`, `schedule_tier`, and generation budget, so teams
run different documentation policies over different dataset groups. Cross-conf
consistency is held by the shared UC3 ontology that every conf reads, not by a single
conf. Each conf carries its own run trigger and event feed; all confs feed **one
global cross-dataset review queue** (`/spoke/metagen/item`). A complementary
**per-dataset rollup** (`/spoke/metagen/dataset`) aggregates that queue into one
row per dataset — item count, candidate-level approved/rejected/total counts, and
the boundary — with a `conf_id` filter that scopes both membership and counts to a
single conf.

Per-dataset participation is opt-in via a separate boundary row at
`/spoke/common/data/{dataset_urn}/attr/metagen/boundary`. A dataset is generated for a
conf only when it matches that conf's `dataset_filter` **and** has an `is_enabled=true`
boundary; the boundary's `allowed` array caps which element kinds any conf may write on
that dataset.

The **result granularity is the item, not the run**. An `item` is one
editable-metadata slot — either `dataset.description` for a dataset, or
`column.<fieldPath>.description` for one column. Items are shared across confs (keyed by
`(dataset_urn, item_id)`); each candidate carries the `conf_id`/`conf_name` that produced
it. The `result_limit`/`overwrite_pending` budget applies per `(conf_id, dataset_urn,
item_id)`. A reviewer approves at most one candidate **per item globally across all
confs** (immediate DataHub emit, item locked from further generation) — approving a
candidate from one conf atomically demotes an approved sibling from any other conf — and
may reject any number of candidates (rejected candidates are deleted at the start of the
next run). Service surface:
[BACKEND §Metadata Generation Service](feature/BACKEND.md#metadata-generation-service-srcbackendmetagen).
LLM step (producer-reviewer adversarial debate, identical wiring to UC3
ontogen): [BACKEND_LLM §Metagen Adversarial Debate](feature/BACKEND_LLM.md#metagen-adversarial-debate).

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/metagen/conf` | List confs (paginated, sortable by `created_at`/`updated_at`/`name`, default `created_at_desc`). Each row also carries `dataset_affected_count` — distinct datasets that already hold a candidate from this conf (`COUNT(DISTINCT dataset_urn)` over `metagen_candidates`, DB-only, no live DataHub call) — and `last_run_at`, the newest run-complete/run-failed event time for the conf (`null` if it has never run) | Metadata Generation | UC4 |
| `POST` | `/spoke/metagen/conf` | Create a conf — body `{name, is_enabled, schedule_tier, dataset_filter, result_limit, overwrite_pending}`; `name` unique (`409 METAGEN_CONF_EXISTS` on collision) → `201` | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/conf/{conf_id}` | Get one conf | Metadata Generation | UC4 |
| `PUT` | `/spoke/metagen/conf/{conf_id}` | Replace a conf; `404 METAGEN_CONF_NOT_FOUND` when absent | Metadata Generation | UC4 |
| `PATCH` | `/spoke/metagen/conf/{conf_id}` | Partially update a conf | Metadata Generation | UC4 |
| `DELETE` | `/spoke/metagen/conf/{conf_id}` | Hard-delete a conf — retains every item, candidate (all statuses), and candidate embedding it produced, orphaning them (`conf_id` → `NULL`) as parentless results with no re-linking; already-approved descriptions stay in DataHub | Metadata Generation | UC4 |
| `POST` | `/spoke/metagen/conf/{conf_id}/method/run` | Trigger a manual generation run for this conf. Optional body `{"dataset_urns": [...]}` narrows scope; `?dry_run=true` evaluates without persisting. Concurrent runs of the same conf return `409 METAGEN_RUNNING`. Rejected with `409 METAGEN_DISABLED` when the conf is disabled and `dry_run` is not true | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/conf/{conf_id}/event` | Per-conf generation-run event history (e.g. `METAGEN.RUN_COMPLETE`, `METAGEN.RUN_FAILED`). Paginated, sortable by `occurred_at` (default `occurred_at_desc`) | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/conf/{conf_id}/dataset` | Datasets this conf's `dataset_filter` matches (covered). Paginated, sortable by `dataset_urn` (default `dataset_urn_asc`). Each row carries `dataset_urn`, `is_enabled`, `allowed`, `blocked` (bool), `reason`. With `?include_disallowed=true` (default `false`), also includes boundary-blocked covered datasets (boundary missing / disabled / empty `allowed`), mirroring `/uncovered`. `404 METAGEN_CONF_NOT_FOUND` when absent | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/uncovered` | Registered datasets (`dataset_registry.datahub_registered=true`) reached by no enabled conf (paginated, sortable by `dataset_urn`, default `dataset_urn_asc`). Each row carries a `reason`. With `?include_disallowed=true` (default `false`), also includes datasets a conf matches but the boundary blocks (missing / disabled / empty `allowed`) | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/event` | Cross-conf union of all confs' generation-run events (e.g. `METAGEN.RUN_COMPLETE`, `METAGEN.RUN_FAILED`). Paginated, sortable by `occurred_at` (default `occurred_at_desc`) | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/item` | List items across datasets and confs (paginated, sortable by `created_at`/`updated_at`/`dataset_urn` (default `created_at_desc`); filterable by `dataset_urn`, `kind`, `status`, `conf_id`). Each row carries `dataset_urn`, `item_id`, `kind`, `field_path`, `status`, `candidate_count`, `created_at`, `composite_id` — `conf_id`/`conf_name` surface per-candidate at the item-detail route, not on these item rows | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/item/{composite_id}` | Item detail by composite id `{dataset_urn}::{item_id}` — includes all candidates (with `conf_id`/`conf_name`) and their statuses | Metadata Generation | UC4 |
| `GET` | `/spoke/metagen/dataset` | Per-dataset rollup of generation results (paginated, sortable by `last_modified_at` (default `last_modified_at_desc`); filterable by `dataset_urn` text and `conf_id`). Each row carries `dataset_urn`, `is_enabled`, `allowed` (boundary; `is_enabled=false`/`allowed=[]` when none), `item_count`, candidate-level `approved_count`/`rejected_count`/`candidate_count`, and `last_modified_at`. Counts are candidate-level; with `conf_id` set, rows are restricted to datasets holding a candidate from that conf and counts are scoped to that conf's candidates | Metadata Generation | UC4 |

`uncovered` `reason` values: `no_conf_match` (matched by no enabled conf's
`dataset_filter`) and `boundary_blocked` (matched by a conf but the boundary is
missing, disabled, or has an empty `allowed` — only surfaced when
`include_disallowed=true`).

**Payload caps** (validated at the schema layer; cap violations return `422`):
- conf `dataset_filter.{tags,glossary_terms,dataset_urns}` ≤ 1,000 entries per dimension
- conf `result_limit` ∈ `[1, 20]`
- candidate `value` Markdown body ≤ 16 KiB
- candidate `method/review.reason` ≤ 2,000 chars

### Governance (`/spoke/governance`)

#### Metric (`/spoke/governance/metric`)

Governance metrics are named, scheduled aggregations over the data estate. Each metric
carries a definition (`attr/conf`) that controls how it is computed and scheduled, and a
timeseries of measurement results (`attr/result`). Metrics represent enterprise-wide or
department-wide signals rather than per-dataset observations.

> **Pure aggregation principle**: A metric does not observe the data estate directly. It
> aggregates results that already exist in DataHub metadata or DataSpoke validation results.

Metrics are read-only consumers of DataHub metadata — they never write aspects or connect
to source databases. Built-in metric types, mode semantics, and result shape: see
[USE_CASE §UC5](USE_CASE_en.md#uc5-governance) and
[BACKEND §Metrics Service](feature/BACKEND.md#metrics-service-srcbackendmetrics).
DataHub aspects consumed by `doc-health` are listed in
[DATAHUB_INTEGRATION §Aspect Usage by Feature](DATAHUB_INTEGRATION.md#aspect-usage-by-feature).

**`metric_id`**: Kebab-case slug, **client-supplied** (e.g. `ingestion-freshness`,
`validation-score`, `doc-health`). On create it is carried in the `POST /spoke/governance/metric`
request body and must be unique — a colliding id returns `409 METRIC_EXISTS`. Used in
route paths for read/update/delete and as the DAG-name suffix `metrics-{metric_id}`.

**Definition body** (`POST /spoke/governance/metric`, PUT/PATCH `.../attr/conf`):

| Field | Type | Notes |
|---|---|---|
| `metric_id` | string | **Create only** (`POST /spoke/governance/metric` body). Kebab-case slug matching `^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$\|^[a-z0-9]$`; `422 INVALID_PARAMETER` on bad format, `409 METRIC_EXISTS` on collision. On PUT/PATCH the id comes from the path. The create-body `metric_id` is returned as `id` in responses |
| `mode` | `"active"` \| `"passive"` | `passive` is reserved; create/PUT with `mode: "passive"` returns `501 NOT_IMPLEMENTED` |
| `is_enabled` | bool | Required; controls scheduled execution |
| `metric_type` | `"ingestion-freshness"` \| `"validation-score"` \| `"doc-health"` | Unsupported values return `422 INVALID_PARAMETER` |
| `title` | string | Display title |
| `description` | string | What the metric measures |
| `metrics` | string[] | Subset of the type's emitted keys (see USE_CASE §UC5); unknown keys return `422 INVALID_PARAMETER` |
| `metric_conf` | object | Type-specific. `ingestion-freshness` and `validation-score` require `time_window_sec` (positive int seconds) — the fallback window when no per-dataset window can be derived (see USE_CASE §UC5); `doc-health` takes `{}` |
| `schedule_tier` | `"hourly"` \| `"daily"` \| `"weekly"` \| null | When null, the metric runs only on-demand |
| `dataset_filter` | object | Optional `{origin?, tags?[], glossary_terms?[], dataset_urns?[]}`. `origin` is the DataHub `FabricType` value carried as the third URN segment (e.g. `PROD`/`DEV`/`CORP`/`EI`/`STG`/`NON_PROD`/…); DataSpoke forwards it verbatim and lets DataHub reject unknowns, but an empty-or-whitespace-only `origin` is rejected at PUT/PATCH with `422 INVALID_PARAMETER`. The other three dimensions are OR-ed among themselves and AND-ed with `origin`. `{}` = all datasets. See [DATAHUB_INTEGRATION §Origin filter group](DATAHUB_INTEGRATION.md#origin-filter-group) for the resolver shape |

`dataset_urns` URN format is validated at PUT/PATCH (`422 INVALID_DATASET_URN`);
unresolved-at-runtime entries are skipped and reported in the `METRIC.RUN_COMPLETE`
event's `unresolved_urns` field. UC3's `ontogen/attr/conf.dataset_filter` and UC4's
per-conf `metagen/conf.dataset_filter` use the same four-dimension shape and the same
validation.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/governance/metric` | List all metrics (paginated, sortable by `created_at`/`updated_at`/`title` (default `created_at_desc`); filterable by `metric_type`, `mode`, `is_enabled`). The display label is `title` — `metric_definitions` has no `name` column. Each row also carries `last_run_at` (the `occurred_at` of the latest `METRIC.RUN_COMPLETE` event for the metric, or `null` when it has never completed a run) | Governance | UC5 |
| `POST` | `/spoke/governance/metric` | Create a metric; `metric_id` supplied in body. Returns `409 METRIC_EXISTS` on a colliding id, `501 NOT_IMPLEMENTED` when `mode: "passive"` | Governance | UC5 |
| `GET` | `/spoke/governance/metric/{metric_id}` | Get metric summary (identity, mode, metric_type, enabled status) | Governance | UC5 |
| `GET` | `/spoke/governance/metric/{metric_id}/attr` | Get metric attributes overview (`id`, `title`, mode, metric_type, schedule_tier, enabled status, latest `values`, `latest_measured_at`) | Governance | UC5 |
| `GET` | `/spoke/governance/metric/{metric_id}/attr/conf` | Get full metric definition | Governance | UC5 |
| `PUT` | `/spoke/governance/metric/{metric_id}/attr/conf` | Replace an existing metric definition; `404 METRIC_NOT_FOUND` when the id is absent (use `POST /spoke/governance/metric` to create) | Governance | UC5 |
| `PATCH` | `/spoke/governance/metric/{metric_id}/attr/conf` | Update metric definition fields | Governance | UC5 |
| `DELETE` | `/spoke/governance/metric/{metric_id}/attr/conf` | Remove metric definition | Governance | UC5 |
| `GET` | `/spoke/governance/metric/{metric_id}/attr/result` | Get measurement results (each row carries `values: dict[str,float]` and `breakdown`; paginated, sortable by `measured_at`; `?from=…&to=…` for time range) | Governance | UC5 |
| `POST` | `/spoke/governance/metric/{metric_id}/method/run` | Trigger a metric measurement run; `?dry_run=true` evaluates without persisting. Concurrent runs return `409 METRIC_RUNNING`. Rejected with `409 METRIC_DISABLED` when the metric is disabled and `dry_run` is not true | Governance | UC5 |
| `GET` | `/spoke/governance/metric/{metric_id}/event` | Metric run events (run completions, definition changes). Paginated, sortable by `occurred_at` (default `occurred_at_desc`), `from`/`to` time-range | Governance | UC5 |

**Payload caps** (validated at the schema layer; cap violations return `422`):
- `attr/conf.dataset_filter.{tags,glossary_terms,dataset_urns}` ≤ 1,000 entries per dimension

### Admin (`/admin`)

Operator and system routes accessible to users with the DataSpoke `Admin` role.
Admin status is checked per request from DataSpoke `users.role` (or
`min(role_snapshot, users.role)` for API-token callers) — the JWT does not carry
an `admin` claim. Internal mirrors of selected routes live
under `/internal/admin/…` for unattended automation (Airflow DAGs, scripts) —
the internal mount is gated by the `X-Internal-Token` shared-secret header
instead of a JWT.

| Method | Path | Body | Response | Auth |
|--------|------|------|----------|------|
| `POST` | `/admin/dags/verify` | — | `{found, missing, total_expected}` | JWT + Admin role |
| `GET` | `/admin/dags` | — | `{groups: [{group, paused, mixed, dags: [{dag_id, paused}]}]}` — schedule (paused) state of the six controllable DAG groups. A fixed status object, **not** a record collection: no pagination or `sort` | JWT + Admin role |
| `PATCH` | `/admin/dags/{group}` | `{paused: bool}` | updated group status `{group, paused, mixed, dags: [{dag_id, paused}]}` | JWT + Admin role |
| `GET` | `/admin/conf` | — | runtime config (behavioral tunables + `updated_at`) | JWT + Admin role |
| `PATCH` | `/admin/conf` | partial conf fields | updated runtime config | JWT + Admin role |
| `GET` | `/admin/peripherals` | — | `{datahub: {is_configured}, langfuse: {is_configured}, smtp: {is_configured}}` — quick status overview consumed by the admin landing page. A fixed status object, **not** a record collection: no pagination or `sort` | JWT + Admin role |
| `GET` | `/admin/users` | — | paginated list of DataSpoke users (standard `offset`/`limit`/`total_count` envelope; content key `users: [{id, email, name, has_google, role, created_at, updated_at}]` — `role` from the DB column). Sortable by `created_at`/`updated_at`/`email` (default `created_at_desc`) | JWT + Admin role |
| `PATCH` | `/admin/users/{id}` | `{name}` | updated user | JWT + Admin role |
| `PATCH` | `/admin/users/{id}/role` | `{role: "Admin"\|"Editor"\|"Reader"}` | `{role}` | JWT + Admin role |
| `DELETE` | `/admin/users/{id}` | — | `204` | JWT + Admin role |
| `GET` | `/admin/users/{id}/api-tokens` | — | a user's API tokens (same shape as `GET /auth/api-tokens`, sans raw token; paginated with the standard `offset`/`limit`/`total_count` envelope, sortable by `created_at`, default `created_at_desc`) | JWT + Admin role |
| `DELETE` | `/admin/users/{id}/api-tokens/{token_id}` | — | `204` — revokes a user's token (incident response) | JWT + Admin role |
| `GET` | `/admin/peripherals/datahub` | — | current DataHub config: `{gms_url, kafka_brokers, token, service_corpuser_urn, default_env, is_configured, updated_at}`. `token` is masked (`""` unset, `"********"` set); `service_corpuser_urn` and `default_env` are non-secret and returned plain | JWT + Admin role |
| `PATCH` | `/admin/peripherals/datahub` | partial DataHub fields | updated DataHub config (with `token` masked) | JWT + Admin role |
| `GET` | `/admin/peripherals/langfuse` | — | current Langfuse config: `{host, public_key, secret_key, project_id, environment_tag, is_configured, updated_at}`. `secret_key` is masked (`""` unset, `"********"` set); `project_id` and `environment_tag` are non-secret and returned plain | JWT + Admin role |
| `PATCH` | `/admin/peripherals/langfuse` | partial Langfuse fields | updated Langfuse config (with `secret_key` masked) | JWT + Admin role |
| `GET` | `/admin/peripherals/smtp` | — | current SMTP config: `{host, port, username, from_address, use_tls, password, is_configured, updated_at}`. `password` is masked (`""` unset, `"********"` set) | JWT + Admin role |
| `PATCH` | `/admin/peripherals/smtp` | partial SMTP fields | updated SMTP config (with `password` masked) | JWT + Admin role |

`/admin/dags` is **operational schedule control**: it pauses and unpauses the periodic
DAGs that Airflow runs, and Airflow is the SSOT for paused state (DataSpoke keeps no copy).
This is a distinct axis from `/admin/peripherals` (external **connections** — DataHub,
Langfuse, SMTP) and from `/admin/conf` (behavioral **tunables**). Airflow is not a peripheral;
a DAG's paused state and a feature's conf-level enablement are independent — a paused DAG never
fires regardless of enabled confs, and an unpaused DAG still skips disabled confs at run time.

The six controllable groups and their member DAGs (the group→DAG map is owned by the backend;
see [`spec/feature/BACKEND.md` §DAG Catalogue](feature/BACKEND.md#dag-catalogue)):

| `group` | Member DAGs |
|---------|-------------|
| `datahub_sync` | `datahub-sync-hourly` |
| `auth_role_sync` | `auth-role-sync-daily` |
| `ingestion_active` | `ingestion-active-{hourly,daily,weekly}` |
| `ontogen` | `ontogen-{hourly,daily,weekly}` |
| `metagen` | `metagen-{hourly,daily,weekly}` |
| `metrics` | `metrics-{hourly,daily,weekly}` |

`GET /admin/dags` reads paused state for every member DAG in one Airflow call and folds it per
group: `paused` is `true` only when **all** members are paused, and `mixed` is `true` when members
disagree (some paused, some not). The per-DAG `dags[]` detail is always returned. `PATCH
/admin/dags/{group}` sets `is_paused` on every member DAG of the group to the request's `paused`
value and returns the recomputed group status. An unknown `group` returns `404 DAG_GROUP_NOT_FOUND`;
when Airflow is unreachable both routes return `503 AIRFLOW_UNAVAILABLE`. Scheduled DAGs ship paused at
creation, so operators unpause the groups they want active here. No `/internal` mirror exists.

`/admin/conf` reads and updates the singleton runtime configuration — the behavioral tunables
that shape LLM inference and generation (`llm_provider`, `llm_model`, the ontogen/metagen debate,
RAG, and iteration knobs, `metagen_confidence_threshold`, `validation_score_n_intervals`) plus the
auth-mirror knob `auth_datahub_corp_group` (string, default `dataspoke-users`) that names the
DataHub corpGroup used as the DataSpoke-user provenance marker. The surface also carries four
boolean dependency toggles — `stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`,
`stub_notification_service` — that force the named external dependency into stub or real mode
at runtime (no restart). It is seeded with factory defaults and persisted in the `runtime_config`
table (see [`spec/feature/BACKEND_SCHEMA.md`](feature/BACKEND_SCHEMA.md); defaults live in impl,
not here). `PATCH` is partial; numeric fields are bound-validated (out-of-range → `422`).

The conf surface also carries `llm_api_key` for **online** key rotation, but it is stored in the
`dataspoke-llm-secret` Kubernetes Secret (not the DB): `PATCH` with `llm_api_key` writes the
Secret and an empty string clears it; `GET` returns it masked (`""` unset / `"********"` set) and
**never** returns the plaintext. See [`spec/feature/BACKEND_LLM.md` §LLM API key](feature/BACKEND_LLM.md).

`/admin/users/{id}` accepts display-name changes only; email is immutable
because the DataHub corpuser URN is immutable. `/admin/users/{id}/role`
writes `users.role` first, then propagates to DataHub via the
`batchAssignRole` GraphQL mutation — DataSpoke is the SSOT for role, and
the DataHub-side assignment is a one-way mirror reconciled nightly by the
`auth-role-sync-daily` DAG (see [AUTH §Role Drift Reconciliation](feature/AUTH.md#role-drift-reconciliation)).
`DELETE /admin/users/{id}` hard-deletes the DataSpoke row and the DataHub
corpuser (via `hard_delete_entity`), which also removes the corpuser's
group memberships, role assignments, and ownership references in the
DataHub graph; `ON DELETE CASCADE` on `api_tokens.user_id` removes the
user's tokens at the DB level.

`GET /admin/users/{id}/api-tokens` and `DELETE /admin/users/{id}/api-tokens/{token_id}`
exist for incident response — admins can see and revoke any user's tokens,
but they cannot mint tokens on behalf of other users (only the owner can
mint). See [feature/AUTH.md §API Tokens](feature/AUTH.md#api-tokens).

`/admin/peripherals/smtp` follows the same pattern as the existing
`/admin/peripherals/datahub` and `/admin/peripherals/langfuse` routes
(explicit per-peripheral path, typed response, `is_configured` flag).
Non-secret fields (`host`, `port`, `username`, `from_address`, `use_tls`)
are persisted in the `peripheral_config` DB table; the `password` field
is routed to a dedicated K8s Secret `dataspoke-smtp-secret` (data key
`password`) on `PATCH`, never to the DB — same pattern as
`dataspoke-datahub-secret.token` and `dataspoke-langfuse-secret.secret_key`.
`PATCH` is partial; `password=""` clears the secret. Missing SMTP config
fails `POST /auth/password/reset/request` with
`503 PERIPHERAL_NOT_CONFIGURED` (`detail.peripheral = "smtp"`); all other
auth flows remain functional.

The DataHub and Langfuse peripherals carry non-secret connection settings
alongside their masked credential. For DataHub, `service_corpuser_urn` names
the corpuser actor DataSpoke stamps on the assertion `lastUpdated` and ingestion run-event audit stamps
and `default_env` is the fabric/env (`PROD`/`DEV`/`QA`/`TEST`, …) applied when
an ingestion recipe omits `env`. For Langfuse, `project_id` and
`environment_tag` are surfaced to LLM tracing — `environment_tag` becomes the
Langfuse trace `environment`. These fields are non-secret: stored in the
`peripheral_config.settings` JSONB and returned plain (never masked). Unset
rows read back factory defaults (`service_corpuser_urn` →
`urn:li:corpuser:dataspoke`, `default_env` → `DEV`; `project_id` /
`environment_tag` → `""`). Behavioral wiring is detailed in
[`spec/feature/BACKEND.md`](feature/BACKEND.md) and
[`spec/feature/BACKEND_LLM.md`](feature/BACKEND_LLM.md).

Across all peripherals, `is_configured` is a logical AND: it is `true` only when both the
config row is present **and** the associated K8s Secret is set. A DB row without its secret, or
a secret without its row, reads back `false`. On `PATCH`, a request carrying the secret field
(`token` / `secret_key` / `password`) writes that value to the K8s Secret **first**; the DB write
is skipped if the Secret write fails (`503`). A secret field omitted from the body leaves the
Secret unchanged; an empty-string secret clears it. An empty `PATCH` body is a no-op (neither
Secret nor DB is written).

### Internal Admin (`/internal/admin`)

Internal-only routes gated by the `X-Internal-Token` shared-secret header. Used by scripts,
Airflow DAGs, and automation.

| Method | Path | Body | Response | Auth |
|--------|------|------|----------|------|
| `POST` | `/internal/admin/bootstrap` | — | `{created, user_id, email}` | `X-Internal-Token` |
| `POST` | `/internal/admin/dags/verify` | — | `{found, missing, total_expected}` | `X-Internal-Token` |
| `POST` | `/internal/admin/datahub/sync` | `{"dataset_urns": list[str] \| null}` | `{checked, flipped_true, flipped_false, unchanged, not_found}` | `X-Internal-Token` |
| `PATCH` | `/internal/admin/conf` | partial conf fields | updated runtime config | `X-Internal-Token` |
| `PATCH` | `/internal/admin/peripherals/datahub` | partial DataHub fields | updated DataHub config (with `token` masked) | `X-Internal-Token` |
| `PATCH` | `/internal/admin/peripherals/langfuse` | partial Langfuse fields | updated Langfuse config (with `secret_key` masked) | `X-Internal-Token` |
| `PATCH` | `/internal/admin/peripherals/smtp` | partial SMTP fields | updated SMTP config (with `password` masked) | `X-Internal-Token` |

`POST /internal/admin/bootstrap` seeds the built-in `dataspoke@dataspoke.local / dataspoke` admin user when no
Admin row exists in the `users` table. The endpoint is idempotent: if any Admin already exists
it returns `{created: false}` without touching anything. The `helm-charts/bin/post-install/seed-admin-user.sh`
post-install script invokes it during both dev and prod installs. Operators must rotate the
default password via `PATCH /auth/me` before going to production.

`PATCH /internal/admin/conf` is the unattended mirror of `PATCH /admin/conf`; the dev-profile
install (`./helm-charts/bin/install.sh --profile dev`) uses it to seed `llm_provider`/`llm_model`
from `DATASPOKE_DEV_LLM_*` after the chart is installed.
`PATCH /internal/admin/peripherals/smtp` is the unattended mirror of
`PATCH /admin/peripherals/smtp`, parallel to the existing
`/internal/admin/peripherals/{datahub,langfuse}` mirrors used by
`bin/post-install/seed-peripheral-config.sh`.

### Internal Activities (`/internal/activities`)

Cluster-internal activity endpoints invoked by Airflow DAGs (HttpOperator → in-cluster API
DNS) to drive long-running domain workflows (ingestion, ontogen, metagen, metric runs).
Gated by the same `X-Internal-Token` header. The per-domain route shapes are not
catalogued in this spec — they are an implementation detail of the workflow boundary and live
with the relevant feature service in [BACKEND.md](feature/BACKEND.md). External clients must
not call these routes; they are not exposed through ingress.

### System

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness check (no auth required) |
| `GET` | `/ready` | Readiness check — per-dependency reachability for DataHub, PostgreSQL, Redis |

`/ready` always returns `200` with `{status, checks}`, where `checks` carries a boolean per
dependency (`datahub`, `postgres`, `redis`). `status` is `"ok"` only when every check is `true`,
otherwise `"degraded"`. An unconfigured or unreachable peripheral (e.g. DataHub) yields its
`checks` flag `false` and a `"degraded"` status — never a `503`. The endpoint reports state for
probes to interpret rather than gating on dependency presence.

> **Prefix exception**: System routes are mounted at the root (`/health`, `/ready`) — not
> under `/api/v1/…` — so probes from kubelet, ingress, and platform tooling stay independent
> of the API version. This is the only documented exception to the `/api/v1` prefix
> convention.

---

## Request & Response Conventions

These rules apply `API_DESIGN_PRINCIPLE_en.md` concretely to DataSpoke.

> **Style consistency**: All DataSpoke API endpoints must follow the conventions in this
> section uniformly — snake_case field names, ISO 8601 UTC timestamps, `offset`/`limit`
> for pagination, `from`/`to` for time-range filters, and `sort={field}_{asc|desc}` for
> ordering. Any deviation from these conventions requires explicit justification in the
> relevant feature spec.

### Field Naming

All request body and response fields use **snake_case**.

### Standard Response Envelope

All collection responses include a content key named after the resource + pagination
metadata:

```json
{
  "datasets": [
    { "urn": "urn:li:dataset:…", "name": "orders", "quality_score": 82 },
    { "urn": "urn:li:dataset:…", "name": "customers", "quality_score": 91 }
  ],
  "offset": 0,
  "limit": 20,
  "total_count": 143,
  "resp_time": "2026-02-27T10:00:00.000Z"
}
```

The `datasets`-keyed example above is illustrative only — there is no live cross-dataset list
endpoint. The `DatasetListResponse` schema in `src/api/schemas/dataset.py` is **dead** (bound to
no route) and is flagged for deletion; do not treat it as a contract.

Single-resource responses return the object directly with `resp_time` at the top level:

```json
{
  "urn": "urn:li:dataset:…",
  "name": "orders",
  "quality_score": 82,
  "resp_time": "2026-02-27T10:00:00.000Z"
}
```

### Query Parameters

| Parameter | Type | Purpose |
|-----------|------|---------|
| `offset` | integer | Pagination start (default `0`) |
| `limit` | integer | Page size (default `20`, max `1000`) |
| `sort` | string | Field name + direction suffix `_asc` or `_desc`, e.g. `quality_score_desc`, `occurred_at_asc` |
| `from` | string (ISO 8601) | Start of time-range filter, inclusive; used on `result` and `event` endpoints |
| `to` | string (ISO 8601) | End of time-range filter, inclusive; used on `result` and `event` endpoints |
| `event_major_type` | string (repeatable) | Filter the unified per-dataset timeline (`/spoke/common/data/{urn}/event`) by event-type major prefix — `INGESTION`, `VALIDATION`, or `METAGEN`. Repeat to OR multiple majors; omitted = all majors |
| `q` | string | Natural language query (search endpoints only) |

### Meta-Classifier Conventions

`attr`, `method`, and `event` sub-resources follow the `API_DESIGN_PRINCIPLE_en.md`
definitions:

- `attr` — Read or update a subset of resource attributes. Two flavours:
  - **Configuration / state attributes** (`attr/<feat>/conf`, `attr/conf`): use `GET` to
    read, `PUT` to replace, `PATCH` to update partial fields, `DELETE` to remove.
  - **Result attributes** (`attr/<feat>/result`, `attr/result`): periodic measurement
    records — use `GET` to read (supports `?from=…&to=…`, `?latest=true`, and
    feature-specific filters). Results are immutable in baseline; feature-specific
    state transitions on individual proposals (e.g. UC4 metagen candidate review)
    live on their own `attr/<feat>/<thing>/{id}/method/<action>` sub-paths rather
    than on `result`.
- `method` — Business actions that go beyond CRUD. Action vocabulary used in this spec:
  `run` (trigger a pipeline), `review` (approve/reject a proposal via `verdict` body
  field). Always `POST`. Use the `?dry_run=true` query parameter for no-write mode
  instead of separate dry-run paths.
- `event` — Immutable history log of occurrences on a resource. Always `GET`; supports
  `offset`/`limit` pagination and `sort=occurred_at_desc` (default order, newest first).
  Supports `from`/`to` for time-range filtering. Sub-paths may be defined in feature specs
  to narrow by outcome (e.g. `.../event/failure`, `.../event/success`), but the parent `.../event`
  path must remain and return all event types. All events returned at `.../event` and any
  of its sub-paths must share a **uniform top-level JSON structure** — the same field
  names and types (e.g. `event_type`, `occurred_at`, `status`, `detail`) — so that
  clients can process them generically even when event types differ.

### Date/Time

All timestamps use ISO 8601 with UTC: `2026-02-27T10:00:00.000Z`.

---

## Middleware Stack

Requests pass through, in order: (1) **CORS** — allow configured origins, reject others with
403; (2) **request logging** — method, path, trace ID, client IP before the handler;
(3) **rate limiting** — SlowAPI fixed-window per user (Redis with in-memory fallback,
default 120 req/min). On 429 the response body matches the standard error envelope
(`error_code: "RATE_LIMIT_EXCEEDED"`, `message`, `trace_id`, `resp_time`) and headers
include `Retry-After` plus `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`;
(4) **JWT validation** — verify signature/expiry and extract claims;
(5) **role enforcement** — read `users.role`, apply the method × role gate on `/spoke/*` and the Admin-only gate on `/admin/*`;
(6) **route handler** — FastAPI DI + business logic;
(7) **response logging** — status, latency, trace ID.

> Rate limiting runs as Starlette middleware before any route handler so unauthenticated
> clients are rate-limited too. The per-user key is the JWT `sub` claim when present,
> falling back to client IP. Auth/role checks (layers 4–5) are route-level dependencies
> rather than blanket middleware, so unauthenticated routes (`/health`, `/auth/*`)
> coexist without exclusion lists.

### Trace ID

Every request is assigned a `X-Trace-Id` (UUID v4) at layer 2. If the client provides
`X-Trace-Id` in the request headers, that value is reused. The trace ID is included in
all log lines and in every response header.

---

## Error Catalogue

All errors follow the standard envelope:

```json
{
  "error_code": "DATASET_NOT_FOUND",
  "message": "No dataset found for URN 'urn:li:dataset:unknown'.",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "resp_time": "2026-02-27T10:00:00.000Z"
}
```

The `resp_time` (ISO 8601 UTC, millisecond precision) is included on every error
response, matching the success envelope.

A small set of errors carry an additional `detail` object with structured,
machine-readable context about the failure. Currently emitted by:

- `UNKNOWN_VARIABLE` → `detail.unknown: string[]` lists offending variable keys.
- `INVALID_SCORE` → `detail.score` echoes the rejected value (JSON number when finite, otherwise a string like `"nan"` since JSON has no NaN/Inf).
- `INVALID_PARAMETER` → `detail.errors` carries FastAPI's `.errors()` field-error list (`loc`/`msg`/`type`/`input` per failed field; `input` echoes the rejected value).

Clients should treat `detail` as optional; absent for errors that don't need it.

### HTTP Status Codes

| Status | When used |
|--------|-----------|
| `200 OK` | Successful read, action, or `PUT` that replaces an existing resource |
| `201 Created` | Resource successfully created (`POST`, or `PUT` targeting a new resource) |
| `204 No Content` | Successful deletion |
| `400 Bad Request` | Malformed request, missing required fields, invalid parameter values |
| `401 Unauthorized` | Missing or expired access token |
| `403 Forbidden` | Valid token but insufficient group claim |
| `404 Not Found` | Resource does not exist |
| `409 Conflict` | Duplicate resource or concurrent run attempt |
| `422 Unprocessable Entity` | Pydantic validation failure (field type mismatch, constraint violation), or a request that is well-formed but cannot be processed because a referenced precondition is not met (e.g. dataset not yet present in DataHub) |
| `413 Content Too Large` | Request body exceeds the route's size cap (e.g. `text/markdown` body on ontogen seed / run endpoints) |
| `429 Too Many Requests` | Rate limit exceeded. Body uses the standard error envelope with `error_code: "RATE_LIMIT_EXCEEDED"`; response carries `Retry-After` and `X-RateLimit-*` headers (limit, remaining, reset) |
| `500 Internal Server Error` | Fallback for an unhandled `DataSpokeError` with no specific status mapping |
| `502 Bad Gateway` | DataHub GMS unreachable or returned an unexpected error |
| `503 Service Unavailable` | PostgreSQL, Redis, or other storage-tier dependency unreachable; a required peripheral (DataHub/SMTP) not configured; or internal auth secret not configured |

### Application Error Codes

| `error_code` | HTTP | Description |
|-------------|------|-------------|
| `INVALID_PARAMETER` | 422 | Query param or body field fails schema-layer validation (e.g., `PUT/PATCH /spoke/common/data/{urn}/attr/validation/conf` body where `description` carries ASCII control characters other than `\t` (0x09) and `\n` (0x0a) — see [VALIDATION.md §Rule Configuration](feature/VALIDATION.md#rule-configuration)) |
| `INVALID_ROLE` | 422 | A supplied role value is not one of `Reader`/`Editor`/`Admin` (e.g. `POST /auth/register`, `POST /admin/users`, or `PATCH /admin/users/{id}/role` with an unrecognized role) |
| `UNAUTHORIZED` | 401 | Token missing, expired, or malformed |
| `FORBIDDEN` | 403 | Valid token; caller's role does not satisfy route requirement |
| `DATASET_NOT_FOUND` | 404 | Dataset URN does not exist in DataHub (read paths, e.g. `GET /spoke/common/data/{urn}`) |
| `DATASET_NOT_IN_DATAHUB` | 422 | The targeted dataset URN is not yet tracked by DataHub, so a feature with a "dataset must exist in SSOT first" precondition cannot proceed (e.g. `PUT /spoke/common/data/{urn}/attr/validation/conf`) |
| `NODE_NOT_FOUND` | 404 | Ontology node ID not found |
| `EDGE_NOT_FOUND` | 404 | Ontology edge ID not found |
| `TRIPLE_NOT_FOUND` | 404 | Ontology triple ID not found |
| `CONFIG_NOT_FOUND` | 404 | Validation or other per-dataset configuration not found (never created, or deleted) |
| `INGESTION_SOURCE_NOT_FOUND` | 404 | Ingestion source id does not exist (`/spoke/ingestion/sources/{id}`) |
| `SECRET_REF_MALFORMED` | 422 | A `${name__key}` reference in a recipe has no `__` separator or an empty name/key segment |
| `SECRET_REF_NOT_FOUND` | 422 | A recipe's `${name__key}` references a `dataspoke-source-cred-<name>` Secret or `key` that does not exist at source save (also surfaces as a run-time `status="error"` if deleted later) |
| `METRIC_NOT_FOUND` | 404 | Metric ID does not exist |
| `DAG_GROUP_NOT_FOUND` | 404 | `PATCH /admin/dags/{group}` references a group that is not one of `datahub_sync`/`auth_role_sync`/`ingestion_active`/`ontogen`/`metagen`/`metrics` |
| `DUPLICATE_CONFIG` | 409 | Config with same name already exists |
| `INGESTION_SOURCE_READONLY` | 409 | Create/update/delete attempted on a `DATAHUB_MANAGED` source; DataHub is SSOT, so it is synced down and read-only in DataSpoke |
| `INGESTION_RUN_NOT_APPLICABLE` | 409 | `…/sources/{id}/method/run` called on a non-`ACTIVE_CUSTOM_MANAGED` source; only DataSpoke-managed sources have a DataSpoke-side run pipeline |
| `INGESTION_RUNNING` | 409 | An ingestion run is already in progress for this source |
| `UNKNOWN_VARIABLE` | 422 | `POST .../attr/validation/result` body carries `variables` keys not declared in the dataset's `attr/validation/conf.variables` |
| `INVALID_SCORE` | 422 | `POST .../attr/validation/result` body has `score` outside `[0.0, 1.0]` |
| `METAGEN_RUNNING` | 409 | A run of this metagen conf is already in progress (`metagen:running:{conf_id}` lock held) |
| `METAGEN_DISABLED` | 409 | This metagen conf has `is_enabled=false`; non-dry-run rejected |
| `METAGEN_CONF_EXISTS` | 409 | `POST /spoke/metagen/conf` body carries a `name` that already exists |
| `METAGEN_CONF_NOT_FOUND` | 404 | Metagen `conf_id` does not exist (`/spoke/metagen/conf/{conf_id}`) |
| `METAGEN_DATASET_NOT_IN_BOUNDARY` | 422 | Candidate review attempted on an item whose dataset has no `is_enabled=true` per-dataset metagen boundary |
| `METRIC_RUNNING` | 409 | A metric measurement run is already in progress for this metric |
| `METRIC_DISABLED` | 409 | Metric definition has `is_enabled=false`; non-dry-run rejected |
| `METRIC_EXISTS` | 409 | `POST /spoke/governance/metric` body carries a `metric_id` that already exists |
| `ONTOGEN_RUNNING` | 409 | An ontology inference run is already in progress |
| `ONTOGEN_DISABLED` | 409 | Ontogen conf has `is_enabled=false`; non-dry-run rejected |
| `ONTOGEN_TRIPLE_DEPENDENCY_PENDING` | 422 | Triple review attempted while one or more of its subject node, edge, or object node is not yet approved |
| `PAYLOAD_TOO_LARGE` | 413 | `text/markdown` request body exceeds the route's size cap. Ontogen seed (`POST`/`PATCH /spoke/ontogen/attr/seed[/{seed_id}]`) and run (`POST /spoke/ontogen/method/run`) bodies are capped at 128 KiB |
| `INVALID_DATASET_URN` | 422 | A `dataset_filter.dataset_urns` entry is not a well-formed `urn:li:dataset:(…)` URN. Validated at POST/PUT/PATCH for `ontogen/attr/conf`, `metagen/conf[/{conf_id}]`, and `metric/{id}/attr/conf` |
| `NOT_IMPLEMENTED` | 501 | The requested mode or capability is reserved for future work. Returned by `POST /spoke/governance/metric` and `PUT /spoke/governance/metric/{id}/attr/conf` when `mode: "passive"` |
| `EMAIL_ALREADY_REGISTERED` | 409 | `POST /auth/register` body carries an email already mapped to an existing user |
| `INVALID_RESET_TOKEN` | 400 | `POST /auth/password/reset/confirm` token does not match any row, is expired, or has already been used |
| `OAUTH_STATE_MISMATCH` | 400 | `GET /auth/google/callback` state cookie missing or does not match the value embedded in the OAuth state JWT |
| `OAUTH_EMAIL_NOT_VERIFIED` | 400 | `GET /auth/google/callback` received an ID token where `email_verified=false`; unverified Google emails cannot resolve to a DataSpoke account |
| `OAUTH_NOT_CONFIGURED` | 503 | `GET /auth/google/{login,callback}` invoked while Google OAuth credentials or the OAuth-state HMAC secret are not configured — operator must set `DATASPOKE_GOOGLE_OAUTH_CLIENT_{ID,SECRET}` and `DATASPOKE_OAUTH_STATE_SECRET` |
| `GOOGLE_ACCOUNT_LINKED_ELSEWHERE` | 409 | `GET /auth/google/callback` resolved a Google `sub` that is already linked to a different DataSpoke user (one Google account per user) |
| `INVALID_REFRESH_TOKEN` | 401 | `POST /auth/token/refresh` received a structurally-valid JWT whose `type` claim is not `"refresh"` (e.g. an access token presented at the refresh endpoint) |
| `READ_ONLY_ROLE` | 403 | Caller has `Reader` role (or an API token with effective `Reader` privilege); route requires `Editor` or `Admin` (any write method on `/spoke/*`, or the Editor+ read route `GET /spoke/ingestion/secrets`) |
| `INVALID_API_TOKEN` | 401 | `Authorization: Bearer dsk_...` token does not match any `api_tokens` row, or the format is malformed |
| `TOKEN_REVOKED` | 401 | API token row exists but `revoked_at` is set |
| `TOKEN_EXPIRED` | 401 | API token row exists but `expires_at` is in the past |
| `TOKEN_NOT_FOUND` | 404 | `DELETE /auth/api-tokens/{id}` or `DELETE /admin/users/{id}/api-tokens/{token_id}` references a non-existent token |
| `TOKEN_LIMIT_EXCEEDED` | 409 | `POST /auth/api-tokens` attempted while user already has 10 active (non-revoked) tokens |
| `DATAHUB_SYNC_FAILED` | 503 | DataHub-side user mirror operation failed (create / role change / role propagation). For user creation this triggers a compensating hard-delete of the partial DataSpoke `users` row; for role propagation the DataSpoke write is preserved and the nightly DAG reconciles |
| `PERIPHERAL_NOT_CONFIGURED` | 503 | A required peripheral is not configured. `detail.peripheral` identifies which one (`"smtp"` for `/auth/password/reset/request`; `"datahub"` for any DataHub-requiring endpoint when DataHub is unconfigured). Distinct from `DATAHUB_UNAVAILABLE` (502), which is the configured-but-unreachable case. The `/ready` health endpoint is the exception that reports an unconfigured peripheral as `degraded` rather than returning this code |
| `DATAHUB_UNAVAILABLE` | 502 | DataHub GMS is configured but did not respond or returned an error |
| `AIRFLOW_UNAVAILABLE` | 503 | The in-cluster Airflow REST API did not respond or returned an error while reading or setting DAG paused state (`GET`/`PATCH /admin/dags`) |
| `STORAGE_UNAVAILABLE` | 503 | PostgreSQL or Redis connection failed (including auth refresh or revoke fail-closed when the revocation store is unreachable) |
| `INTERNAL_AUTH_NOT_CONFIGURED` | 503 | `X-Internal-Token` shared-secret header is required for `/internal/*` routes but the server-side secret is unset |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests; back off and retry |
| `INTERNAL_ERROR` | 500 | Unhandled `DataSpokeError` with no specific status mapping (fallback) |
