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
all DataSpoke clients — the portal UI and external AI agents. It exposes a three-tier URI
structure: baseline features defined in MANIFESTO §2.1 live under `/spoke/common/` and
`/spoke/dg/`; the `/spoke/de/` and `/spoke/da/` tiers are extensibility surfaces for
organization-specific routes.

```
/api/v1/spoke/common/…     — Baseline features: ingestion, validation, ontology generation, metadata generation
/api/v1/spoke/de/…         — Reserved for Data Engineering extensions (no baseline routes)
/api/v1/spoke/da/…         — Reserved for Data Analysis extensions (no baseline routes)
/api/v1/spoke/dg/…         — Governance (metric, overview)
/api/v1/hub/…              — DataHub pass-through (optional ingress for clients)
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

### Token Strategy

DataSpoke uses **JWT (JSON Web Tokens)** for stateless authentication.

| Token type | Lifetime | Storage |
|------------|----------|---------|
| Access token | 15 minutes | Memory / `Authorization` header |
| Refresh token | 7 days | HttpOnly cookie |

Token issuance and refresh are handled at:
- `POST /auth/token` — issue access + refresh tokens (credential exchange)
- `POST /auth/token/refresh` — issue new access token from refresh token
- `POST /auth/token/revoke` — revoke refresh token (logout)

### JWT Claims

Access-token payload: `sub` (user uuid), `email`, `groups` (array of user-group identifiers
— `de`, `da`, `dg`; a user may belong to multiple), `exp`, `iat`. The middleware enforces
that a request targeting `/spoke/de/…` must have `"de"` in the `groups` claim.

### Group-to-Route Access Control

| URI tier | Required group claim | Accessible to |
|----------|---------------------|---------------|
| `/spoke/common/…` | any valid group | DE, DA, DG |
| `/spoke/de/…` | `"de"` | DE (and admins) — reserved; no routes currently defined |
| `/spoke/da/…` | `"da"` | DA (and admins) — reserved; no routes currently defined |
| `/spoke/dg/…` | `"dg"` | DG (and admins) |
| `/hub/…` | any valid group | DE, DA, DG |
| `/auth/…` | none (public) | unauthenticated clients |
| `/admin/…` | `"admin"` | admins only |

### Admin Role

Users with `"admin"` in `groups` bypass group-tier restrictions and can call any route.
Admin routes (user management, system configuration) live under `/api/v1/admin/…` and
require the `"admin"` claim exclusively.

### Known Limitations (Current Stub)

The current authentication implementation uses a stub identity store:

- **Single admin account**: Only one user
  (configured via `DATASPOKE_ADMIN_EMAIL` / `DATASPOKE_ADMIN_PASSWORD`) can authenticate.
  All other credentials are rejected.
- **Redis-backed token revocation**: Revoked refresh tokens are stored in Redis under
  `revoked_refresh:{sha256[:16]}` with TTL equal to the token's remaining lifetime.
  Refresh and revoke are fail-closed on the Redis path — if the store is unreachable,
  `POST /auth/token/refresh` returns `503 STORAGE_UNAVAILABLE` (Redis is the storage subsystem).
- **No group resolution**: The admin account receives all groups (`admin`, `de`, `da`, `dg`);
  non-admin users receive an empty group list.
- **HTTP-only cookies**: The refresh token cookie uses `secure=False`.
  Production deployments must set `secure=True`.

All stub code is marked with `TBD(user-accounts)` comments. See
[BACKEND §User Account Management](feature/BACKEND.md#user-account-management-tbd)
for the planned migration path.

### Auth Flow

Login: client `POST /auth/token` with `{email, password}` → API verifies credentials against
the identity store, receives the user record + groups, and returns `{access_token}` in the
body plus the refresh token as an HttpOnly cookie. Subsequent protected calls
(e.g. `GET /spoke/common/data/{urn}/attr/ingestion/conf`) carry
`Authorization: Bearer <access_token>`; the API validates the JWT signature/expiry and
enforces the `groups` claim against the URI tier before dispatching.

---

## Route Catalogue

All routes are prefixed with `/api/v1`.

> **Routing principle**: Baseline features live under `/spoke/common/` (shared
> dataset-centric operations: ingestion, validation, ontology generation, metadata generation) and
> `/spoke/dg/` (governance metrics and overviews). The `/spoke/de/` and
> `/spoke/da/` tiers exist as extensibility surfaces for organization-specific
> routes and contain no baseline endpoints. For dataset-centric operations, the
> `/spoke/common/data/{dataset_urn}/…` structure is the **canonical surface** for
> per-dataset state (`attr/<feat>/`), actions (`method/<feat>/`), and events
> (`event/<feat>` or `event`). The dedicated routers
> `/spoke/common/{ingestion,validation,metagen}` expose only cross-dataset list views
> that aggregate the per-dataset `attr/<feat>/*` data — they do not expose
> per-dataset detail (use the canonical `data/{dataset_urn}` surface for that).
> Any team that owns a dataset can access per-dataset features regardless of
> group membership.

### Auth

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/auth/token` | Issue access + refresh tokens |
| `POST` | `/auth/token/refresh` | Refresh access token |
| `POST` | `/auth/token/revoke` | Revoke refresh token (logout) |

### Common (`/spoke/common`)

Baseline features consumed by all user groups.

#### Ontology Generation

The ontology is a global artifact, so its conf, seeds, manual run trigger, and
inference-run event log are singletons rooted at `/spoke/common/ontogen` rather than
under any dataset URN. Inference output follows a **subject / predicate / object
triple model** with three independently reviewable result types — `node` (subject /
object), `edge` (predicate), and `triple` (`(subject_node, edge, object_node)` fact).
A triple may only be composed of pre-approved nodes and edges; review proceeds
nodes → edges → triples.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/ontogen/attr/conf` | Get singleton operational conf (`is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) | Ontology Generation | UC3 |
| `PUT` | `/spoke/common/ontogen/attr/conf` | Create or replace operational conf | Ontology Generation | UC3 |
| `PATCH` | `/spoke/common/ontogen/attr/conf` | Partially update operational conf | Ontology Generation | UC3 |
| `DELETE` | `/spoke/common/ontogen/attr/conf` | Remove operational conf (effectively disables) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/attr/seed` | List seeds — returns `[{seed_id, updated_at, preview}]` (preview is a short Markdown snippet); the seed body is fetched per-seed below | Ontology Generation | UC3 |
| `POST` | `/spoke/common/ontogen/attr/seed` | Create an inference seed — body is a raw Markdown document (`Content-Type: text/markdown`); server assigns `seed_id` | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/attr/seed/{seed_id}` | Get seed Markdown document (`Content-Type: text/markdown`) | Ontology Generation | UC3 |
| `PATCH` | `/spoke/common/ontogen/attr/seed/{seed_id}` | Replace seed Markdown body (`Content-Type: text/markdown`) | Ontology Generation | UC3 |
| `DELETE` | `/spoke/common/ontogen/attr/seed/{seed_id}` | Retire a seed | Ontology Generation | UC3 |
| `POST` | `/spoke/common/ontogen/method/run` | Trigger a manual re-inference. Optional `Content-Type: text/markdown` body acts as a **one-shot prompt** for this run, on top of the persistent seeds (not stored). With no body — including periodic Airflow invocations — falls back to `attr/conf.default_run_prompt`. `?dry_run=true` evaluates without persisting. Concurrent runs return `409 ONTOGEN_RUNNING`. Rejected with `409 ONTOGEN_DISABLED` when the conf is disabled and `dry_run` is not true | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/event` | Global inference-run event history (e.g. `ONTOGEN.RUN_COMPLETE`, `ONTOGEN.RUN_FAILED`) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/node` | List nodes (subjects / objects) with confidence and status | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/node/{node_id}` | Get node detail (incl. member datasets) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/node/{node_id}/attr` | Get node attributes (confidence, source evidence) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/node/{node_id}/event` | Node-level change history | Ontology Generation | UC3 |
| `POST` | `/spoke/common/ontogen/result/node/{node_id}/method/review` | Review a pending node — body: `{"verdict": "approve"\|"reject", "reason": "…"}` | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/edge` | List edges (predicates) with confidence and status | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/edge/{edge_id}` | Get edge detail | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/edge/{edge_id}/attr` | Get edge attributes (confidence, source evidence) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/edge/{edge_id}/event` | Edge-level change history | Ontology Generation | UC3 |
| `POST` | `/spoke/common/ontogen/result/edge/{edge_id}/method/review` | Review a pending edge — body: `{"verdict": "approve"\|"reject", "reason": "…"}` | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/triple` | List triples — `(subject_node_id, edge_id, object_node_id)` facts — with confidence and status | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/triple/{triple_id}` | Get triple detail (resolved subject node, edge, object node) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/triple/{triple_id}/attr` | Get triple attributes (confidence, source evidence) | Ontology Generation | UC3 |
| `GET` | `/spoke/common/ontogen/result/triple/{triple_id}/event` | Triple-level change history | Ontology Generation | UC3 |
| `POST` | `/spoke/common/ontogen/result/triple/{triple_id}/method/review` | Review a pending triple — body: `{"verdict": "approve"\|"reject", "reason": "…"}`. Returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` if any of subject node, edge, or object node is not yet approved | Ontology Generation | UC3 |

**Payload caps** (validated at the schema layer; cap violations return `422`):
- `attr/conf.default_run_prompt` ≤ 16,000 chars
- `attr/conf.dataset_filter.dataset_urns` ≤ 1,000 entries
- `attr/seed` Markdown body ≤ 64 KiB
- `method/run` one-shot Markdown body ≤ 64 KiB
- node / edge / triple `method/review.reason` ≤ 2,000 chars

#### Data Resource (`/spoke/common/data/{dataset_urn}`)

The canonical resource for a dataset. All teams (DE, DA, DG) access dataset attributes,
ingestion, validation, and generation through this shared path. The three meta-classifiers
group sub-resources by feature: state and configuration live under `attr/<feature>/`
(`conf`, plus `result` for validation and metagen as periodic timeseries), action triggers
under `method/<feature>/<action>`, and lifecycle events under `event/<feature>` (or
`event` alone for the unified per-dataset timeline). In a data-mesh organization any team
that owns a dataset can register and manage ingestion, validation, and generation — DE
teams provide deep technical specs while DA or other teams may register simpler
configurations.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/data/{dataset_urn}` | Get dataset summary (identity, owner, tags) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}/attr` | Get dataset attributes (schema summary, ownership, tags) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Get ingestion configuration for dataset | Ingestion Control | UC1 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Create or replace ingestion configuration | Ingestion Control | UC1 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Partially update ingestion configuration | Ingestion Control | UC1 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Remove ingestion configuration | Ingestion Control | UC1 |
| `POST` | `/spoke/common/data/{dataset_urn}/method/ingestion/run` | Execute ingestion pipeline directly — `active-custom` configs only (`dry_run` in body for no-write mode); concurrent runs return `409 INGESTION_RUNNING`; rejected with `409 INGESTION_DISABLED` when the conf is disabled and `dry_run` is not true; rejected with `409 INGESTION_NOT_APPLICABLE` for `passive` configs (passive ingestion is run externally) | Ingestion Control | UC1 |
| `GET` | `/spoke/common/data/{dataset_urn}/event/ingestion` | Ingestion event reports (success/failure notices) | Ingestion Control | UC1 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Get validation configuration (`description` + declared `variables`) | Validation | UC2, UC5 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Create or replace validation configuration. PUT for a URN absent from DataHub returns `422 DATASET_NOT_IN_DATAHUB` | Validation | UC2, UC5 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Partially update validation configuration | Validation | UC2, UC5 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Soft-delete the validation slot — emits DataHub `status.removed = true`. A subsequent `PUT` resurrects the same assertion URN | Validation | UC2, UC5 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Append a pipeline-emitted result `{data_time, score, variables}`. Unknown variable keys return `422 UNKNOWN_VARIABLE`; `score` outside `[0,1]` returns `422 INVALID_SCORE` | Validation | UC2, UC5 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Get historical results (timeseries on `data_time`; `?from=…&until=…&limit=…`, default `limit=1000`, server cap `10000`) | Validation | UC2, UC5 |
| `GET` | `/spoke/common/data/{dataset_urn}/event/validation` | Validation event reports (success/failure notices) | Validation | UC2, UC5 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/metagen/conf` | Get metadata generation configuration (target fields, schedule_tier, status) | Metadata Generation | UC4 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/metagen/conf` | Create or replace metadata generation configuration | Metadata Generation | UC4 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/metagen/conf` | Partially update metadata generation configuration | Metadata Generation | UC4 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/metagen/conf` | Remove metadata generation configuration | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/metagen/result` | Get metadata proposals (historical; `?latest=true` for most recent only; `?approved=true` to filter to approved proposals) | Metadata Generation | UC4 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/metagen/result/{result_id}` | Approve (or reject, or partially approve specific fields of) a pending metadata proposal — body: `{"verdict": "approve"\|"reject", "fields": [...] (optional, omit for full approval), "reason": "…"}`. On approval, DataSpoke writes the approved subset to DataHub. | Metadata Generation | UC4 |
| `POST` | `/spoke/common/data/{dataset_urn}/method/metagen/run` | Trigger metadata generation run; concurrent runs return `409 GENERATION_RUNNING`. Rejected with `409 GENERATION_DISABLED` when the conf is disabled and `dry_run` is not true | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/event/metagen` | Metadata generation event reports (success/failure notices) | Metadata Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/event` | Dataset-level event history (all event types including ingestion, validation, and metagen) | Data Resource | — |

#### Redefined DataHub Functions *(TBD)*

Future routes for blended dataset creation and modification. Example candidates:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/spoke/common/data` | Create a dataset — write core metadata to DataHub and initialize DataSpoke-side records in a single call |
| `PATCH` | `/spoke/common/data/{dataset_urn}` | Update dataset metadata — blend DataHub aspect writes with DataSpoke-specific updates |

These routes are **not yet defined**; scope and design will be specified when the feature is
planned. See [DATAHUB_INTEGRATION §Key principles](DATAHUB_INTEGRATION.md#overview).

#### Ingestion (`/spoke/common/ingestion`)

A cross-dataset list view of ingestion attributes. Each row combines dataset identity
with the ingestion attributes stored under `common/data/{dataset_urn}/attr/ingestion/*`
(currently `conf`). Useful for operations dashboards and bulk management.

DataSpoke ingestion implements source-agnostic metadata extraction built on DataHub's
entity-aspect model — connecting to heterogeneous data sources and emitting results as
standard DataHub aspects. Design framework, source abstraction model, and aspect emission
details: see [BACKEND §Ingestion Service](feature/BACKEND.md#ingestion-service-srcbackendingestion)
and [DATAHUB_INTEGRATION §Aspect Reference](DATAHUB_INTEGRATION.md#aspect-reference).

Per-dataset detail, actions, and events live on the canonical `data/{dataset_urn}`
surface: `attr/ingestion/conf` (CRUD), `method/ingestion/run`, `event/ingestion`.

`attr/ingestion/conf` carries a `mode` flag (`active-custom` | `passive`) —
`active-custom` configs are run by DataSpoke's in-house extractor on the configured
`schedule_tier`; `passive` configs are populated by external ingestors (DataHub Managed
Ingestion, custom acryl-datahub-SDK scripts, or any pipeline that emits
`DataProcessInstance` records per run) and have their run history mirrored into
`event/ingestion` by an hourly poll job. Both modes share the same API surface;
`method/ingestion/run` applies to `active-custom` only and returns
`409 INGESTION_NOT_APPLICABLE` for `passive`. See
[BACKEND §Ingestion Service](feature/BACKEND.md#ingestion-service-srcbackendingestion)
and [DATAHUB_INTEGRATION §Custom Ingestor Guide](DATAHUB_INTEGRATION.md#custom-ingestor-guide).

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/ingestion` | List ingestion attributes across datasets — each row aggregates the per-dataset `attr/ingestion/*` (paginated, filterable) | Ingestion Control | UC1 |

#### Validation (`/spoke/common/validation`)

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
| `GET` | `/spoke/common/validation` | List validation attributes across datasets — each row aggregates the per-dataset `attr/validation/*` (conf description + variable count + latest result `data_time` and `score`) (paginated, filterable) | Validation | UC2, UC5 |

#### Metadata Generation (`/spoke/common/metagen`)

A cross-dataset list view of metadata generation attributes. Each row combines dataset
identity with the metadata generation attributes stored under
`common/data/{dataset_urn}/attr/metagen/*` (`conf` and latest `result`). Useful for
monitoring generation status across all datasets and bulk management.

Per-dataset detail, actions, and events live on the canonical `data/{dataset_urn}`
surface: `attr/metagen/{conf,result}` (PATCH on `result/{result_id}` performs review), `method/metagen/run`, `event/metagen`.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/metagen` | List metadata generation attributes across datasets — each row aggregates the per-dataset `attr/metagen/*` (conf and latest result) (paginated, filterable) | Metadata Generation | UC4 |

**Payload caps** (per-dataset PATCH on `attr/metagen/result/{result_id}`; validated at schema; violations return `422`):
- `reason` ≤ 2,000 chars
- `fields` list ≤ 200 entries
- each `fields[*]` entry ≤ 512 chars

### Data Governance (`/spoke/dg`)

#### Metric (`/spoke/dg/metric`)

Governance metrics are named, configurable measurements tracked over time — for example,
the count of poorly documented datasets or the count of stale datasets. Each metric
carries a definition (`attr/conf`) that controls how it is computed and scheduled, and a
timeseries of measurement results (`attr/result`). Metrics represent enterprise-wide or
department-wide signals rather than per-dataset observations.

> **Pure aggregation principle**: A metric does not observe the data estate directly. It
> aggregates results that already exist in DataHub metadata or DataSpoke validation results.

Metrics are read-only consumers of DataHub metadata — they never write aspects or connect
to source databases. Design framework (observatory pattern, governance
dimensions), built-in metric types, and extensibility model: see
[BACKEND §Metrics Service](feature/BACKEND.md#metrics-service-srcbackendmetrics) and
[DATAHUB_INTEGRATION §Aspect Usage by Feature](DATAHUB_INTEGRATION.md#aspect-usage-by-feature).

**`metric_id`**: Kebab-case slug, system-generated or user-supplied (e.g.
`ingestion-freshness`, `validation-score`). Used in route paths and as the DAG-name
suffix `metrics-{metric_id}`.

**`measurement_query.dataset_filter`**: Optional filter object in the metric definition.
Fields: `tags` (list of DataHub tag URNs), `glossary_terms` (list of DataHub glossary term
URNs), and `dataset_urns` (list of explicit `urn:li:dataset:(…)` URNs for pinning to a
known set). When specified, only datasets matching ANY listed tag, glossary term, or
explicit URN are included in the measurement. Filters are OR-ed across all three
dimensions; an empty array on any dimension contributes nothing; `{}` means all datasets.
URN format is validated at PUT/PATCH time (`422 INVALID_DATASET_URN`); `dataset_urns`
entries that don't resolve in DataHub at run time are skipped and reported in the
`METRIC.RUN_COMPLETE` event's `unresolved_urns` field. The same shape and validation
apply to UC3's `ontogen/attr/conf.dataset_filter` (reported via `ONTOGEN.RUN_COMPLETE`).

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/dg/metric` | List all metrics (paginated; filterable by theme, status) | Governance | UC5 |
| `GET` | `/spoke/dg/metric/{metric_id}` | Get metric summary (identity, theme, enabled status) | Governance | UC5 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr` | Get metric attributes overview (theme, schedule_tier, enabled status) | Governance | UC5 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr/conf` | Get full metric definition (title, theme, measurement_query, schedule_tier, enabled status) | Governance | UC5 |
| `PUT` | `/spoke/dg/metric/{metric_id}/attr/conf` | Create or replace metric definition | Governance | UC5 |
| `PATCH` | `/spoke/dg/metric/{metric_id}/attr/conf` | Update metric definition fields | Governance | UC5 |
| `DELETE` | `/spoke/dg/metric/{metric_id}/attr/conf` | Remove metric definition | Governance | UC5 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr/result` | Get measurement results (numeric timeseries; `?from=…&to=…` for time range) | Governance | UC5 |
| `POST` | `/spoke/dg/metric/{metric_id}/method/run` | Trigger a metric measurement run; concurrent runs return `409 METRIC_RUNNING`. Rejected with `409 METRIC_DISABLED` when the metric is disabled and `dry_run` is not true | Governance | UC5 |
| `GET` | `/spoke/dg/metric/{metric_id}/event` | Metric run events (run completions, definition changes) | Governance | UC5 |

#### Overview (`/spoke/dg/overview`)

Governance views of the data estate that cannot be expressed as per-metric timeseries:
ontology-based topology views (consuming the UC3 node / triple graph), medallion layer
coverage maps, and ownership topology. Use these paths only when the `/spoke/dg/metric`
routes are insufficient. All views are read-only aggregations over DataHub aspects,
DataSpoke validation results, and the ontology.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/dg/overview` | Get multi-perspective overview snapshot (ontology graph + medallion coverage + ownership topology) | Governance | UC5 |
| `GET` | `/spoke/dg/overview/attr` | Get visualization config (layout, coloring, filters) | Governance | UC5 |
| `PATCH` | `/spoke/dg/overview/attr` | Update visualization config | Governance | UC5 |

### DataHub Pass-Through (`/hub`)

Optional ingress that forwards requests to DataHub GMS. Useful for clients that want a
single base URL. Authentication is still enforced by DataSpoke; the request is proxied
after JWT validation.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/hub/graphql` | Proxy DataHub GraphQL queries |
| `*` | `/hub/openapi/{path:path}` | Proxy DataHub REST OpenAPI endpoints (all methods) |

### Admin (`/admin`)

Operator and system routes accessible to users with the `"admin"` group claim. Mirrors of
these endpoints are also available under `/internal/admin/…` for unattended automation
(Airflow DAGs, scripts) — the internal mount is gated by the `X-Internal-Token`
shared-secret header instead of a JWT.

| Method | Path | Body | Response | Auth |
|--------|------|------|----------|------|
| `POST` | `/admin/dags/verify` | — | `{found, missing, total_expected}` | JWT (`admin` group) |

Additional admin routes (user management, identity store administration) are reserved for
future feature specs and are not catalogued here.

### Internal Admin (`/internal/admin`)

Internal-only routes gated by the `X-Internal-Token` shared-secret header. Used by scripts,
Airflow DAGs, and automation.

| Method | Path | Body | Response | Auth |
|--------|------|------|----------|------|
| `POST` | `/internal/admin/dags/verify` | — | `{found, missing, total_expected}` | `X-Internal-Token` |
| `POST` | `/internal/admin/datahub/sync` | `{"dataset_urns": list[str] \| null}` | `{checked, flipped_true, flipped_false, unchanged, not_found}` | `X-Internal-Token` |

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
| `GET` | `/ready` | Readiness check (verifies DataHub, PostgreSQL, Redis connectivity) |

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
| `limit` | integer | Page size (default `20`, max `100`) |
| `sort` | string | Field name + direction suffix `_asc` or `_desc`, e.g. `quality_score_desc`, `occurred_at_asc` |
| `from` | string (ISO 8601) | Start of time-range filter, inclusive; used on `result` and `event` endpoints |
| `to` | string (ISO 8601) | End of time-range filter, inclusive; used on `result` and `event` endpoints |
| `q` | string | Natural language query (search endpoints only) |

### Meta-Classifier Conventions

`attr`, `method`, and `event` sub-resources follow the `API_DESIGN_PRINCIPLE_en.md`
definitions:

- `attr` — Read or update a subset of resource attributes. Two flavours:
  - **Configuration / state attributes** (`attr/<feat>/conf`, `attr/conf`): use `GET` to
    read, `PUT` to replace, `PATCH` to update partial fields, `DELETE` to remove.
  - **Result attributes** (`attr/<feat>/result`, `attr/result`): periodic measurement or
    proposal records — use `GET` to read (supports `?from=…&to=…`, `?latest=true`, and
    feature-specific filters such as `?approved=true`). `PATCH` on an individual result
    row (`attr/<feat>/result/{result_id}`) is permitted for state transitions on that row
    (e.g. review verdict on a generation proposal); body shape is feature-specific.
- `method` — Business actions that go beyond CRUD. Action vocabulary used in this spec:
  `run` (trigger a pipeline), `review` (approve/reject a proposal via `verdict` body
  field). Always `POST`. Use `dry_run` in the request body for no-write mode instead
  of separate dry-run paths.
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
(5) **group enforcement** — check the `groups` claim against the URI tier;
(6) **route handler** — FastAPI DI + business logic;
(7) **response logging** — status, latency, trace ID.

> Rate limiting runs as Starlette middleware before any route handler so unauthenticated
> clients are rate-limited too. The per-user key is the JWT `sub` claim when present,
> falling back to client IP. Auth/group checks (layers 4–5) are route-level dependencies
> (`Depends(require_common)`, `Depends(require_dg)`, etc.) rather than blanket middleware,
> so unauthenticated routes (`/health`, `/auth/*`) coexist without exclusion lists and
> each router controls its required group membership.

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
| `429 Too Many Requests` | Rate limit exceeded. Body uses the standard error envelope with `error_code: "RATE_LIMIT_EXCEEDED"`; response carries `Retry-After` and `X-RateLimit-*` headers (limit, remaining, reset) |
| `500 Internal Server Error` | Fallback for an unhandled `DataSpokeError` with no specific status mapping |
| `502 Bad Gateway` | DataHub GMS unreachable or returned an unexpected error |
| `503 Service Unavailable` | PostgreSQL, Redis, or other storage-tier dependency unreachable; or internal auth secret not configured |

### Application Error Codes

| `error_code` | HTTP | Description |
|-------------|------|-------------|
| `INVALID_PARAMETER` | 400 | Query param or body field fails validation |
| `MISSING_REQUIRED_FIELD` | 400 | Required body field not provided |
| `UNAUTHORIZED` | 401 | Token missing, expired, or malformed |
| `FORBIDDEN` | 403 | Valid token; groups claim does not satisfy route requirement |
| `DATASET_NOT_FOUND` | 404 | Dataset URN does not exist in DataHub (read paths, e.g. `GET /spoke/common/data/{urn}`) |
| `DATASET_NOT_IN_DATAHUB` | 422 | The targeted dataset URN is not yet tracked by DataHub, so a feature with a "dataset must exist in SSOT first" precondition cannot proceed (e.g. `PUT /spoke/common/data/{urn}/attr/validation/conf`) |
| `NODE_NOT_FOUND` | 404 | Ontology node ID not found |
| `EDGE_NOT_FOUND` | 404 | Ontology edge ID not found |
| `TRIPLE_NOT_FOUND` | 404 | Ontology triple ID not found |
| `CONFIG_NOT_FOUND` | 404 | Ingestion or validation configuration not found |
| `METRIC_NOT_FOUND` | 404 | Metric ID does not exist |
| `DUPLICATE_CONFIG` | 409 | Config with same name already exists |
| `INGESTION_DISABLED` | 409 | Ingestion conf has `is_enabled=false`; non-dry-run rejected |
| `INGESTION_NOT_APPLICABLE` | 409 | `method/ingestion/run` called against a `passive`-mode conf; passive ingestion is run externally and has no DataSpoke-side run pipeline |
| `INGESTION_RUNNING` | 409 | An ingestion run is already in progress for this config |
| `UNKNOWN_VARIABLE` | 422 | `POST .../attr/validation/result` body carries `variables` keys not declared in the dataset's `attr/validation/conf.variables` |
| `INVALID_SCORE` | 422 | `POST .../attr/validation/result` body has `score` outside `[0.0, 1.0]` |
| `GENERATION_RUNNING` | 409 | A generation run is already in progress for this dataset |
| `GENERATION_DISABLED` | 409 | Metagen conf has `is_enabled=false`; non-dry-run rejected |
| `METRIC_RUNNING` | 409 | A metric measurement run is already in progress for this metric |
| `METRIC_DISABLED` | 409 | Metric definition has `is_enabled=false`; non-dry-run rejected |
| `ONTOGEN_RUNNING` | 409 | An ontology inference run is already in progress |
| `ONTOGEN_DISABLED` | 409 | Ontogen conf has `is_enabled=false`; non-dry-run rejected |
| `ONTOGEN_TRIPLE_DEPENDENCY_PENDING` | 422 | Triple review attempted while one or more of its subject node, edge, or object node is not yet approved |
| `INVALID_DATASET_URN` | 422 | A `dataset_filter.dataset_urns` entry is not a well-formed `urn:li:dataset:(…)` URN. Validated at PUT/PATCH for both `ontogen/attr/conf` and `metric/{id}/attr/conf` |
| `DATAHUB_UNAVAILABLE` | 502 | DataHub GMS did not respond or returned an error |
| `STORAGE_UNAVAILABLE` | 503 | PostgreSQL or Redis connection failed (including auth refresh fail-closed when the revocation store is unreachable) |
| `INTERNAL_AUTH_NOT_CONFIGURED` | 503 | `X-Internal-Token` shared-secret header is required for `/internal/*` routes but the server-side secret is unset |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests; back off and retry |
| `INTERNAL_ERROR` | 500 | Unhandled `DataSpokeError` with no specific status mapping (fallback) |
