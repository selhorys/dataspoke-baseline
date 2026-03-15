# DataSpoke API

> This document is the master reference for the DataSpoke API — its route catalogue,
> authentication model, request/response conventions, middleware stack, error catalogue,
> and real-time channels.
>
> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Routing model defined in [ARCHITECTURE](../ARCHITECTURE.md).
> Request/response conventions derive from [API_DESIGN_PRINCIPLE](../API_DESIGN_PRINCIPLE_en.md).
> DataHub integration patterns are in [DATAHUB_INTEGRATION](../DATAHUB_INTEGRATION.md).
> Backend services that implement these routes are in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication & Authorization](#authentication--authorization)
3. [Route Catalogue](#route-catalogue)
4. [Request & Response Conventions](#request--response-conventions)
5. [Middleware Stack](#middleware-stack)
6. [Error Catalogue](#error-catalogue)
7. [WebSocket Channels](#websocket-channels)

---

## Overview

The DataSpoke API is a FastAPI (Python 3.13) service that acts as the single ingress for
all DataSpoke clients — the portal UI and external AI agents. It exposes a three-tier URI
structure that maps directly to the user-group taxonomy defined in the MANIFESTO:

```
/api/v1/spoke/common/…     — Cross-cutting features shared across DE, DA, and DG
/api/v1/spoke/de/…         — Data Engineering features
/api/v1/spoke/da/…         — Data Analysis features
/api/v1/spoke/dg/…         — Data Governance features
/api/v1/hub/…              — DataHub pass-through (optional ingress for clients)
```

The API is the only **HTTP-facing** component for external clients (the portal UI and
AI agents). Backend services and Temporal workers also access DataHub, PostgreSQL, Redis,
Qdrant, and Temporal directly but are not exposed over HTTP.

In the future, DataSpoke may also expose **redefined DataHub functions** — blended endpoints that proxy DataHub's basic operations (e.g., dataset creation, metadata browsing) while simultaneously handling DataSpoke-specific data in a single call. These would appear under `/spoke/common/data` as creation and modification routes (e.g., `POST /spoke/common/data`). See [DATAHUB_INTEGRATION §Key principles](../DATAHUB_INTEGRATION.md#overview) for details.

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
DataHub  Postgres  Qdrant / Redis / Temporal
```

### API-First Design

Standalone OpenAPI 3.0 specifications live in `api/` as independent artifacts. AI agents
and the frontend team iterate on those specs without a running backend. The FastAPI
implementation must remain consistent with those artifacts. When a route changes, update
`api/` first, then the implementation.

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

```json
{
  "sub": "user-uuid",
  "email": "user@example.com",
  "groups": ["de"],
  "exp": 1234567890,
  "iat": 1234567890
}
```

The `groups` claim is an array of user-group identifiers (`de`, `da`, `dg`). A user may
belong to multiple groups. The middleware enforces that a request targeting
`/spoke/de/…` must have `"de"` in the `groups` claim.

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

### Auth Flow

```
Client                       DataSpoke API              Identity Store
  │                               │                          │
  │── POST /auth/token ──────────►│                          │
  │   {email, password}           │── verify credentials ───►│
  │                               │◄─ user record, groups ───│
  │◄── {access_token,             │
  │     refresh_token cookie} ────│
  │                               │
  │── GET /spoke/common/data/{dataset_urn}/attr/ingestion/conf ►│
  │   Authorization: Bearer <at>  │── validate JWT, check groups ─► 200 OK
```

---

## Route Catalogue

All routes are prefixed with `/api/v1`. Routes marked **WS** are WebSocket endpoints.

> **User-group routing principle**: User-group-specific paths (`/spoke/de/…`,
> `/spoke/da/…`, `/spoke/dg/…`) should be defined **only when a feature is exclusively
> used by that user group**. For dataset-centric operations — ingestion, validation,
> generation, search — the `/spoke/common/data/{dataset_urn}/…` structure is preferred
> so that any team owning a dataset can access the feature regardless of group membership.
> As a result, the current catalogue has no `/spoke/de` or `/spoke/da` sections; all
> shared dataset operations live under `/spoke/common`.

### Auth

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/auth/token` | Issue access + refresh tokens |
| `POST` | `/auth/token/refresh` | Refresh access token |
| `POST` | `/auth/token/revoke` | Revoke refresh token (logout) |

### Common (`/spoke/common`)

Cross-cutting features consumed by multiple user groups.

#### Ontology

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/ontology` | List concept categories | Ontology Builder | UC4, UC8 |
| `GET` | `/spoke/common/ontology/{concept_id}` | Get concept detail + relationships | Ontology Builder | UC4, UC8 |
| `GET` | `/spoke/common/ontology/{concept_id}/attr` | Get concept attributes (confidence, parent) | Ontology Builder | UC4 |
| `GET` | `/spoke/common/ontology/{concept_id}/event` | Change history for a concept | Ontology Builder | UC4 |
| `POST` | `/spoke/common/ontology/{concept_id}/method/approve` | Approve a pending concept proposal | Ontology Builder | UC4 |
| `POST` | `/spoke/common/ontology/{concept_id}/method/reject` | Reject a pending concept proposal | Ontology Builder | UC4 |

#### Data Resource (`/spoke/common/data/{dataset_urn}`)

The canonical resource for a dataset. All teams (DE, DA, DG) access dataset attributes,
ingestion, validation, and generation through this shared path. Ingestion, validation,
and generation are organized under `attr/` with parallel sub-resource structures: `conf`
(configurations with status), `method` (action triggers), and `event` (success/failure
notices). Validation and generation additionally have `result` (periodic results as
timeseries). In a data-mesh organization any team that owns a dataset can register and
manage ingestion, validation, and generation — DE teams provide deep technical specs
while DA or other teams may register simpler configurations.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/data/{dataset_urn}` | Get dataset summary (identity, owner, tags) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}/attr` | Get dataset attributes (schema summary, ownership, tags) | Data Resource | — |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Get ingestion configuration for dataset | Ingestion Config | UC1 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Create or replace ingestion configuration | Ingestion Config | UC1 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Partially update ingestion configuration | Ingestion Config | UC1 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/ingestion/conf` | Remove ingestion configuration | Ingestion Config | UC1 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/ingestion/method/run` | Trigger ingestion run via Temporal (`dry_run` in body for no-write mode) | Ingestion Execution | UC1 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/ingestion/event` | Ingestion event reports (success/failure notices) | Ingestion Execution | UC1 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Get validation configuration for dataset | Validation Config | UC2, UC3, UC6 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Create or replace validation configuration | Validation Config | UC2, UC3, UC6 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Partially update validation configuration | Validation Config | UC2, UC3, UC6 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Remove validation configuration | Validation Config | UC2, UC3, UC6 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Get validation results (timeseries; `?from=…&to=…` for time range) | Online Data Validator | UC2, UC3, UC6 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/validation/method/run` | Trigger validation run via Temporal (`dry_run` in body for no-write mode) | Online Data Validator | UC2, UC3, UC6 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/event` | Validation event reports (success/failure notices) | Online Data Validator | UC2, UC3, UC6 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/gen/conf` | Get generation configuration (target fields, period, status) | Automated Doc Generation | UC4 |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/gen/conf` | Create or replace generation configuration | Automated Doc Generation | UC4 |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/gen/conf` | Partially update generation configuration | Automated Doc Generation | UC4 |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/gen/conf` | Remove generation configuration | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/gen/result` | Get generation results (historical; `?latest=true` for most recent only) | Automated Doc Generation | UC4 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/gen/method/generate` | Trigger metadata generation run | Automated Doc Generation | UC4 |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/gen/method/apply` | Apply approved generation results to DataHub | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/gen/event` | Generation event reports (success/failure notices) | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/data/{dataset_urn}/event` | Dataset-level event history (all event types) | Data Resource | — |
| **WS** | `/spoke/common/data/{dataset_urn}/stream/validation` | Real-time validation progress stream | Online Data Validator | UC2 |

#### Redefined DataHub Functions *(TBD)*

Future routes for blended dataset creation and modification. Example candidates:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/spoke/common/data` | Create a dataset — write core metadata to DataHub and initialize DataSpoke-side records in a single call |
| `PATCH` | `/spoke/common/data/{dataset_urn}` | Update dataset metadata — blend DataHub aspect writes with DataSpoke-specific updates |

These routes are **not yet defined**; scope and design will be specified when the feature is planned. See [DATAHUB_INTEGRATION §Key principles](../DATAHUB_INTEGRATION.md#overview).

#### Ingestion (`/spoke/common/ingestion`)

A cross-dataset view of ingestion configurations and events. Each entry combines dataset
identity with the ingestion data stored under
`common/data/{dataset_urn}/attr/ingestion/`. Useful for operations dashboards and bulk
management.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/ingestion` | List all ingestion configs across datasets (paginated, filterable) | Ingestion Config | UC1 |
| `GET` | `/spoke/common/ingestion/{dataset_urn}` | Get ingestion config detail (dataset identity + config body) | Ingestion Config | UC1 |
| `GET` | `/spoke/common/ingestion/{dataset_urn}/attr` | Get config attributes (schedule, deep_spec_enabled flag, status, owner) | Ingestion Config | UC1 |
| `PATCH` | `/spoke/common/ingestion/{dataset_urn}/attr` | Update config attributes | Ingestion Config | UC1 |
| `POST` | `/spoke/common/ingestion/{dataset_urn}/method/run` | Trigger ingestion run via Temporal (`dry_run` in body for no-write mode) | Ingestion Execution | UC1 |
| `GET` | `/spoke/common/ingestion/{dataset_urn}/event` | Ingestion event reports (success/failure notices) | Ingestion Execution | UC1 |

#### Validation (`/spoke/common/validation`)

A cross-dataset view of validation configurations, results, and events. Each entry combines
dataset identity with the validation data stored under
`common/data/{dataset_urn}/attr/validation/`. Useful for quality dashboards and bulk rule
management.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/validation` | List all validation configs across datasets (paginated, filterable) | Validation Config | UC2, UC3, UC6 |
| `GET` | `/spoke/common/validation/{dataset_urn}` | Get validation config detail (dataset identity + config body) | Validation Config | UC2, UC3, UC6 |
| `GET` | `/spoke/common/validation/{dataset_urn}/attr` | Get config attributes (rules, result spec, schedule, status, owner) | Validation Config | UC2, UC3, UC6 |
| `PATCH` | `/spoke/common/validation/{dataset_urn}/attr` | Update config attributes | Validation Config | UC2, UC3, UC6 |
| `GET` | `/spoke/common/validation/{dataset_urn}/attr/result` | Get validation results for this dataset (timeseries; `?from=…&to=…` for time range) | Online Data Validator | UC2, UC3, UC6 |
| `POST` | `/spoke/common/validation/{dataset_urn}/method/run` | Trigger validation run via Temporal (`dry_run` in body for no-write mode) | Online Data Validator | UC2, UC3, UC6 |
| `GET` | `/spoke/common/validation/{dataset_urn}/event` | Validation event reports (success/failure notices) | Online Data Validator | UC2, UC3, UC6 |

#### Generation (`/spoke/common/gen`)

A cross-dataset view of generation configurations, results, and events. Each entry
combines dataset identity with the generation data stored under
`common/data/{dataset_urn}/attr/gen/`. Useful for monitoring generation status across
all datasets and bulk management.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/gen` | List all generation configs across datasets (paginated, filterable) | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/gen/{dataset_urn}` | Get generation detail (dataset identity + config + latest result) | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/gen/{dataset_urn}/attr` | Get config attributes (target fields, period, status, owner) | Automated Doc Generation | UC4 |
| `PATCH` | `/spoke/common/gen/{dataset_urn}/attr` | Update config attributes | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/gen/{dataset_urn}/attr/result` | Get generation results for this dataset (historical; `?from=…&to=…` for time range) | Automated Doc Generation | UC4 |
| `POST` | `/spoke/common/gen/{dataset_urn}/method/generate` | Trigger generation run | Automated Doc Generation | UC4 |
| `POST` | `/spoke/common/gen/{dataset_urn}/method/apply` | Apply approved results to DataHub | Automated Doc Generation | UC4 |
| `GET` | `/spoke/common/gen/{dataset_urn}/event` | Generation event reports (success/failure notices) | Automated Doc Generation | UC4 |

#### Search (`/spoke/common/search`)

Natural language search over dataset metadata using vector similarity. Available to all
user groups. Accepts `?sql_context=true` to include text-to-SQL optimized column detail,
sample values, and inferred join paths in the response — superseding the former dedicated
text-to-SQL and join-paths paths.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/common/search` | Natural language search (`?q=…`; add `?sql_context=true` for SQL context + join paths) | Natural Language Search, Text-to-SQL Metadata | UC5, UC7 |
| `POST` | `/spoke/common/search/method/reindex` | Trigger reindex for a dataset (`?dataset_urn=…`) | Natural Language Search | UC5 |

### Data Governance (`/spoke/dg`)

#### Metric (`/spoke/dg/metric`)

Governance metrics are named, configurable measurements tracked over time — for example,
the count of poorly documented datasets, the count of erroneous datasets per medallion
layer, or data downtime duration. Each metric carries a definition (`attr/conf`) that
controls how it is computed, scheduled, and alerted, and a timeseries of measurement
results (`attr/result`). Metrics represent enterprise-wide or department-wide signals
rather than per-dataset observations.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/dg/metric` | List all metrics (paginated; filterable by theme, status) | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}` | Get metric summary (identity, theme, active status) | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr` | Get metric attributes overview (theme, period, active status, alarm enabled) | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr/conf` | Get full metric definition (title, theme, measurement period, alarm setup, active status) | Enterprise Metrics Dashboard | UC6 |
| `PUT` | `/spoke/dg/metric/{metric_id}/attr/conf` | Create or replace metric definition | Enterprise Metrics Dashboard | UC6 |
| `PATCH` | `/spoke/dg/metric/{metric_id}/attr/conf` | Update metric definition fields | Enterprise Metrics Dashboard | UC6 |
| `DELETE` | `/spoke/dg/metric/{metric_id}/attr/conf` | Remove metric definition | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr/result` | Get measurement results (numeric timeseries; `?from=…&to=…` for time range) | Enterprise Metrics Dashboard | UC6 |
| `POST` | `/spoke/dg/metric/{metric_id}/method/run` | Trigger a metric measurement run | Enterprise Metrics Dashboard | UC6 |
| `POST` | `/spoke/dg/metric/{metric_id}/method/activate` | Activate metric (enable scheduled measurement) | Enterprise Metrics Dashboard | UC6 |
| `POST` | `/spoke/dg/metric/{metric_id}/method/deactivate` | Deactivate metric | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}/event` | Metric run events and alarm notices | Enterprise Metrics Dashboard | UC6 |
| **WS** | `/spoke/dg/metric/stream` | Real-time metric update stream | Enterprise Metrics Dashboard | UC6 |

##### Metric Issue (`/spoke/dg/metric/{metric_id}/attr/issue`)

Auto-detected metadata issues with lifecycle tracking. Metric issues are created
automatically when a measurement run detects gaps (e.g., datasets missing owners,
descriptions, or tags) and auto-resolved when subsequent runs find the gap fixed.
Unlike events (immutable log entries), metric issues are **stateful action items**
with a four-state lifecycle (`open → in_progress → resolved | dismissed`), an
assignee, and a due date. The `dismissed` transition is a manual business decision
via the API; `resolved` is set automatically by the measurement pipeline.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/dg/metric/{metric_id}/attr/issue` | List metric issues (paginated; filterable by `status`, `priority`, `issue_type`, `assignee`) | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr/issue/{metric_issue_id}` | Get metric issue detail (type, priority, status, assignee, projected score impact) | Enterprise Metrics Dashboard | UC6 |
| `PATCH` | `/spoke/dg/metric/{metric_id}/attr/issue/{metric_issue_id}` | Update metric issue fields (`status`, `assignee`, `due_date`) | Enterprise Metrics Dashboard | UC6 |
| `POST` | `/spoke/dg/metric/{metric_id}/attr/issue/{metric_issue_id}/method/dismiss` | Dismiss metric issue — acknowledged, will not fix | Enterprise Metrics Dashboard | UC6 |
| `GET` | `/spoke/dg/metric/{metric_id}/attr/issue/{metric_issue_id}/event` | Metric issue lifecycle events (status transitions, assignment changes) | Enterprise Metrics Dashboard | UC6 |

#### Overview (`/spoke/dg/overview`)

Additional perspectives on the data estate that cannot be expressed as per-metric
timeseries: graph-based topology views, medallion layer coverage maps, and similar
structural views. Use these paths only when the `/spoke/dg/metric` routes are
insufficient to represent the data needed.

| Method | Path | Purpose | Feature | UC |
|--------|------|---------|---------|-----|
| `GET` | `/spoke/dg/overview` | Get multi-perspective overview snapshot (graph + medallion coverage) | Multi-Perspective Data Overview | UC8 |
| `GET` | `/spoke/dg/overview/attr` | Get visualization config (layout, coloring, filters) | Multi-Perspective Data Overview | UC8 |
| `PATCH` | `/spoke/dg/overview/attr` | Update visualization config | Multi-Perspective Data Overview | UC8 |

### DataHub Pass-Through (`/hub`)

Optional ingress that forwards requests to DataHub GMS. Useful for clients that want a
single base URL. Authentication is still enforced by DataSpoke; the request is proxied
after JWT validation.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/hub/graphql` | Proxy DataHub GraphQL queries |
| `*` | `/hub/openapi/{path:path}` | Proxy DataHub REST OpenAPI endpoints (all methods) |

### Admin (`/admin`)

Routes for user management and system configuration. Accessible only to users with `"admin"` in the `groups` claim. Specific routes are defined in dedicated admin feature specs and are not catalogued here.

### System

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness check (no auth required) |
| `GET` | `/ready` | Readiness check (verifies DataHub, PostgreSQL, Redis connectivity) |

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

- `attr` — Read or update a subset of resource attributes (configuration, thresholds,
  visualization settings). Use `GET` to read, `PATCH` to update partial fields.
- `method` — Business actions that go beyond CRUD: `run`, `approve`, `reject`,
  `apply`, `generate`, `reindex`. Always `POST`. Use `dry_run` in the request body
  for no-write mode instead of separate dry-run paths.
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

Requests pass through middleware in the following order:

```
Incoming Request
       │
       ▼
┌─────────────────────────┐
│ 1. CORS                 │  Allow configured origins; reject others with 403
├─────────────────────────┤
│ 2. Request Logging      │  Log method, path, trace ID, client IP (before handler)
├─────────────────────────┤
│ 3. Auth (JWT Validate)  │  Verify signature, expiry, extract claims
│                         │  Skip for /health, /ready, /auth/*
├─────────────────────────┤
│ 4. Group Enforcement    │  Check groups claim against URI tier
│                         │  Return 403 if insufficient
├─────────────────────────┤
│ 5. Rate Limiting        │  Token-bucket per user (Redis-backed)
│                         │  Default: 120 req/min; burst: 20
├─────────────────────────┤
│ 6. Route Handler        │  FastAPI dependency injection + business logic
├─────────────────────────┤
│ 7. Response Logging     │  Log status code, latency, trace ID (after handler)
└─────────────────────────┘
       │
       ▼
Outgoing Response
```

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
  "trace_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

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
| `422 Unprocessable Entity` | Pydantic validation failure (field type mismatch, constraint violation) |
| `429 Too Many Requests` | Rate limit exceeded; `Retry-After` header is set |
| `502 Bad Gateway` | DataHub GMS unreachable or returned an unexpected error |
| `503 Service Unavailable` | Temporal, PostgreSQL, or Qdrant connection failure |

### Application Error Codes

| `error_code` | HTTP | Description |
|-------------|------|-------------|
| `INVALID_PARAMETER` | 400 | Query param or body field fails validation |
| `MISSING_REQUIRED_FIELD` | 400 | Required body field not provided |
| `UNAUTHORIZED` | 401 | Token missing, expired, or malformed |
| `FORBIDDEN` | 403 | Valid token; groups claim does not satisfy route requirement |
| `DATASET_NOT_FOUND` | 404 | Dataset URN does not exist in DataHub |
| `CONCEPT_NOT_FOUND` | 404 | Ontology concept ID not found |
| `CONFIG_NOT_FOUND` | 404 | Ingestion config or validation config not found |
| `METRIC_NOT_FOUND` | 404 | Metric ID does not exist |
| `METRIC_ISSUE_NOT_FOUND` | 404 | Metric issue ID does not exist |
| `DUPLICATE_CONFIG` | 409 | Config with same name already exists |
| `INGESTION_RUNNING` | 409 | An ingestion run is already in progress for this config |
| `VALIDATION_RUNNING` | 409 | A validation run is already in progress for this config |
| `GENERATION_RUNNING` | 409 | A generation run is already in progress for this dataset |
| `DATAHUB_UNAVAILABLE` | 502 | DataHub GMS did not respond or returned an error |
| `STORAGE_UNAVAILABLE` | 503 | PostgreSQL, Redis, or Qdrant connection failed |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests; back off and retry |

---

## WebSocket Channels

WebSocket connections follow the same authentication model as REST: the client must send
a valid JWT in the first message after opening the connection.

### Connection Handshake

```
Client                        DataSpoke API
  │                                │
  │── WS upgrade ─────────────────►│
  │                                │
  │── {"type":"auth",              │
  │    "token":"<access_token>"} ──►│
  │                                │── validate JWT
  │◄── {"type":"auth_ok"} ─────────│
  │                                │   (stream begins)
  │◄── {"type":"progress", …} ─────│
  │◄── {"type":"result", …} ───────│
  │                                │── server closes on completion
```

If auth fails, the server sends `{"type":"auth_error","error_code":"UNAUTHORIZED"}` and
closes the connection.

### Validation Progress Stream (`/spoke/common/data/{dataset_urn}/stream/validation`)

Messages sent during a validation run:

```json
{"type": "progress", "step": "fetch_aspects", "pct": 20, "msg": "Fetching DataHub aspects"}
{"type": "progress", "step": "compute_score", "pct": 60, "msg": "Computing quality score"}
{"type": "progress", "step": "anomaly_detect", "pct": 80, "msg": "Running anomaly detection"}
{"type": "result",
 "status": "completed",
 "quality_score": 78,
 "issues": [{"type": "freshness", "severity": "warning", "detail": "Last updated 3 days ago"}],
 "recommendations": ["Review freshness SLA", "Add ownership tag"]}
```

### Metric Update Stream (`/spoke/dg/metric/stream`)

Pushed when the Temporal metrics collection workflow emits an update:

```json
{"type": "metric_update",
 "metric_id": "poorly-documented-datasets",
 "measured_at": "2026-02-27T10:00:00.000Z",
 "value": 42,
 "alarm": false}
```
