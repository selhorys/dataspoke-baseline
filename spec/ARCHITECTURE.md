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

DataSpoke is a **loosely coupled sidecar** to DataHub. DataHub is the Hub (metadata SSOT); each user-group-specific extension is a Spoke.

```
┌───────────────────────────────────────────────┐
│                 DataSpoke UI                  │
│         Portal: DE / DA / DG entry points     │
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

DataHub is deployed and managed **separately** — DataSpoke connects to it as an external dependency.

```
┌─────────────────────────┐         ┌─────────────────────────┐
│   DataSpoke Stack       │         │   DataHub Instance      │
│                         │  SDK    │   (External)            │
│   UI                    │◄───────►│   GMS                   │
│   API                   │  Kafka  │   Kafka                 │
│   Backend / Workers     │  GQL    │   Search (ES)           │
│   Qdrant / PostgreSQL   │         │   MySQL / Postgres      │
│   Temporal / Redis      │         │                         │
└─────────────────────────┘         └─────────────────────────┘
```

**Rationale**: DataSpoke is a sidecar extension, not a DataHub replacement. Enterprises have existing DataHub installations; loose coupling enables independent deployment and evolution.

### Key Architectural Tenets

1. **DataHub-backed SSOT** — DataHub stores metadata; DataSpoke extends without modifying core.
2. **Three-Tier API Routing** — Common features under `/spoke/common/`, user-group features under `/spoke/[de|da|dg]/`, DataHub pass-through under `/hub/`.
3. **API-First** — Standalone OpenAPI specs in `api/` enable parallel frontend/backend development and AI-agent iteration.
4. **Layer Separation** — Four components (UI, API, Backend/Pipeline, DataHub) are independently scalable and replaceable.
5. **Cloud-Native** — Kubernetes-ready with containerized deployments.
6. **Blended API/UI for Convenience** — DataSpoke may re-expose DataHub's basic functions through its own API and UI layer, combining DataHub-native and DataSpoke-specific metadata in a single call (see [DATAHUB_INTEGRATION §Key principles](DATAHUB_INTEGRATION.md#overview)).

---

## Core Principles

### 1. DataHub as Metadata SSOT

DataHub is the **mandatory backend** for metadata persistence. DataSpoke tries not to duplicate metadata that DataHub already persists — it reads from and writes to DataHub, adding a computational layer on top.

| Role | Responsibility |
|------|---------------|
| **DataHub** | Persist metadata aspects, emit change events, serve GraphQL queries |
| **DataSpoke** | Compute quality scores, anomaly detection, semantic search, ontology proposals, enrichment |

Integration channels (read, write, event) and their SDK patterns are defined in [`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

### 2. API Convention Compliance

All REST APIs conform to [`API_DESIGN_PRINCIPLE_en.md`](API_DESIGN_PRINCIPLE_en.md). The architecture enforces this through shared middleware for request/response formatting, content/metadata separation, and error handling.

### 3. API-First Design

API documentation exists as **standalone artifacts** in `api/` (OpenAPI 3.0 + markdown):
- AI agents iterate on API specs without running the backend.
- Frontend development starts before backend implementation.
- Contract testing and mocking without running services.

### 4. Layer Separation

| Benefit | Mechanism |
|---------|-----------|
| Independent scaling | UI, API, Workers scale separately |
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

For layout, shared components, routing, and auth, see [`spec/feature/FRONTEND_BASIC.md`](feature/FRONTEND_BASIC.md). Per-workspace specs: [`FRONTEND_DE.md`](feature/FRONTEND_DE.md), [`FRONTEND_DA.md`](feature/FRONTEND_DA.md), [`FRONTEND_DG.md`](feature/FRONTEND_DG.md).

```
src/frontend/
├── app/            # Next.js pages per user group (de, da, dg)
├── components/     # Reusable UI (charts, graphs, tables, search)
├── lib/            # API client, state management, hooks
└── styles/         # Global styles and themes
```

### 2. DataSpoke API

**Technology**: FastAPI (Python 3.13)

Three-tier URI structure:

```
/api/v1/spoke/common/...   → Cross-cutting features: ontology, ingestion, validation, generation, search
/api/v1/spoke/de/...       → Reserved for DE-exclusive features (no routes currently defined)
/api/v1/spoke/da/...       → Reserved for DA-exclusive features (no routes currently defined)
/api/v1/spoke/dg/...       → Data Governance: metrics dashboard, multi-perspective overview
/api/v1/hub/...            → DataHub pass-through (optional ingress for clients)
```

Supports RESTful CRUD and WebSocket channels for real-time streaming (alerts, validation progress).

For the complete route catalogue, JWT authentication model, middleware stack, error catalogue, and WebSocket channel specs, see [`spec/feature/API.md`](feature/API.md).

```
src/api/
├── routers/        # Routers per API tier (common, dg, hub, auth) + system health
├── schemas/        # Pydantic request/response models
├── middleware/      # Auth, logging, rate limiting, API convention enforcement
└── main.py         # FastAPI application entry
```

### 3. DataSpoke Backend / Pipeline

**Technology**: Python 3.13, Temporal for orchestration

Core computational layer. For the full backend specification — layered architecture, shared services, Temporal workflows, and infrastructure integration patterns — see [`spec/feature/BACKEND.md`](feature/BACKEND.md). Data contracts (PostgreSQL schema, Qdrant collections) in [`spec/feature/BACKEND_SCHEMA.md`](feature/BACKEND_SCHEMA.md). Individual feature designs are specified per feature in `spec/feature/spoke/`.

**Key capabilities by domain**:

| Domain | Capabilities |
|--------|-------------|
| Ingestion (DE) | Multi-source enrichment, custom extractors, PL/SQL lineage parsing |
| Validation (DE/DA) | Quality scoring, time-series anomaly detection (Prophet, Isolation Forest), SLA prediction |
| Documentation (DE) | LLM-powered semantic clustering, source code analysis, ontology proposals |
| Search (DA) | Embedding generation, vector similarity (Qdrant), NL query parsing |
| Text-to-SQL (DA) | Column profiling, join path recommendation, context window optimization |
| Metrics (DG) | Health score aggregation, department mapping, trend analysis |
| Visualization (DG) | Graph layout, medallion classification, blind spot detection |

```
src/backend/        # Feature service implementations
src/workflows/      # Temporal workflow definitions
src/shared/         # DataHub client wrappers, shared models, LLM integration
```

### 4. DataHub (External)

DataHub is deployed and managed separately. DataSpoke interacts through three channels:

| Channel | Direction | Purpose |
|---------|-----------|---------|
| Python SDK (read) | DataHub → DataSpoke | Query metadata aspects, timeseries profiles |
| Python SDK (write) | DataSpoke → DataHub | Persist enriched metadata, deprecation markers |
| Kafka events | DataHub → DataSpoke | React to metadata changes in real time |

For SDK entry points, aspect catalog, error handling, and configuration, see [`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

### 5. Supporting Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Vector DB | Qdrant | Semantic search, embedding storage, metadata similarity |
| Message Broker | Kafka | Event streaming (shared with DataHub) |
| Orchestration | Temporal | Durable workflow execution (ingestion, anomaly detection, embedding sync, metrics collection) |
| Operational DB | PostgreSQL | Ingestion configs, quality rules/results, health scores, ontology graph, user preferences |
| Cache | Redis | Validation result caching for AI agent loops, API response caching, rate limiting |
| LLM Provider | External API | Semantic analysis, ontology construction, documentation generation, code interpretation |

---

## Data Flow

### 1. Metadata Enrichment & Synchronization

Covers UC1 (Deep Ingestion) → event-driven downstream processing.

```
External Sources                    DataSpoke Backend               DataHub
(Confluence, Excel, APIs,   ──►    Ingestion Service        ──►   GMS
 GitHub repos, SQL logs)           (extract, transform,            (persist aspects,
                                    LLM-enrich, validate)          emit MCE/MAE)
                                                                      │
                                   Event Consumers           ◄──   Kafka
                                   ├─ Vector DB Sync (DA)
                                   ├─ Validator Trigger (DE)
                                   ├─ Metrics Update (DG)
                                   └─ Ontology Re-index
```

### 2. Online Validation

Covers UC2 (Pipeline Verification), UC3 (Predictive SLA). Shared across DE and DA groups.

```
AI Agent / User
      │
      ▼
API: /api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run
      │
      ▼
Validator Service
  1. Retrieve entity context (DataHub aspects + Qdrant vectors)
  2. Compute quality score (profile history, assertions, freshness)
  3. Detect anomalies (Prophet/Isolation Forest on timeseries)
  4. Traverse upstream lineage for root cause analysis
  5. Recommend alternatives from Qdrant similarity search
      │
      ▼
Validation Result (status, issues, recommendations, alternatives)
```

For **predictive SLA** (UC3), Temporal workflows run scheduled monitoring that uses the same scoring engine but adds threshold learning and pre-breach alerting.

### 3. Semantic Search & Text-to-SQL

Covers UC5 (NL Search), UC7 (Text-to-SQL Metadata).

```
User Query (natural language)
      │
      ▼
API: /api/v1/spoke/common/search  (add ?sql_context=true for Text-to-SQL context)
      │
      ▼
Search / Context Service
  1. Parse NL intent (entity type, filters, compliance context)
  2. Generate query embedding via LLM
  3. Hybrid search: Qdrant vectors + DataHub GraphQL filters
  4. Enrich results with metadata (tags, lineage, usage stats)
  5. [Text-to-SQL] Add column profiles, join paths, sample queries
      │
      ▼
Search Results / SQL Context (ranked, enriched, conversational)
```

### 4. Ontology & Documentation

Covers UC4 (Doc Generation), UC8 (Multi-Perspective Overview). Both use the shared Ontology/Taxonomy Builder (see [Shared Services](#shared-services)).

```
All Datasets (schema, descriptions, tags, lineage, code refs)
      │
      ▼
Ontology/Taxonomy Builder (LLM-powered)
  1. Classify datasets into business concept categories
  2. Detect semantic clusters and overlaps
  3. Propose canonical entities + consistency rules
  4. Weekly drift/violation scanning
      │
      ├──► UC4: Doc Generation (ontology proposals, similar-table diffs, code-based docs)
      └──► UC8: Multi-Perspective Overview (graph node grouping, domain classification)
```

### 5. Metrics Collection & Visualization

Covers UC6 (Metrics Dashboard), UC8 (Multi-Perspective Overview).

```
Temporal Scheduled Workflow (periodic)
      │
      ▼
Metrics Collector
  1. Enumerate all datasets (DataHub GraphQL)
  2. Compute health scores per dataset (description, ownership, tags, freshness)
  3. Aggregate by department (ownership → HR API mapping)
  4. Detect trends, decay rates, blind spots
  5. Persist to PostgreSQL time-series tables
      │
      ▼
API: /api/v1/spoke/dg/metric   (dashboard)
API: /api/v1/spoke/dg/overview  (graph visualization)
```

---

## Feature-to-Architecture Mapping

Maps MANIFESTO features to the system components and infrastructure they require. Detailed designs live in `spec/feature/spoke/` per feature.

### Data Engineering (DE)

DE features are served through `/spoke/common/` routes (dataset-centric operations are shared across all user groups that own a dataset). No `/spoke/de/` routes are currently defined.

| Feature | UC | API Route | Backend Services | Infrastructure |
|---------|----|-----------|--------------------|----------------|
| Deep Technical Spec Ingestion | UC1 | `/spoke/common/ingestion/`, `/spoke/common/data/{urn}/attr/ingestion/` | Ingestion Service, Custom Extractors, Field Mapping Engine | Temporal, DataHub SDK, Qdrant |
| Online Data Validator | UC2, UC3 | `/spoke/common/validation/`, `/spoke/common/data/{urn}/attr/validation/` | Quality Score Engine, Anomaly Detection, SLA Predictor | PostgreSQL, Redis, Prophet/IF |
| Automated Doc Generation | UC4 | `/spoke/common/gen/`, `/spoke/common/data/{urn}/attr/gen/` | Ontology Builder (shared), Source Code Analyzer, Consistency Engine | LLM API, Qdrant, PostgreSQL |

### Data Analysis (DA)

DA features are served through `/spoke/common/` routes. No `/spoke/da/` routes are currently defined.

| Feature | UC | API Route | Backend Services | Infrastructure |
|---------|----|-----------|--------------------|----------------|
| Natural Language Search | UC5 | `/spoke/common/search/` | NL Query Parser, Vector Search, PII Classifier | Qdrant, LLM API |
| Text-to-SQL Optimized Metadata | UC7 | `/spoke/common/search?sql_context=true` | Column Profiler, Join Path Recommender, Context Optimizer | Qdrant, DataHub GraphQL |
| Online Data Validator | UC2 | `/spoke/common/validation/` | (shared with DE) | (shared with DE) |

### Data Governance (DG)

| Feature | UC | API Route | Backend Services | Infrastructure |
|---------|----|-----------|--------------------|----------------|
| Enterprise Metrics Dashboard | UC6 | `/spoke/dg/metric/` | Health Score Aggregator, Department Mapper, Issue Tracker, Notification Engine | PostgreSQL, Temporal |
| Multi-Perspective Data Overview | UC8 | `/spoke/dg/overview/` | Ontology Builder (shared), Graph Layout Engine, Medallion Detector, Blind Spot Analyzer | Qdrant, LLM API, PostgreSQL |

### Common (cross-group)

| Feature | UC | API Route | Backend Services | Infrastructure |
|---------|----|-----------|--------------------|----------------|
| Ontology/Taxonomy Builder | UC4, UC8 | `/spoke/common/ontology/` | LLM Classification, Hierarchy Builder, Relationship Inference | LLM API, PostgreSQL, Qdrant |
| Quality Score Engine | UC2, UC3, UC6 | — (internal shared service; no dedicated API route) | Score Aggregator, Anomaly Detector | PostgreSQL, Redis |
| Redefined DataHub Functions *(TBD)* | — | `/spoke/common/data` (creation, modification) | Blended API/UI that proxies DataHub reads/writes alongside DataSpoke-specific data | DataHub SDK (read + write) |

### Cross-Cutting Infrastructure

| Concern | Infrastructure | Consumers |
|---------|---------------|-----------|
| Kafka Event Consumers | Kafka (shared with DataHub) | Vector DB sync (DA), validator triggers (DE), metrics update (DG), ontology re-index |
| Temporal Workflows | Temporal | Scheduled ingestion (DE), anomaly detection (DE), embedding maintenance (DA), metrics collection (DG) |
| PostgreSQL Operational Tables | PostgreSQL | Ingestion configs/runs, quality rules/results, health scores, ontology graph, user preferences |
| Redis Caching | Redis | Validation result cache (AI agent loops), API response cache, rate limiting |

---

## Shared Services

Reusable backend services consumed by multiple features. These live in `src/shared/`.

### Ontology/Taxonomy Builder

Shared by UC4 (Doc Generation) and UC8 (Multi-Perspective Overview).

**Purpose**: LLM-powered service that builds and maintains business concept taxonomies from metadata.

**Processing pipeline**:
1. **Dataset → Concept Classification** — LLM analyzes schema, descriptions, tags, lineage per dataset
2. **Concept Hierarchy Construction** — LLM synthesizes categories into a hierarchy
3. **Cross-Concept Relationship Inference** — pairwise semantic analysis for graph edges
4. **Confidence Scoring & Human Review Queue** — low-confidence results queued for governance

**Storage** (PostgreSQL):
- `concept_categories` — id, name, parent_id, description
- `dataset_concept_map` — dataset_urn, concept_id, confidence_score
- `concept_relationships` — concept_a, concept_b, relationship_type

**Properties**: Incremental updates on new ingestion; versioned taxonomy; human-in-the-loop for low-confidence (< 0.7) classifications.

### Quality Score Engine

Shared by UC2 (Pipeline Verification), UC3 (Predictive SLA), UC6 (Metrics Dashboard).

**Purpose**: Aggregate multiple DataHub aspects (profiles, assertions, ownership, documentation, freshness) into a single 0–100 quality score per dataset.

**Consumers**:
- Validator (DE/DA) — per-entity health assessment
- Metrics Dashboard (DG) — department-level aggregation
- Multi-Perspective Overview (DG) — graph node coloring

### DataHub Client Wrapper

Shared by all features.

**Purpose**: Thin wrapper around `acryl-datahub` SDK providing connection management, retry logic, and convenience methods. Patterns defined in [`DATAHUB_INTEGRATION.md`](DATAHUB_INTEGRATION.md).

---

## Technology Stack

### Runtime Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Frontend | Next.js + TypeScript | SSR, React ecosystem, type safety |
| API | FastAPI (Python 3.13) | Async support, auto OpenAPI docs, Pydantic validation |
| Backend | Python 3.13 | Rich data/ML libraries, DataHub SDK compatibility |
| Vector DB | Qdrant | Self-hostable, Rust-based performance, simple deployment |
| Message Broker | Kafka | DataHub integration standard |
| Orchestration | Temporal | Durable workflows, built-in retry, workflow-as-code testing |
| Operational DB | PostgreSQL | ACID guarantees, JSONB flexibility |
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

For full testing conventions, toolchain configuration, mocking rules, and the integration test lock protocol, see [`spec/TESTING.md`](TESTING.md).

---

## Deployment Architecture

### Kubernetes Topology

DataHub exists in a separate namespace or cluster. DataSpoke deploys into its own namespace.

```
Namespace: dataspoke
├── dataspoke-frontend         (Deployment)    Ingress: /app/*
├── dataspoke-api              (Deployment)    Ingress: /api/*
├── dataspoke-workers          (Deployment)    — no ingress (Temporal activities)
├── dataspoke-event-consumer   (Deployment)    — no ingress (Kafka consumer) [optional]
├── temporal-server            (Deployment)
├── qdrant                     (StatefulSet, PV)
├── postgresql                 (StatefulSet, PV)
└── redis                      (Deployment)

External Dependencies:
  datahub-gms:8080    (GraphQL / REST)
  datahub-kafka:9092  (Event streaming)
```

`dataspoke-event-consumer` is optional — by default Kafka consumers are co-located in `dataspoke-workers`. Enable the separate pod for independent scaling in production (Kafka consumers scale by partition count; Temporal workers by activity throughput). See [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md).

Replica counts, resource requests/limits, and PV sizes are configurable via Helm values. The table below shows **minimum requirements** for a functional single-node deployment:

| Component | Min Replicas | Min Memory | Min CPU | Min PV |
|-----------|-------------|-----------|---------|--------|
| dataspoke-frontend | 1 | 256Mi | 0.25 | — |
| dataspoke-api | 1 | 512Mi | 0.5 | — |
| dataspoke-workers | 1 | 1Gi | 0.5 | — |
| dataspoke-event-consumer† | 1 | 512Mi | 0.25 | — |
| temporal-server | 1 | 1Gi | 0.5 | — |
| qdrant | 1 | 1Gi | 0.5 | 10Gi |
| postgresql | 1 | 512Mi | 0.5 | 10Gi |
| redis | 1 | 256Mi | 0.25 | — |

† Optional — disabled by default; enable via `event-consumer.enabled`.

**Network**: DataSpoke namespace requires access to DataHub namespace. Configure NetworkPolicy if using strict policies.

### Configuration

All runtime configuration is driven by **environment variables** with two tiers:

| Prefix | Scope | Who reads it |
|--------|-------|-------------|
| `DATASPOKE_DEV_*` | Dev environment only | `dev_env/*.sh` scripts |
| `DATASPOKE_*` (no `DEV`) | Application runtime | DataSpoke app code (FastAPI, workers, frontend) |

Dev-only variables (`DATASPOKE_DEV_*`) configure Kubernetes cluster settings, namespace names, chart versions, and port-forward ports. The application code never reads them.

Application runtime variables (`DATASPOKE_*`) are the same names in dev and prod — only the values differ. In dev, they point to `localhost` (port-forwarded from k8s). In production, they are injected via Helm values → Kubernetes ConfigMap/Secret.

**Application runtime variable groups**:

| Group | Variables | Purpose |
|-------|-----------|---------|
| DataHub connection | `DATASPOKE_DATAHUB_GMS_URL`, `DATASPOKE_DATAHUB_TOKEN`, `DATASPOKE_DATAHUB_KAFKA_BROKERS` | GMS endpoint for SDK read/write, personal access token (empty in dev), Kafka brokers for MCE/MAE events |
| PostgreSQL | `DATASPOKE_POSTGRES_HOST`, `DATASPOKE_POSTGRES_PORT`, `DATASPOKE_POSTGRES_USER`, `DATASPOKE_POSTGRES_PASSWORD`, `DATASPOKE_POSTGRES_DB` | Operational DB for ingestion configs, quality results, health scores, ontology graph |
| Redis | `DATASPOKE_REDIS_HOST`, `DATASPOKE_REDIS_PORT`, `DATASPOKE_REDIS_PASSWORD` | Cache for validation results, API responses, rate limiting |
| Qdrant | `DATASPOKE_QDRANT_HOST`, `DATASPOKE_QDRANT_HTTP_PORT`, `DATASPOKE_QDRANT_GRPC_PORT`, `DATASPOKE_QDRANT_API_KEY` | Vector DB for semantic search, embedding storage |
| Temporal | `DATASPOKE_TEMPORAL_HOST`, `DATASPOKE_TEMPORAL_PORT`, `DATASPOKE_TEMPORAL_NAMESPACE` | Workflow orchestration |
| LLM API | `DATASPOKE_LLM_PROVIDER`, `DATASPOKE_LLM_API_KEY`, `DATASPOKE_LLM_MODEL` | LLM integration (e.g. Gemini, OpenAI, Anthropic) for ontology, doc generation, semantic analysis |

**Dev-only variable groups** (examples):

| Group | Variables | Purpose |
|-------|-----------|---------|
| Cluster & namespaces | `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` | Cluster context and namespace targeting |
| Chart versions | `DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION`, `DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION` | Helm chart version pins |
| Port-forward | `DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_*` (UI, GMS, Kafka), `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_*` | Port mappings |

For production, secrets (`DATASPOKE_LLM_API_KEY`, `DATASPOKE_POSTGRES_PASSWORD`, `DATASPOKE_REDIS_PASSWORD`, etc.) are stored as Kubernetes Secrets and referenced by deployments. Configuration flows through the umbrella Helm chart: Helm values → ConfigMap/Secret → container environment. See [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md) for details.

For the full variable listing with defaults, see [`spec/feature/DEV_ENV.md` §Configuration](feature/DEV_ENV.md#configuration).

### Development Environment

For dev/CI, DataHub and DataSpoke infrastructure dependencies are provisioned via `dev_env/`:
```bash
cd dev_env && ./install.sh    # Install DataHub + DataSpoke infra + examples (5–10 min first run)
cd dev_env && ./uninstall.sh  # Tear down
# Settings: dev_env/.env (DATASPOKE_DEV_* for cluster config, DATASPOKE_* for app config)
```

The dev environment installs only **infrastructure dependencies** (PostgreSQL, Redis, Qdrant, Temporal) into the cluster. DataSpoke application services (frontend, API, workers) run on the developer's host, connecting to port-forwarded infrastructure services. See [`spec/feature/DEV_ENV.md`](feature/DEV_ENV.md).

The bundled dev environment is **NOT** for production. For production Kubernetes deployment, see [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md).

---

## Repository Structure

```
dataspoke-baseline/
├── api/                # Standalone OpenAPI 3.0 specs (API-first)
├── dev_env/            # Kubernetes dev environment
├── helm-charts/        # Kubernetes deployment manifests
├── docker-images/      # Dockerfiles for each service (multi-stage builds)
├── spec/               # Architecture and feature specifications
│   ├── feature/        # Cross-cutting feature specs (API, BACKEND, BACKEND_SCHEMA, FRONTEND_*, DEV_ENV, HELM_CHART)
│   └── feature/spoke/  # User-group-specific feature specs (DE/DA/DG)
├── src/
│   ├── frontend/       # Next.js (pages per user group: de, da, dg)
│   ├── api/            # FastAPI (routers per user group, schemas, middleware)
│   ├── backend/        # Feature service implementations
│   ├── workflows/      # Temporal workflow definitions
│   └── shared/         # DataHub client, shared models, LLM integration
├── tests/              # Unit, integration, E2E test suites
├── ref/                # External source for AI reference (git-ignored)
├── migrations/         # Alembic database migrations
├── pyproject.toml      # Python project metadata and dependencies (uv)
├── uv.lock             # Locked dependency versions (committed)
└── .venv/              # Python virtual environment (git-ignored, created by uv sync)
```

---

## Design Decisions

### Technology Choices

| Decision | Chosen | Rationale | Alternative |
|----------|--------|-----------|-------------|
| API framework | FastAPI | Async, auto OpenAPI, Pydantic, high perf | Flask (simpler but no async), Django (too opinionated) |
| Frontend | Next.js | SSR, file-based routing, React ecosystem | CRA (no SSR), Vue (smaller ecosystem) |
| Vector DB | Qdrant | Self-hostable, Rust perf, simple binary | Weaviate (if multi-tenancy or GraphQL needed), Pinecone (managed only) |
| Orchestration | Temporal | Durable workflows, workflow-as-code testing | Airflow (if existing infra or batch DAGs required) |
| Operational DB | PostgreSQL | ACID, JSONB, mature ecosystem | MongoDB (no ACID for critical operational data) |
| API documentation | Standalone OpenAPI in `api/` | AI agents iterate without backend; contract testing | Inline docs only (blocks parallel development) |

### Architectural Choices

| Decision | Rationale |
|----------|-----------|
| DataHub as external dependency | Enterprises have existing installations; sidecar pattern enables independent lifecycle |
| Three-tier URI segmentation | `/spoke/common/` for shared features, `/spoke/[de|da|dg]/` for user-group features, `/hub/` for DataHub pass-through — clear ownership with a home for cross-cutting capabilities |
| Shared Ontology Builder | UC4 and UC8 both need dataset-to-concept mapping; avoids duplication, ensures consistency |
| Shared Quality Score Engine | UC2, UC3, UC6 all need composite health scores; single algorithm, multiple consumers |
| LLM as external service | Model-agnostic; swap providers without code changes; no GPU infrastructure required |
| Redis for validation caching | AI agents in tight coding loops need sub-second validation responses |
