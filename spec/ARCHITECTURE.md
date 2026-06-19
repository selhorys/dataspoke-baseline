# DataSpoke: System Architecture

> This document defines the system-wide architecture for DataSpoke.
> Conforms to [MANIFESTO](MANIFESTO_en.md) (highest authority).
> For API conventions see [API_DESIGN_PRINCIPLE](API_DESIGN_PRINCIPLE_en.md).
> For DataHub SDK/aspect patterns see [DATAHUB_INTEGRATION](DATAHUB_INTEGRATION.md).

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Core Principles](#core-principles)
3. [System Components](#system-components)
4. [Data Flow](#data-flow)
5. [Feature-to-Architecture Mapping](#feature-to-architecture-mapping)
6. [Shared Services](#shared-services)
7. [Technology Stack](#technology-stack)
8. [Deployment Architecture](#deployment-architecture)
9. [Repository Structure](#repository-structure)
10. [Design Decisions](#design-decisions)

---

## Architecture Overview

### Hub-and-Spoke Model

DataSpoke is a **loosely coupled sidecar** to DataHub. DataHub is the Hub (metadata SSOT);
each organization-specific extension built on top of DataSpoke is a Spoke.

```
┌───────────────────────────────────────────────┐
│                 DataSpoke UI                  │
│   Single shell · function-based menu:         │
│   Governance · Ingestion · Validation ·       │
│   OntoGen · MetaGen                           │
└───────────────────────┬───────────────────────┘
                        │
┌───────────────────────▼───────────────────────┐
│                DataSpoke API                  │
└───────────┬───────────────────────┬───────────┘
            │                       │
┌───────────▼───────────┐ ┌────────▼────────────┐
│       DataHub         │ │      DataSpoke      │
│    (metadata SSOT)    │ │  Backend / Pipeline │
│                       │ │  + Shared Services  │
└───────────────────────┘ └─────────────────────┘
```

### Deployment Boundary

DataHub is deployed and managed **separately** — DataSpoke connects to it as an external
dependency.

```
┌─────────────────────────┐         ┌─────────────────────────┐
│   DataSpoke Stack       │         │   DataHub Instance      │
│                         │  SDK    │   (External)            │
│   UI                    │◄───────►│   GMS                   │
│   API                   │  Kafka  │   Kafka                 │
│   Backend               │  GQL    │   Search (ES)           │
│   PostgreSQL (pgvector  │         │   MySQL / Postgres      │
│   + AGE) / Redis /      │         │                         │
│   Airflow                │         │                         │
└─────────────────────────┘         └─────────────────────────┘
```

**Rationale**: DataSpoke is a sidecar extension, not a DataHub replacement. Enterprises have
existing DataHub installations; loose coupling enables independent deployment and evolution.

### Key Architectural Tenets

1. **DataHub-backed SSOT** — DataHub stores metadata; DataSpoke extends without modifying core.
2. **Five-feature baseline** — The Baseline Product implements five MANIFESTO features (Ingestion
   Control, Validation, Ontology Generation, Metadata Generation, Governance). All backend
   services, API routes, and UI surfaces map to one of these five.
3. **Two-Axis API Routing** — Per-dataset, cross-feature routes live under
   `/spoke/common/data/{dataset_urn}/…` (the dataset resource). Cross-dataset list
   views and global features live under one namespace per MANIFESTO §2.1 feature —
   `/spoke/ingestion`, `/spoke/validation`, `/spoke/ontogen/`, `/spoke/metagen/`,
   `/spoke/governance/`. DataHub pass-through under `/hub/`.
4. **API-First** — The FastAPI implementation in `src/api/` is the single source of truth for the
   API contract, with auto-generated OpenAPI docs enabling parallel frontend development and
   AI-agent iteration.
5. **Layer Separation** — Four components (UI, API, Backend/Pipeline, DataHub) are independently
   scalable and replaceable.
6. **Cloud-Native** — Kubernetes-ready with containerized deployments.
7. **Blended API/UI for Convenience** — DataSpoke may re-expose DataHub's basic functions through
   its own API and UI layer, combining DataHub-native and DataSpoke-specific metadata in a single
   call (see [DATAHUB_INTEGRATION §Key principles](DATAHUB_INTEGRATION.md#overview)).
8. **DataSpoke owns user identity and role** — DataSpoke's `users` table is the SSOT for both
   identity (email, password hash, Google `sub`) and role (`Admin` / `Editor` / `Reader`).
   DataHub holds a propagated copy of role on the shadow `corpuser` record, used only for
   DataHub-UI authorisation; role changes go DataSpoke→DataHub via `batchAssignRole`, and a
   nightly `auth-role-sync-daily` DAG reconciles drift (DataSpoke wins). DataHub UI access uses
   Google OIDC pointed at the same OAuth client as DataSpoke. JWT carries identity only
   (`sub`, `email`, `exp`, `iat`); per-request DB role lookup gates `/admin/*` and applies the
   method × role matrix on `/spoke/*` and `/hub/*` (Reader = GET only). Long-lived API tokens
   are opaque, self-service minted under `/auth/api-tokens`, with intersection privilege
   (`effective = min(role_snapshot, current users.role)`). See [feature/AUTH.md](feature/AUTH.md).

---

## Core Principles

### 1. DataHub as Metadata SSOT

DataHub is the **mandatory backend** for metadata persistence. DataSpoke tries not to duplicate
metadata that DataHub already persists — it reads from and writes to DataHub, adding a
computational layer on top.

| Role | Responsibility |
|------|---------------|
| **DataHub** | Persist metadata aspects, emit change events, serve GraphQL queries |
| **DataSpoke** | Validation result store + historical-result cache for external pipelines, semantic search, ontology proposals, enrichment, metrics |

Integration channels (read, write, event) and their SDK patterns are defined in
[`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

### 2. API Convention Compliance

All REST APIs conform to [`API_DESIGN_PRINCIPLE_en.md`](API_DESIGN_PRINCIPLE_en.md). The
architecture enforces this through shared middleware for request/response formatting,
content/metadata separation, and error handling.

### 3. API-First Design

The FastAPI implementation in `src/api/` is the **single source of truth** for the API contract.
FastAPI auto-generates OpenAPI 3.0 documentation from Pydantic models and route definitions:
- AI agents read route definitions and Pydantic schemas directly from `src/api/` for accurate,
  always-in-sync API knowledge.
- Frontend development references the live ReDoc UI (`/redoc`) or reads `src/api/routers/` for
  the current contract.
- The API spec at `spec/API.md` defines the architectural route catalogue; the
  implementation must conform to it.

### 4. Layer Separation

| Benefit | Mechanism |
|---------|-----------|
| Independent scaling | UI, API scale separately; Airflow handles workflow execution |
| Technology flexibility | Swap Next.js for another framework without affecting backend |
| Security boundaries | UI never accesses DB directly |
| Team autonomy | Frontend and backend teams work independently |

---

## System Components

### 1. DataSpoke UI

**Technology**: Next.js (TypeScript)

A single shell with a function-based left-side menu — Governance (Dashboard + Metrics),
Ingestion Control, Validation, OntoGen, MetaGen. Provides:
- Chart visualizations for the Governance Dashboard and metric trends
- Interactive graph rendering for ontology visualization (UC3 nodes / triples)
- Polling-based live freshness against `event/...` and `attr/.../result` endpoints (no WebSocket / SSE in the baseline API)

For layout, shared components, routing, and auth, see
[`spec/feature/FRONTEND_BASIC.md`](feature/FRONTEND_BASIC.md). Per-function specs:
[`FRONTEND_GOVERNANCE.md`](feature/FRONTEND_GOVERNANCE.md),
[`FRONTEND_INGESTION.md`](feature/FRONTEND_INGESTION.md),
[`FRONTEND_VALIDATION.md`](feature/FRONTEND_VALIDATION.md),
[`FRONTEND_ONTOGEN.md`](feature/FRONTEND_ONTOGEN.md),
[`FRONTEND_METAGEN.md`](feature/FRONTEND_METAGEN.md).

### 2. DataSpoke API

**Technology**: FastAPI (Python 3.13)

Function-based URI structure — one namespace per MANIFESTO §2.1 feature:

```
/api/v1/spoke/common/data/{dataset_urn}/...  → Dataset resource (per-dataset, cross-feature)
/api/v1/spoke/ingestion                      → Ingestion Control cross-dataset list
/api/v1/spoke/validation                     → Validation cross-dataset list
/api/v1/spoke/ontogen/...                    → Ontology Generation (global singleton)
/api/v1/spoke/metagen/...                    → Metadata Generation (conf collection + global review queue)
/api/v1/spoke/governance/...                 → Governance metrics
/api/v1/hub/...                              → DataHub pass-through (optional ingress for clients)
```

RESTful CRUD only — the baseline API has no WebSocket or SSE surface; clients poll
`event/...` and `attr/.../result` endpoints for live freshness.

For the complete route catalogue, JWT authentication model, middleware stack, and error
catalogue, see [`spec/API.md`](API.md).

### 3. DataSpoke Backend / Pipeline

**Technology**: Python 3.13, Airflow for orchestration

Core computational layer. For the full backend specification — layered architecture, shared
services, per-feature service designs, Airflow workflows, and infrastructure integration
patterns — see [`spec/feature/BACKEND.md`](feature/BACKEND.md). LLM inference loop,
per-service validator rule tables, the opt-in adversarial debate framework, and test-mode
toggles in [`spec/feature/BACKEND_LLM.md`](feature/BACKEND_LLM.md). Data contracts
(PostgreSQL schema including pgvector tables) in
[`spec/feature/BACKEND_SCHEMA.md`](feature/BACKEND_SCHEMA.md).

**Key capabilities by feature** (MANIFESTO §2.1):

| Feature | Capabilities |
|---------|-------------|
| Ingestion Control | Per-source ingestion in three modes — `DATAHUB_MANAGED` (DataHub's recipe + cron; DataSpoke syncs the definition + maps datasets), `ACTIVE_CUSTOM_MANAGED` (DataSpoke's pluggable/forkable extractor runs on a tier schedule), `PASSIVE` (external ingestor; DataSpoke records registration + declared scope and mirrors run history). Source→dataset mapping makes all ingestion visible, including an unmanaged bucket |
| Validation | One validation slot per dataset (description + declared variable names) emitted as DataHub `assertionInfo`; ingestion of pipeline-emitted timeseries results emitted as `assertionRunEvent`; historical-result query (`from`/`until`) for use as a baseline cache. |
| Ontology Generation | Singleton-config + Markdown-seed LLM pipeline that emits a subject / predicate / object triple ontology — nodes (subjects/objects), edges (predicates), and triples (facts) — from a fixed DataHub aspect set (`datasetProperties`, `schemaMetadata`, `editableDatasetProperties`, `editableSchemaMetadata`, `glossaryTerms`, and `documentInfo` on related `document` entities); persisted in PostgreSQL (relational + pgvector) and surfaced through the ontogen API with independent review queues per result type |
| Metadata Generation | A collection of named generation confs, each with its own `dataset_filter` / `schedule_tier` / budget, proposing candidate values for the editable description aspects — `editableDatasetProperties.description` (one slot per dataset) and `editableSchemaMetadata.editableSchemaFieldInfo[].description` (one slot per column) — gated by a per-dataset opt-in boundary shared across confs. Each `(conf, item)` accumulates up to `result_limit` candidates across runs; one global review queue lets a reviewer approve one candidate per item (immediate DataHub emit, item locked across all confs) and reject others (cleared at next run). Producer-Reviewer adversarial debate identical to UC3 ontogen, except only debate-accepted candidates persist. Cross-conf consistency held by the shared ontology, not one conf. |
| Governance | Active metric aggregation (pure aggregation over DataHub metadata + DataSpoke results) with three built-in types — `ingestion-freshness`, `validation-score`, `doc-health` — each emitting named floating-point `values` plus a per-dataset breakdown over a configurable `dataset_filter` (`origin` AND-ed with OR-ed tags/glossary_terms/dataset_urns); scheduled per `schedule_tier` or run on demand. Passive ingestion of externally-computed metric results is reserved for a future release. |

Source layout: `src/backend/` (feature services), `src/workflows/` (Airflow DAGs + helpers),
`src/shared/` (DataHub client, shared models, LLM integration).

### 4. DataHub (External)

DataHub is deployed and managed separately. DataSpoke interacts through three channels:

| Channel | Direction | Purpose |
|---------|-----------|---------|
| Python SDK (read) | DataHub → DataSpoke | Query metadata aspects, ingestion run history, assertion results, `document` entities (filtered by `relatedAssets` on in-scope datasets) |
| Python SDK (write) | DataSpoke → DataHub | Emit ingestion-extracted aspects, validation `assertionInfo` (on conf upsert) + `assertionRunEvent` (per pipeline-posted result) + `status` (on soft-delete / restore), editable description aspects (`editableDatasetProperties`, `editableSchemaMetadata`), `documentInfo` on `document` entities (and `Status.removed=true` for soft-delete) |
| Kafka events *(optional)* | DataHub → DataSpoke | Available for future event-driven extensions; not consumed by baseline UC1–UC5 flows |

For SDK entry points, aspect catalog, error handling, and configuration, see
[`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

### 5. Supporting Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Message Broker | Kafka | Event streaming (shared with DataHub) |
| Orchestration | Airflow | Workflow execution via Python DAGs and HttpOperator tasks (ingestion, embedding sync, metrics collection). Validation results originate in the data pipeline, not in DataSpoke's Airflow. |
| Operational DB | PostgreSQL 17 (pgvector + AGE) | Ingestion configs, validation configs (description + declared variables) and pipeline-posted results, health scores, ontology graph, user preferences, **vector embeddings** (pgvector); Apache AGE graph extension is installed as reserved infrastructure |
| Cache | Redis | API response caching, rate limiting |
| LLM Provider | External API | Semantic analysis, ontology construction, documentation generation, code interpretation |

---

## Data Flow

Per-feature behaviour is defined in [USE_CASE](USE_CASE_en.md). This section captures only
the cross-cutting flow that ties the features together.

| UC | Trigger surface | Implementation entry | DataHub side-effect |
|---|---|---|---|
| UC1 Ingestion Control | Airflow tier DAG (`ACTIVE_CUSTOM_MANAGED`) or hourly `datahub-sync-hourly` DAG (`DATAHUB_MANAGED`/`PASSIVE` sync); manual `POST .../sources/{id}/method/run` (`ACTIVE_CUSTOM_MANAGED` only) | `IngestionService` | `ACTIVE_CUSTOM_MANAGED`: pluggable extractor emits `Status` + `DatasetProperties` + `SchemaMetadata` + `DataProcessInstance` aspects. `DATAHUB_MANAGED`/`PASSIVE`: no aspect writes; syncs source defs + run history and rebuilds the source→dataset mapping. |
| UC2 Validation | External pipeline `POST .../attr/validation/result` after each partition write; `PUT/PATCH/DELETE .../attr/validation/conf` for configuration | `ValidationService` (config + result store; runs no validation logic) | Emits `assertionInfo` on conf upsert; emits `assertionRunEvent` per pipeline-posted result (timestamped to `data_time`); emits `status.removed` on DELETE / restore (`method/restore`). |
| UC3 Ontology Generation | Airflow tier DAG (singleton conf); manual `POST /spoke/ontogen/method/run` | `OntogenService` | None — UC3 is read-only on the DataHub side; approval flips status in DataSpoke storage only. |
| UC4 Metadata Generation | Airflow tier DAG fans out across enabled confs at the tier; manual `POST /spoke/metagen/conf/{conf_id}/method/run` per conf | `MetagenService` | On reviewer approval of a candidate only: writes to the editable description aspect (`editableDatasetProperties.description` for dataset-description items, `editableSchemaMetadata.editableSchemaFieldInfo[].description` for column-description items) — never to non-editable counterparts. |
| UC5 Governance | Airflow tier DAG; manual `POST /spoke/governance/metric/{id}/method/run` | `MetricsService` (pure aggregation, no source-DB reads) + `OverviewService` | Read-only — never writes aspects. Aggregates over DataHub metadata + DataSpoke result tables. |

Cross-cutting invariants:

- UC1, UC3, UC4, UC5 are schedule-driven via Airflow tier DAGs (`hourly` / `daily` /
  `weekly`) plus on-demand `POST .../method/run`. UC2 is pipeline-driven — the data
  pipeline computes results and POSTs them to `attr/validation/result`. The Kafka
  consumer pattern is reserved for organisation-specific extensions.
- All `dataset_filter`-bearing features (UC3 ontogen conf, UC4 metagen conf, UC5 metric
  definitions) share the same `tags` / `glossary_terms` / `dataset_urns` shape; unresolved
  `dataset_urns` surface on the corresponding `RUN_COMPLETE` event's `unresolved_urns`
  field.
- All `method/run` actions (UC1, UC3, UC4, UC5) are guarded by per-resource concurrency
  locks (`409 *_RUNNING`), and reviewer triples gate on dependency status
  (`422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`).

---

## Feature-to-Architecture Mapping

Maps the five MANIFESTO features to the system components and infrastructure they require.
Each feature owns a top-level namespace under `/spoke/`.

| Feature | UC | API Route | Backend Services | Infrastructure |
|---------|----|-----------|------------------|----------------|
| Ingestion Control | UC1 | `/spoke/ingestion/sources` (per-source CRUD + `/method/run`, `/datasets`, `/event`), `/spoke/ingestion/unmanaged`, read-only `/spoke/common/data/{urn}/attr/ingestion` | Ingestion Service (pluggable extractors + DataHub-managed/passive sync), Extractor Registry | Airflow (tier-based periodic DAGs + hourly `datahub-sync-hourly`), Redis (concurrency guard), DataHub SDK, PostgreSQL |
| Validation | UC2 | `/spoke/validation` (cross-dataset list) + `/spoke/common/data/{urn}/attr/validation/{conf,result}` + `/event/validation` | Validation Config Manager (PUT/PATCH/DELETE conf → `assertionInfo` / `status`), Result Store (POST/GET result → `assertionRunEvent`) | DataHub SDK, PostgreSQL |
| Ontology Generation | UC3 | `/spoke/ontogen/` (singleton conf + Markdown seeds + node / edge / triple browse + review) | LLM Classification, Relationship Inference, Triple Composition, Review Queue (node + edge + triple) | LLM API, PostgreSQL (pgvector), Airflow (tier-based periodic DAG) |
| Metadata Generation | UC4 | `/spoke/metagen/conf` (conf collection: CRUD + per-conf `/method/run`, `/event`) + `/spoke/metagen/{item,event,uncovered}` (global review queue, cross-conf events, uncovered datasets) + `/spoke/common/data/{urn}/attr/metagen/{boundary,item}` + `/event/metagen` | Metadata Generation Service, Producer-Reviewer Adversarial Debate, Per-Item Candidate Review Queue | LLM API, PostgreSQL (pgvector for `metagen_candidate_embeddings`), DataHub SDK (read context + approved writes to editable description aspects), Airflow (tier-based periodic DAGs fanning out across confs) |
| Governance | UC5 | `/spoke/governance/metric/` | Metrics Aggregator (pure aggregation, no source-DB reads), Built-in measurers (`ingestion-freshness`, `validation-score`, `doc-health`), Per-Dataset Breakdown, Factory-Default Bootstrap | Airflow (tier-based periodic DAGs + on-demand `metrics`), PostgreSQL, DataHub GraphQL |

### Optional / future routes

| Surface | Notes |
|---------|-------|
| Redefined DataHub Functions *(TBD)* — `/spoke/common/data` (create/modify) | Blended API/UI that proxies DataHub reads/writes alongside DataSpoke-specific data; scope to be specified when planned |

### Cross-Cutting Infrastructure

| Concern | Infrastructure | Consumers |
|---------|----------------|-----------|
| Airflow DAGs | Airflow | Periodic active-custom ingestion (UC1), DataHub-managed/passive source + mapping sync (UC1, hourly `datahub-sync-hourly`), ontology re-inference (UC3), metadata generation (UC4), governance metrics (UC5). Validation (UC2) is **not** scheduled by DataSpoke — pipelines POST results directly. |
| PostgreSQL Operational Tables | PostgreSQL (pgvector + AGE reserved) | Ingestion sources (per-source recipes) + source→dataset mapping, validation configs (description + declared variables) and pipeline-posted results (data_time, score, variables), ontology seeds + nodes + edges + triples + embeddings, metadata generation proposals + review state, governance metric results |
| Redis Caching | Redis | API response cache, rate limiting, JWT refresh-token revocation list |
| Kafka Event Consumers *(optional)* | Kafka (shared with DataHub) | Reserved for future event-driven cross-feature triggers; not used by baseline UC1–UC5 flows, which are schedule-driven via Airflow |

---

## Shared Services

Reusable backend services consumed by multiple features. These live in `src/shared/`.

### Ontology Generator

Own feature (UC3) that also serves UC4 (Metadata Generation) and UC5 (Governance) as consumers.

**Purpose**: LLM-powered service that builds and maintains a **subject / predicate / object
triple ontology** from a fixed DataHub aspect set (`datasetProperties`, `schemaMetadata`,
`editableDatasetProperties`, `editableSchemaMetadata`, `glossaryTerms`, and `documentInfo`
on related `document` entities) and human-authored Markdown seeds.

**Processing pipeline**:
1. **Dataset → Node Inference** — LLM analyses schema, descriptions, glossary terms,
   related document bodies, and active seeds to propose nodes (subjects / objects) with
   member datasets
2. **Edge (Predicate) Inference** — semantic analysis proposes the relationship-type
   vocabulary used across the dataset estate
3. **Triple Composition** — pairwise reasoning proposes `(subject_node, edge, object_node)`
   facts referencing already-proposed nodes and edges
4. **Confidence Scoring & Human Review Queue** — each node, edge, and triple carries an
   independent confidence score and review status; review proceeds nodes → edges →
   triples via `POST /spoke/ontogen/result/{node|edge|triple}/{id}/method/review`

**Storage** (PostgreSQL with pgvector):
- `ontogen_seeds` — id, markdown body, status (relational)
- `ontogen_nodes` — id, name, description, confidence, status (relational)
- `dataset_node_map` — dataset_urn, node_id, confidence_score, is_primary (relational)
- `ontogen_edges` — id, label, semantics, confidence, status (relational)
- `ontogen_triples` — id, subject_node_id, edge_id, object_node_id, confidence, status (relational)
- `node_embeddings`, `dataset_embeddings` — pgvector tables for similarity recall

**Properties**: Re-inference on the configured `schedule_tier`; dry-run mode evaluates without
persisting; human-in-the-loop review at three layers (node, edge, triple) with a triple
gated on its endpoint nodes and edge being `status='approved'` (human-approved; an
`llm_approved` dependency does not satisfy the gate — `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`
otherwise); approval flips the entry's `status` in DataSpoke storage with no DataHub side
effect.

### DataHub Client Wrapper

Shared by all features.

**Purpose**: Thin wrapper around `acryl-datahub` SDK providing connection management, retry
logic, and convenience methods. Patterns defined in
[`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

---

## Technology Stack

### Runtime Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Frontend | Next.js + TypeScript | SSR, React ecosystem, type safety |
| API | FastAPI (Python 3.13) | Async support, auto OpenAPI docs, Pydantic validation |
| Backend | Python 3.13 | Rich data/ML libraries, DataHub SDK compatibility |
| Message Broker | Kafka | DataHub integration standard |
| Orchestration | Airflow 3.1.8 | Python DAG definitions, HttpOperator tasks calling internal activity endpoints, built-in scheduling and retry; LocalExecutor on the dev profile |
| Operational DB | PostgreSQL 17 (pgvector + AGE) | ACID guarantees, JSONB flexibility, first-class vector similarity (pgvector); Apache AGE installed as reserved graph infrastructure |
| Cache | Redis | API caching, rate limiting, session management |
| LLM Integration | External API (via LangChain) | Semantic analysis, ontology, documentation, code interpretation |
| LLM Observability | Self-hosted Langfuse | Per-run trace store for prompts, completions, tool calls, and token counts; sibling subsystem with its own chart and namespace. See [`spec/feature/BACKEND_LLM.md` §Observability](feature/BACKEND_LLM.md#observability) |
| Charts | Highcharts / Recharts | Rich visualization (metrics dashboards, graph views) |

### Development Stack

| Purpose | Technology |
|---------|-----------|
| Package Manager | uv |
| API Testing | pytest, httpx |
| Frontend Testing | Vitest, React Testing Library |
| E2E Testing | Playwright (use-case + ground groups) |
| Linting | ruff (Python), ESLint (TypeScript) |
| Formatting | ruff format (Python), Prettier (TypeScript) |
| Type Checking | mypy (Python), TypeScript compiler |
| CI/CD | GitHub Actions |
| Container Runtime | Docker |
| Orchestrator | Kubernetes + Helm |

For full testing conventions, toolchain configuration, mocking rules, and the integration test
lock protocol, see [`spec/TESTING.md`](TESTING.md).

---

## Deployment Architecture

### Kubernetes Topology

DataHub runs in a separate namespace or cluster. DataSpoke deploys into its own namespace
(`dataspoke-01` by default) containing: `dataspoke-frontend` + `dataspoke-api` (Deployments, ingress-exposed),
`dataspoke-event-consumer` (optional Kafka consumer Deployment — opt-in for organisations
adding event-driven extensions; not deployed in the baseline),
`dataspoke-airflow-api-server` + `dataspoke-airflow-scheduler` +
`dataspoke-airflow-triggerer` (Airflow 3.1 LocalExecutor; api-server replaces the former Flask
webserver), `postgresql` (StatefulSet with PV — custom image layering pgvector + Apache AGE on
PG 17), and `redis` (Deployment). External dependencies are `datahub-gms:8080` (GraphQL/REST)
and `datahub-kafka:9092` (event streaming).

`dataspoke-event-consumer` is **disabled by default** — baseline UC1–UC5 are schedule-driven
via Airflow tier DAGs and do not subscribe to DataHub MCL events. Enable the separate pod
when an organisation adds event-driven extensions; Kafka consumers scale by partition count.

Langfuse runs as a sibling subsystem in its own namespace (`langfuse-01` by default) — web,
worker, and bundled Postgres / Redis / ClickHouse / MinIO — installed from
`helm-charts/langfuse/`. DataSpoke reads the Langfuse connection (host + public/secret key)
from the DB `peripheral_config` table, set via `/api/v1/admin/peripherals/langfuse`; absence
of the configuration disables tracing without affecting LLM call success.

For replica counts, resource requests/limits, PV sizes, component matrix, and network policy,
see [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md). DataSpoke's namespace requires egress
access to the DataHub namespace; configure NetworkPolicy in clusters with default-deny.

### Configuration

Configuration splits across two mechanisms. **App-runtime settings** flow as environment
variables (three tiers below); **peripheral connections** (DataHub URL/token, Langfuse
host/keys) and **behavioral tunables** (LLM provider/model, generation knobs) are runtime
configuration stored in the DB and edited via `/api/v1/admin/peripherals/{datahub,langfuse}`
and `/api/v1/admin/conf` (see [`spec/API.md` §Admin](API.md)), seeded with factory defaults.

| Prefix | Scope | Who reads it |
|--------|-------|-------------|
| `DATASPOKE_*` (no `KUBE`/`DEV`/`TEST`) | App runtime, both profiles | DataSpoke app code (FastAPI, frontend) |
| `DATASPOKE_KUBE_*` | Kube deployment, both profiles | `helm-charts/bin/*.sh` install/uninstall/build scripts |
| `DATASPOKE_DEV_*` | Dev profile only | `helm-charts/bin/peripherals/*.sh`, `helm-charts/bin/post-install/*.sh` |
| `DATASPOKE_TEST_*` | Dev profile only | `tests/integration/{conftest.py,util/*}` — laptop-side test access auto-populated by `install.sh`; never read by app pods |

App-runtime variables (`DATASPOKE_*`) are the same names in dev and prod — only the values
differ. In dev they point to the nginx-ingress external IP (TCP services) or ingress hostnames
(HTTP services); in prod they are injected via Helm values → ConfigMap/Secret. Groups carried
by `.env` and Secret: PostgreSQL, Redis, Airflow, internal-auth token. Chart-values-only
toggles (rendered onto the API pod from `values{-dev}.yaml`, never via `.env`): CORS origins,
stub-auth, test-mode LLM real-call.

Kube-deployment variables (`DATASPOKE_KUBE_*`) configure the cluster context, namespace, image
registry, cloud vendor, and ingress IP/domain — all needed by install scripts in either profile.

Dev-only variables (`DATASPOKE_DEV_*`) hold install-time credentials and seed values for the
in-cluster peripherals (DataHub MySQL, Langfuse internals, dummy data, LLM provider/model/key).
The peripheral scripts auto-populate the connection outputs back into `.env` and then PATCH
them into the runtime DB. The application code never reads `DATASPOKE_DEV_*`.

The LLM API key is read at runtime from the `dataspoke-llm-secret` Kubernetes Secret (rotated
online through `/api/v1/admin/conf`) — not an env var on the deployed app.

For full variable listings, the `.env.example` layout, and production Secret options, see
[`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md).

### Development Environment

Dev and prod deployment share one entry point: `helm-charts/bin/install.sh --profile {dev|prod}`.
The dev profile installs the umbrella chart with the `values-dev.yaml` overlay (reduced
resources, frontend disabled, in-cluster API with stub-mode RuntimeConfig flags seeded `true` via
the post-install seed step) plus the dev peripherals — nginx-ingress, DataHub, Langfuse, dummy
data, dev-lock — and seeds peripheral and runtime config via the admin API. The prod profile installs the umbrella chart only; DataHub and
Langfuse are operator-managed externally, and peripheral wiring goes through `/api/v1/admin/*`.
The API runs in-cluster in both profiles so Airflow callbacks reach it via
`http://dataspoke-api:8002`. Unit tests run locally without the cluster; see
[`TESTING.md §Testing Modes`](TESTING.md#testing-modes).

For the full install/uninstall/build/seed workflow, env-var listings, profile differences,
resource budget, and troubleshooting, see [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md).

---

## Repository Structure

The repository is organized by deployment concern and application layer. Key top-level directories:

| Directory | Purpose |
|-----------|---------|
| `src/` | Application source: `api/` (FastAPI), `backend/` (services), `shared/` (clients), `workflows/` (Airflow DAGs), `frontend/` (Next.js) |
| `spec/` | Architecture and feature specifications (one FRONTEND_*.md per function area in `feature/`) |
| `tests/` | Unit, integration, and E2E test suites |
| `helm-charts/` | Umbrella Helm chart + `bin/` install/uninstall/build scripts + dev peripherals |
| `docker-images/` | Dockerfiles per service (api, airflow, postgres) |
| `migrations/` | Alembic database migrations |
| `.claude/` | Developer AI Scaffold — Claude Code config (skills, agents, hooks) for building the product. See [`AI_SCAFFOLD.md`](AI_SCAFFOLD.md) |
| `plugin/` | End-User AI Scaffold — distributable Claude Code plugin for consuming a running DataSpoke via its public API. See [`AI_PLUGIN.md`](AI_PLUGIN.md) |

### AI scaffolds (two complementary deliverables)

The **Developer AI Scaffold** (the MANIFESTO §2.2 "AI Scaffold" — the `.claude/`
configuration, [`AI_SCAFFOLD.md`](AI_SCAFFOLD.md)) is complemented by a distinct
**End-User AI Scaffold** ([`AI_PLUGIN.md`](AI_PLUGIN.md)). They are sibling deliverables with no
shared privilege boundary:

| Scaffold | Lives in | Access | Audience |
|----------|----------|--------|----------|
| **Developer AI Scaffold** ([`AI_SCAFFOLD.md`](AI_SCAFFOLD.md)) | `.claude/` (in repo) | Full repo: specs, `src/`, helm, DB | Contributors building the product |
| **End-User AI Scaffold** ([`AI_PLUGIN.md`](AI_PLUGIN.md)) | `plugin/` (distributable plugin) | Public API surface only (`/api/v1/{auth,spoke,hub}`, `/redoc`) | Engineers operating a deployed instance |

The Developer scaffold *builds* DataSpoke; the End-User plugin *consumes* a deployed
DataSpoke.

---

## Design Decisions

### Technology Choices

| Decision | Chosen | Rationale | Alternative |
|----------|--------|-----------|-------------|
| API framework | FastAPI | Async, auto OpenAPI, Pydantic, high perf | Flask (simpler but no async), Django (too opinionated) |
| Frontend | Next.js | SSR, file-based routing, React ecosystem | CRA (no SSR), Vue (smaller ecosystem) |
| Orchestration | Airflow | Python DAG definitions, HttpOperator tasks, built-in UI, LocalExecutor | Temporal (heavier infra) |
| Operational DB | PostgreSQL 17 (pgvector + AGE) | Single-engine ACID storage for relational, vector, and graph workloads. Consolidates operational DB + vector DB + graph DB to reduce infra surface | Dedicated Qdrant (Rust perf, separate infra); Weaviate (multi-tenant); Pinecone (managed only); Neo4j (dedicated graph) |
| API documentation | FastAPI auto-generated OpenAPI + Pydantic schemas as SSOT | Always-in-sync docs; AI agents read route definitions directly | Standalone OpenAPI file (requires manual sync) |

### Architectural Choices

| Decision | Rationale |
|----------|-----------|
| DataHub as external dependency | Enterprises have existing installations; sidecar pattern enables independent lifecycle |
| Two-axis URI segmentation | Per-dataset cross-feature surface at `/spoke/common/data/{dataset_urn}/…`; cross-dataset list views + global features under one namespace per MANIFESTO §2.1 feature (`/spoke/ingestion`, `/spoke/validation`, `/spoke/ontogen/`, `/spoke/metagen/`, `/spoke/governance/`); `/hub/` for DataHub pass-through |
| Ontology Generation as a first-class feature | Metadata Generation (UC4) and Governance (UC5) both consume the node / triple graph; making Ontology Generation (UC3) a standalone feature avoids duplication and ensures consistency across consumers |
| Validation as a passive result store | Validation logic belongs in the data pipeline (right credentials, right scale, right environment). DataSpoke contributes a centralized schema-disciplined result store, a historical-result baseline cache, and DataHub assertion emission on the pipeline's behalf. Teams that need multiple distinct checks per dataset use DataHub's native assertion APIs directly. See [`spec/feature/VALIDATION.md`](feature/VALIDATION.md). |
| LLM as external service | Model-agnostic; swap providers without code changes; no GPU infrastructure required |
