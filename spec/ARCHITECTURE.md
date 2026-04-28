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
│         Portal: DE / DA / DG entry points     │
│         (baseline features + extensibility)   │
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
3. **Three-Tier API Routing** — Common features under `/spoke/common/`, group-specific extension
   routes under `/spoke/[de|da|dg]/`, DataHub pass-through under `/hub/`. The group tier is an
   extensibility affordance: baseline features live under `/spoke/common/` and `/spoke/dg/`;
   `/spoke/de/` and `/spoke/da/` are reserved for organization-specific extensions.
4. **API-First** — The FastAPI implementation in `src/api/` is the single source of truth for the
   API contract, with auto-generated OpenAPI docs enabling parallel frontend development and
   AI-agent iteration.
5. **Layer Separation** — Four components (UI, API, Backend/Pipeline, DataHub) are independently
   scalable and replaceable.
6. **Cloud-Native** — Kubernetes-ready with containerized deployments.
7. **Blended API/UI for Convenience** — DataSpoke may re-expose DataHub's basic functions through
   its own API and UI layer, combining DataHub-native and DataSpoke-specific metadata in a single
   call (see [DATAHUB_INTEGRATION §Key principles](DATAHUB_INTEGRATION.md#overview)).

---

## Core Principles

### 1. DataHub as Metadata SSOT

DataHub is the **mandatory backend** for metadata persistence. DataSpoke tries not to duplicate
metadata that DataHub already persists — it reads from and writes to DataHub, adding a
computational layer on top.

| Role | Responsibility |
|------|---------------|
| **DataHub** | Persist metadata aspects, emit change events, serve GraphQL queries |
| **DataSpoke** | Assertion-based validation, semantic search, ontology proposals, enrichment, metrics |

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

Portal-style interface with user-group-specific entry points (DE, DA, DG). Provides:
- Chart visualizations for metrics dashboards (DG) and data overviews
- Interactive graph rendering for taxonomy/ontology visualization
- Real-time updates via WebSocket for validation status and alerts
- Search interface for natural language queries (DA)

For layout, shared components, routing, and auth, see
[`spec/feature/FRONTEND_BASIC.md`](feature/FRONTEND_BASIC.md). Per-workspace specs:
[`FRONTEND_DE.md`](feature/FRONTEND_DE.md), [`FRONTEND_DA.md`](feature/FRONTEND_DA.md),
[`FRONTEND_DG.md`](feature/FRONTEND_DG.md).

### 2. DataSpoke API

**Technology**: FastAPI (Python 3.13)

Three-tier URI structure:

```
/api/v1/spoke/common/...   → Baseline features: ingestion, validation, ontogen, metagen
/api/v1/spoke/de/...       → Reserved for DE-exclusive extensions (no baseline routes)
/api/v1/spoke/da/...       → Reserved for DA-exclusive extensions (no baseline routes)
/api/v1/spoke/dg/...       → Governance (metric, overview)
/api/v1/hub/...            → DataHub pass-through (optional ingress for clients)
```

Supports RESTful CRUD and WebSocket channels for real-time streaming (alerts, validation progress).

For the complete route catalogue, JWT authentication model, middleware stack, and error
catalogue, see [`spec/API.md`](API.md).

### 3. DataSpoke Backend / Pipeline

**Technology**: Python 3.13, Airflow for orchestration

Core computational layer. For the full backend specification — layered architecture, shared
services, Airflow workflows, and infrastructure integration patterns — see
[`spec/feature/BACKEND.md`](feature/BACKEND.md). Data contracts (PostgreSQL schema including
pgvector tables) in [`spec/feature/BACKEND_SCHEMA.md`](feature/BACKEND_SCHEMA.md). Individual
feature designs are specified per feature in `spec/feature/spoke/`.

**Key capabilities by feature** (MANIFESTO §2.1):

| Feature | Capabilities |
|---------|-------------|
| Ingestion Control | Active and passive ingestion modes — DataSpoke either runs the extractor on a tier schedule or mirrors run history from externally-ingested datasets; single control surface for lifecycle management |
| Validation | DataHub assertion management (Open Assertions Spec + DataSpoke extensions), partition-aware rule execution, SQL-based timeseries validation, real-time Online Verifier for coding agents |
| Ontology Generation | Singleton-config + Markdown-seed LLM pipeline that emits a subject / predicate / object triple ontology — nodes (subjects/objects), edges (predicates), and triples (facts) — from DataHub aspects (canonical + UC4-approved editable variants) and DataHub Query entities (highlighted MANUAL + auto-discovered SYSTEM joins, capped per-dataset); persisted in PostgreSQL (pgvector + Apache AGE) and surfaced through the ontogen API with independent review queues per result type |
| Metadata Generation | Per-dataset proposals for documentation fields — table description, column descriptions, and dataProduct cross-data MDs (create/modify/split/retitle actions); review queue with field-level approve/edit/reject; approved writes go to editable DataHub aspects |
| Governance | Metric aggregation (pure aggregation over DataHub metadata + DataSpoke results), per-dataset breakdown, trend analysis, and a multi-perspective overview (metric values, blind spots, ontology graph, medallion layers, ownership topology) |

Source layout: `src/backend/` (feature services), `src/workflows/` (Airflow DAGs + helpers),
`src/shared/` (DataHub client, shared models, LLM integration).

### 4. DataHub (External)

DataHub is deployed and managed separately. DataSpoke interacts through three channels:

| Channel | Direction | Purpose |
|---------|-----------|---------|
| Python SDK (read) | DataHub → DataSpoke | Query metadata aspects, ingestion run history, assertion results, dataProduct entities |
| Python SDK (write) | DataSpoke → DataHub | Emit ingestion-extracted aspects, assertion definitions and `assertionRunEvent`, glossary term attachments (per approved ontology node) and glossary-term relationships (per approved ontology triple), editable description aspects (`editableDatasetProperties`, `editableSchemaMetadata`), `dataProductProperties` |
| Kafka events *(optional)* | DataHub → DataSpoke | Available for future event-driven extensions; not consumed by baseline UC1–UC5 flows |

For SDK entry points, aspect catalog, error handling, and configuration, see
[`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

### 5. Supporting Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Message Broker | Kafka | Event streaming (shared with DataHub) |
| Orchestration | Airflow | Workflow execution via Python DAGs and HttpOperator tasks (ingestion, validation, embedding sync, metrics collection) |
| Operational DB | PostgreSQL 17 (pgvector + AGE) | Ingestion configs, quality rules/results, health scores, ontology graph, user preferences, **vector embeddings** (pgvector), **graph queries** (Apache AGE, future use) |
| Cache | Redis | Validation result caching for AI agent loops, API response caching, rate limiting |
| LLM Provider | External API | Semantic analysis, ontology construction, documentation generation, code interpretation |

---

## Data Flow

### 1. Ingestion Control (UC1)

`attr/ingestion/conf` carries a `mode` flag:

- **Active** — DataSpoke is the ingestor. The configured Airflow tier DAG (`hourly` / `daily`
  / `weekly`) runs the platform extractor and emits aspects to DataHub via the SDK; manual and
  dry-run runs are also supported. Per-run records are written to `event/ingestion`.
- **Passive** — an external pipeline ingests directly into DataHub. DataSpoke does not run the
  extractor; the dataset's conf is marked `mode: passive`. An hourly
  `ingestion-passive-sync-hourly` DAG polls DataHub for ingestion run history of all
  passive-marked datasets and writes one row per run to `event/ingestion`, so the API surface is
  uniform across modes.

Other features (UC2, UC3, UC5) do not subscribe to ingestion events — each runs on its own
Airflow tier DAG schedule (or on demand) per the configs in their respective `attr/conf`.

### 2. Validation (UC2)

Validation runs are triggered by Airflow cron or by
`POST /api/v1/spoke/common/data/{dataset_urn}/method/validation/run` (with optional
`dry_run`). The service (1) resolves the target partition (manual → specified; cron → latest),
(2) computes metrics per rule, (3) executes source SQL for `custom` / `sql_timeseries` rules,
(4) validates against historical records when `ml_validation` is set, (5) registers
`assertionInfo` in DataHub, (6) reports results as `assertionRunEvent`. Output: per-rule results
`(partition, values, verdict, SUCCESS/FAILURE/ERROR)`. **Predictive SLA** uses a SQL-based
timeseries rule with ML validation to detect anomalies before breach. The same API serves
coding-agent loops as an **Online Verifier**.

### 3. Ontology Generation (UC3)

The Ontology Generator is governed by a singleton conf at `/api/v1/spoke/common/ontogen/attr/conf`
(`is_enabled`, `schedule_tier`, `dataset_filter`, `max_manual_queries_per_dataset`,
`max_system_queries_per_dataset`, `default_run_prompt`) and
zero-or-more **seeds** (human-authored Markdown documents at
`/api/v1/spoke/common/ontogen/attr/seed/{seed_id}`) that steer LLM naming and scoping. A
manual `POST /method/run` may also carry a transient Markdown body that acts as a
**one-shot prompt** for that single run on top of the persistent seeds (not stored);
runs without a body — periodic Airflow invocations and manual calls with an empty
body — fall back to `attr/conf.default_run_prompt`. The configured Airflow tier DAG reads
DataHub aspects (schema, descriptions, tags, lineage, usage) and DataHub Query
entities (highlighted MANUAL + auto-discovered SYSTEM joins, capped per dataset by
the conf) — the only input sources in the first implementation — together with the
active seeds (plus the one-shot prompt, if any), then runs an LLM-powered pipeline
that emits a
**subject / predicate / object triple ontology**: **nodes** (subjects / objects),
**edges** (predicates / relationship types), and **triples**
(`(subject_node, edge, object_node)` facts). Inference is **incremental**: each run
loads the existing approved nodes / edges / triples (with embedding-based reuse via
`node_embeddings`) as the working ontology and only proposes additions or refinements
on top — pending and rejected results are not carried forward. Outputs are persisted
in PostgreSQL with Apache AGE (graph) and pgvector (vector) extensions. Each result
type is reviewed independently —
`POST /api/v1/spoke/common/ontogen/result/{node|edge|triple}/{id}/method/review` — and a
triple may only be approved once its subject node, edge, and object node are all
approved (otherwise `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`). On approval, each node
becomes a glossary term attached to its member datasets, and each triple becomes a
glossary-term relationship between subject and object terms. Concurrent inference runs
return `409 ONTOGEN_RUNNING`.

### 4. Metadata Generation (UC4)

Per-dataset configs (`/api/v1/spoke/common/data/{urn}/attr/metagen/…`) drive the Metadata
Generation Service. It reads the UC3 ontology, schema, usage, lineage, and source-code
references and emits **proposals** (never direct writes) into a review queue. Reviewers approve,
partially approve (field subset), or reject via `PATCH …/attr/metagen/result/{result_id}` with
`{verdict, fields, reason}`; on approval, DataSpoke writes the approved subset to **editable**
DataHub aspects only — `editableDatasetProperties.description` for table descriptions and
`editableSchemaMetadata.editableSchemaFieldInfo[].description` for column descriptions. The
non-editable counterparts are reserved for ingestion connectors and are never overwritten by
DataSpoke. Cross-data MD proposals target `dataProduct` entities (`dataProductProperties`); a
single `cross_data.md` proposal carries a list of create / modify / split / retitle actions
that the reviewer approves individually. Future scope: proposals for `domains` and `globalTags`.

### 5. Governance (UC5)

Airflow per-tier periodic DAGs invoke the Metrics Aggregator — pure aggregation with no direct
data observation. It reads pre-existing DataHub metadata (e.g. `datasetProperties.description`,
`ownership`), DataSpoke validation results, and ingestion event history; applies the optional
`dataset_filter` (tags / glossary_terms / dataset_urns, OR-ed; unresolved URNs reported in `METRIC.RUN_COMPLETE.unresolved_urns`); and writes a single measured value +
per-dataset breakdown to `metric_results`. Surfaced via `/api/v1/spoke/dg/metric` (metric
dashboard) and `/api/v1/spoke/dg/overview` (ontology graph, medallion coverage, ownership
topology — all read-only aggregations).

---

## Feature-to-Architecture Mapping

Maps the five MANIFESTO features to the system components and infrastructure they require.
Baseline features live under `/spoke/common/` and `/spoke/dg/`; the `/spoke/de/` and `/spoke/da/`
route tiers are reserved for organization-specific extensions and have no baseline routes.

| Feature | UC | API Route | Backend Services | Infrastructure |
|---------|----|-----------|------------------|----------------|
| Ingestion Control | UC1 | `/spoke/common/ingestion/` (cross-dataset list), `/spoke/common/data/{urn}/{attr,method,event}/ingestion/` | Ingestion Service (active extractors + passive status sync), Source Adapter Framework | Airflow (tier-based periodic DAGs + hourly `ingestion-passive-sync-hourly`), Redis (concurrency guard), DataHub SDK, PostgreSQL |
| Validation | UC2 | `/spoke/common/validation/` (cross-dataset list), `/spoke/common/data/{urn}/{attr,method,event}/validation/` | Assertion Config Manager, Partition-Aware Executor, SQL Timeseries Engine, Online Verifier | Airflow (tier-based periodic DAGs), Redis (concurrency guard + dry-run cache), DataHub SDK, PostgreSQL |
| Ontology Generation | UC3 | `/spoke/common/ontogen/` (singleton conf + Markdown seeds + node / edge / triple browse + review) | LLM Classification, Relationship Inference, Triple Composition, Review Queue (node + edge + triple) | LLM API, PostgreSQL (pgvector + Apache AGE), Airflow (tier-based periodic DAG) |
| Metadata Generation | UC4 | `/spoke/common/metagen/` (cross-dataset list), `/spoke/common/data/{urn}/{attr,method,event}/metagen/` | Metadata Generation Service, Source-Code Analyzer, dataProduct Composer, Review Queue (field-level + action-level) | LLM API, PostgreSQL, DataHub SDK (read + approved writes to editable aspects only) |
| Governance | UC5 | `/spoke/dg/metric/`, `/spoke/dg/overview/` | Metrics Aggregator (pure aggregation, no source-DB reads), Per-Dataset Breakdown, Overview Composer (metric values, blind spots, ontology graph, medallion layers, ownership topology) | Airflow (tier-based periodic DAGs), PostgreSQL, DataHub GraphQL |

### Optional / future routes

| Surface | Notes |
|---------|-------|
| Redefined DataHub Functions *(TBD)* — `/spoke/common/data` (create/modify) | Blended API/UI that proxies DataHub reads/writes alongside DataSpoke-specific data; scope to be specified when planned |

### Cross-Cutting Infrastructure

| Concern | Infrastructure | Consumers |
|---------|----------------|-----------|
| Airflow DAGs | Airflow | Periodic active ingestion (UC1), passive ingestion status sync (UC1, hourly), validation (UC2), ontology re-inference (UC3), metadata generation (UC4), governance metrics (UC5) |
| PostgreSQL Operational Tables | PostgreSQL (pgvector + AGE) | Ingestion configs/runs, validation rules/results, ontology seeds + nodes + edges + triples + embeddings, metadata generation proposals + review state, governance metric results |
| Redis Caching | Redis | Validation dry-run cache (Online Verifier for AI coding loops), API response cache, rate limiting, JWT refresh-token revocation list |
| Kafka Event Consumers *(optional)* | Kafka (shared with DataHub) | Reserved for future event-driven cross-feature triggers; not used by baseline UC1–UC5 flows, which are schedule-driven via Airflow |

---

## Shared Services

Reusable backend services consumed by multiple features. These live in `src/shared/`.

### Ontology Generator

Own feature (UC3) that also serves UC4 (Metadata Generation) and UC5 (Governance) as consumers.

**Purpose**: LLM-powered service that builds and maintains a **subject / predicate / object
triple ontology** from DataHub metadata, source code, SQL logs, external docs, and
human-authored Markdown seeds.

**Processing pipeline**:
1. **Dataset → Node Inference** — LLM analyses schema, descriptions, tags, lineage, code
   references, and active seeds to propose nodes (subjects / objects) with member datasets
2. **Edge (Predicate) Inference** — semantic analysis proposes the relationship-type
   vocabulary used across the dataset estate
3. **Triple Composition** — pairwise reasoning proposes `(subject_node, edge, object_node)`
   facts referencing already-proposed nodes and edges
4. **Confidence Scoring & Human Review Queue** — each node, edge, and triple carries an
   independent confidence score and review status; review proceeds nodes → edges →
   triples via `POST /spoke/common/ontogen/result/{node|edge|triple}/{id}/method/review`

**Storage** (PostgreSQL with pgvector + Apache AGE):
- `ontogen_seeds` — id, markdown body, status (relational)
- `ontogen_nodes` — id, name, description, confidence, status (relational)
- `dataset_node_map` — dataset_urn, node_id, confidence_score, is_primary (relational)
- `ontogen_edges` — id, label, semantics, confidence, status (relational)
- `ontogen_triples` — id, subject_node_id, edge_id, object_node_id, confidence, status
  (relational; also materialised in AGE for graph traversal)
- `node_embeddings`, `dataset_embeddings` — pgvector tables for similarity recall

**Properties**: Re-inference on the configured `schedule_tier`; dry-run mode evaluates without
persisting; human-in-the-loop review at three layers (node, edge, triple) with a triple
gated on its endpoint nodes and edge being approved (`422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`
otherwise); on approval, each node becomes a glossary term attached to its member datasets
and each triple becomes a glossary-term relationship between subject and object terms.

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
| Orchestration | Airflow | Python DAG definitions, HttpOperator tasks calling internal activity endpoints, built-in scheduling and retry |
| Operational DB | PostgreSQL 17 (pgvector + Apache AGE) | ACID guarantees, JSONB flexibility, first-class vector similarity (pgvector), graph queries available (AGE) |
| Cache | Redis | API caching, rate limiting, session management |
| LLM Integration | External API (via LangChain) | Semantic analysis, ontology, documentation, code interpretation |
| Charts | Highcharts / Recharts | Rich visualization (metrics dashboards, graph views) |

### Development Stack

| Purpose | Technology |
|---------|-----------|
| Package Manager | uv |
| API Testing | pytest, httpx |
| Frontend Testing | Jest, React Testing Library |
| E2E Testing | Playwright |
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
containing: `dataspoke-frontend` + `dataspoke-api` (Deployments, ingress-exposed),
`dataspoke-event-consumer` (optional Kafka consumer Deployment — co-located in the API by
default), `dataspoke-airflow-api-server` + `dataspoke-airflow-scheduler` +
`dataspoke-airflow-triggerer` (Airflow 3.1 LocalExecutor; api-server replaces the former Flask
webserver), `postgresql` (StatefulSet with PV — custom image layering pgvector + Apache AGE on
PG 17), and `redis` (Deployment). External dependencies are `datahub-gms:8080` (GraphQL/REST)
and `datahub-kafka:9092` (event streaming).

`dataspoke-event-consumer` is optional — enable the separate pod for independent scaling in
production (Kafka consumers scale by partition count).

For replica counts, resource requests/limits, PV sizes, component matrix, and network policy,
see [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md). DataSpoke's namespace requires egress
access to the DataHub namespace; configure NetworkPolicy in clusters with default-deny.

### Configuration

All runtime configuration is driven by **environment variables** with two tiers:

| Prefix | Scope | Who reads it |
|--------|-------|-------------|
| `DATASPOKE_DEV_*` | Dev environment only | `dev_env/*.sh` scripts |
| `DATASPOKE_*` (no `DEV`) | Application runtime | DataSpoke app code (FastAPI, frontend) |

Dev-only variables (`DATASPOKE_DEV_*`) configure Kubernetes cluster settings, namespace names,
chart versions, and the nginx-ingress IP. The application code never reads them.

Application runtime variables (`DATASPOKE_*`) are the same names in dev and prod — only the
values differ. In dev, they point to the nginx-ingress external IP (TCP services) or ingress
hostnames (HTTP services). In production, they are injected via Helm values → Kubernetes
ConfigMap/Secret.

Application runtime variable groups: DataHub connection, PostgreSQL, Redis, Airflow, LLM API.
Dev-only variable groups: cluster & namespaces, chart versions, ingress IP and domain. For the
full variable listing with defaults, see
[`spec/feature/DEV_ENV.md` §Configuration](feature/DEV_ENV.md#configuration).

For production, secrets are stored as Kubernetes Secrets and injected via Helm values →
ConfigMap/Secret → container environment.
See [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md) for details.

### Development Environment

For dev/CI, DataHub and DataSpoke infrastructure dependencies are provisioned by `dev_env/`
scripts (see [`spec/feature/DEV_ENV.md`](feature/DEV_ENV.md) for install/uninstall,
configuration, and resource budget). The dev environment reuses the production umbrella Helm
chart (`helm-charts/dataspoke/`) with a `values-dev.yaml` overlay that reduces resources and
disables frontend/workers; the API runs in-cluster so Airflow callbacks reach it via
`http://dataspoke-api:8002`. Unit tests run locally without the cluster; see
[`TESTING.md §Testing Modes`](TESTING.md#testing-modes).

The bundled dev environment is **NOT** for production. For production Kubernetes deployment,
see [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md).

---

## Repository Structure

The repository is organized by deployment concern and application layer. Key top-level directories:

| Directory | Purpose |
|-----------|---------|
| `src/` | Application source: `api/` (FastAPI), `backend/` (services), `shared/` (clients), `workflows/` (Airflow DAGs), `frontend/` (Next.js) |
| `spec/` | Architecture and feature specifications (`feature/` for cross-cutting, `feature/spoke/` for user-group-specific) |
| `tests/` | Unit, integration, and E2E test suites |
| `dev_env/` | Kubernetes dev environment scripts |
| `helm-charts/` | Umbrella Helm chart for deployment |
| `docker-images/` | Dockerfiles per service |
| `migrations/` | Alembic database migrations |

---

## Design Decisions

### Technology Choices

| Decision | Chosen | Rationale | Alternative |
|----------|--------|-----------|-------------|
| API framework | FastAPI | Async, auto OpenAPI, Pydantic, high perf | Flask (simpler but no async), Django (too opinionated) |
| Frontend | Next.js | SSR, file-based routing, React ecosystem | CRA (no SSR), Vue (smaller ecosystem) |
| Orchestration | Airflow | Python DAG definitions, HttpOperator tasks, built-in UI, LocalExecutor | Temporal (heavier infra), Kestra (if YAML-first flows preferred) |
| Operational DB | PostgreSQL 17 (pgvector + AGE) | Single-engine ACID storage for relational, vector, and graph workloads. Consolidates operational DB + vector DB + graph DB to reduce infra surface | Dedicated Qdrant (Rust perf, separate infra); Weaviate (multi-tenant); Pinecone (managed only); Neo4j (dedicated graph) |
| API documentation | FastAPI auto-generated OpenAPI + Pydantic schemas as SSOT | Always-in-sync docs; AI agents read route definitions directly | Standalone OpenAPI file (requires manual sync) |

### Architectural Choices

| Decision | Rationale |
|----------|-----------|
| DataHub as external dependency | Enterprises have existing installations; sidecar pattern enables independent lifecycle |
| Three-tier URI segmentation | `/spoke/common/` for baseline shared features, `/spoke/dg/` for governance, `/spoke/[de\|da]/` reserved for organization-specific extensions, `/hub/` for DataHub pass-through — extensibility without forking the baseline |
| Ontology Generation as a first-class feature | Metadata Generation (UC4) and Governance (UC5) both consume the node / triple graph; making Ontology Generation (UC3) a standalone feature avoids duplication and ensures consistency across consumers |
| Validation as DataHub assertion layer | UC2 uses DataHub's native assertion framework; DataSpoke adds partition-aware execution, SQL-based timeseries rules, and a dry-run Online Verifier for coding agents rather than a bespoke scoring engine |
| LLM as external service | Model-agnostic; swap providers without code changes; no GPU infrastructure required |
| Redis for validation caching | AI agents in tight coding loops need sub-second validation responses |
