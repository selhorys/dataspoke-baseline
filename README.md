# DataSpoke

AI-powered sidecar extension for [DataHub](https://datahubproject.io/) — organized by user group for Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG).

DataSpoke is a **loosely coupled sidecar** to DataHub. DataHub stores metadata (the Hub); DataSpoke extends it with quality scoring, semantic search, ontology construction, and metrics dashboards (the Spokes).

This repository delivers two artifacts:

- **Baseline Product** — A pre-built implementation of essential features for an AI-era catalog, targeting DE, DA, and DG user groups.
- **AI Scaffold** — Claude Code conventions, development specs, and utilities that enable rapid construction of custom data catalogs with AI coding agents.

## Architecture

```
┌───────────────────────────────────────────────┐
│                 DataSpoke UI                  │
│         Portal: DE / DA / DG entry points     │
└───────────────────────┬───────────────────────┘
                        │
┌───────────────────────▼───────────────────────┐
│                DataSpoke API                  │
│   /spoke/common/  /spoke/de|da|dg/  /hub/     │
└───────────┬───────────────────────┬───────────┘
            │                       │
┌───────────▼───────────┐ ┌─────────▼───────────┐
│       DataHub         │ │      DataSpoke      │
│    (metadata SSOT)    │ │  Backend / Workers  │
│                       │ │  + Infrastructure   │
└───────────────────────┘ └─────────────────────┘
```

DataHub is deployed and managed **separately** — DataSpoke connects to it as an external dependency.

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js + TypeScript |
| API | FastAPI (Python 3.11+) |
| Orchestration | Temporal |
| Vector DB | Qdrant |
| Operational DB | PostgreSQL |
| Cache | Redis |
| DataHub integration | `acryl-datahub` Python SDK + Kafka |
| LLM integration | External API via LangChain |

## Features

### Data Engineering (DE)

- **Deep Technical Spec Ingestion** — Collects platform-specific metadata (storage formats, Kafka replication, PL/SQL lineage) from Confluence, Excel, GitHub, and SQL logs.
- **Online Data Validator** — Time-series quality scoring, anomaly detection (Prophet / Isolation Forest), SLA prediction, and dry-run validation without writing to the store.
- **Automated Documentation Generation** — Generates docs from source code references, highlights differences between similar tables, and proposes enterprise-wide ontology standards.

### Data Analysis (DA)

- **Natural Language Search** — Explore datasets using natural language queries; hybrid Qdrant vector + DataHub GraphQL search.
- **Text-to-SQL Optimized Metadata** — Curated metadata (column profiles, join paths, sample queries) focused on enabling accurate SQL generation by AI tools.
- **Online Data Validator** — Same as the DE group; shared across user groups.

### Data Governance (DG)

- **Enterprise Metrics Dashboard** — Time-series monitoring of dataset counts, availability ratios, health scores, and trends aggregated by department.
- **Multi-Perspective Data Overview** — Taxonomy/ontology graph visualization with medallion-architecture classification and blind-spot detection.

## Getting Started

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A local Kubernetes cluster (Docker Desktop, minikube, or kind) with **8+ CPUs / 16 GB RAM**
- **Python 3.11+** and **Node.js 18+** for running app services locally

### 1. Configure and Install the Dev Environment

The dev environment provisions **infrastructure dependencies** (DataHub, PostgreSQL, Redis, Qdrant, Temporal, example data sources) into a local Kubernetes cluster. DataSpoke application services run on the host.

```bash
# Copy and edit config (set DATASPOKE_DEV_KUBE_CLUSTER to your cluster context name)
cp dev_env/.env.example dev_env/.env

# Install everything (~5–10 min first run)
cd dev_env && ./install.sh
```

> Using Claude Code? Run `/dataspoke-dev-env-install` for guided end-to-end setup.

### 2. Start Port-Forwarding

```bash
dev_env/datahub-port-forward.sh      # DataHub UI + GMS
dev_env/dataspoke-port-forward.sh    # DataSpoke infrastructure
```

| Service | URL / Address | Credentials |
|---------|--------------|-------------|
| DataHub UI | http://localhost:9002 | `datahub` / `datahub` |
| DataHub GMS | http://localhost:9004 | — |
| PostgreSQL | localhost:9201 | per `dev_env/.env` |
| Redis | localhost:9202 | per `dev_env/.env` |
| Qdrant | localhost:9203 (HTTP), :9204 (gRPC) | — |
| Temporal | localhost:9205 | — |

### 3. Run DataSpoke App Services

```bash
source dev_env/.env

# Frontend
cd src/frontend && npm run dev          # http://localhost:3000

# API
cd src/api && uvicorn main:app --reload --port 8000

# Workers
cd src/workflows && python -m worker
```

### 4. Verify

```bash
source dev_env/.env
kubectl get pods -n $DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
kubectl get pods -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE
kubectl get pods -n $DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE
```

### Uninstall

```bash
cd dev_env && ./uninstall.sh
# Or: /dataspoke-dev-env-uninstall
```

## AI Scaffold

The scaffold lives in `.claude/` and makes AI-assisted development immediately productive from the first session. See [`spec/AI_SCAFFOLD.md`](spec/AI_SCAFFOLD.md) for the full reference.

### Skills (auto-loaded context)

| Skill | Purpose |
|-------|---------|
| `kubectl` | kubectl/helm operations against the local cluster |
| `monitor-k8s` | Full cluster health report (pods, events, Helm releases) |
| `plan-doc` | Route spec authorship to the correct tier in `spec/` |
| `datahub-api` | DataHub data model Q&A and Python SDK code writing |
| `prauto-check-status` | Prauto issue/PR status dashboard and next-heartbeat prediction |
| `prauto-run-heartbeat` | Monitored test-run of `.prauto/heartbeat.sh`; diagnoses and fixes errors |

### Commands (multi-step workflows)

| Command | Purpose |
|---------|---------|
| `/dataspoke-dev-env-install` | End-to-end dev environment setup |
| `/dataspoke-dev-env-uninstall` | Controlled environment teardown |
| `/dataspoke-ref-setup-all` | Download AI reference materials (`ref/`) |
| `/dataspoke-plan-write` | Guided spec authoring (scope → Q&A → write) |

### Subagents (specialized implementers)

| Subagent | Scope |
|----------|-------|
| `api-spec` | OpenAPI 3.0 specs in `api/` |
| `backend` | FastAPI/Python in `src/api/`, `src/backend/`, `src/workflows/`, `src/shared/` |
| `frontend` | Next.js/TypeScript in `src/frontend/` |
| `k8s-helm` | Helm charts, Dockerfiles, Kubernetes manifests |

### Building a Custom Spoke

Fork this repository and adapt:

1. Revise `spec/MANIFESTO_*.md` — redefine user groups, features, and product identity
2. Run `/dataspoke-plan-write` — update architecture and author feature specs
3. Run `/dataspoke-dev-env-install` — bring up the local environment
4. Use `api-spec` → `backend` → `frontend` → `k8s-helm` subagents in sequence

## Repository Structure

```
dataspoke-baseline/
├── api/                    # Standalone OpenAPI 3.0 specs (API-first)
├── dev_env/                # Local Kubernetes dev environment
│   ├── .env                # All settings (gitignored)
│   ├── install.sh / uninstall.sh
│   ├── datahub/            # DataHub Helm install
│   ├── dataspoke-infra/    # DataSpoke infrastructure (PG, Redis, Qdrant, Temporal)
│   └── dataspoke-example/  # Example data sources (PG, Kafka)
├── helm-charts/dataspoke/  # Umbrella Helm chart for production deployment
├── spec/                   # Architecture and feature specifications
│   ├── MANIFESTO_en.md     # Product identity (highest authority)
│   ├── ARCHITECTURE.md     # System architecture, tech stack, feature mapping
│   ├── AI_SCAFFOLD.md      # Claude Code scaffold conventions
│   ├── feature/            # Common feature specs (API, DEV_ENV, HELM_CHART)
│   └── feature/spoke/      # User-group-specific feature specs (DE/DA/DG)
├── src/
│   ├── frontend/           # Next.js app (pages per user group: de, da, dg)
│   ├── api/                # FastAPI routers, schemas, middleware
│   ├── backend/            # Feature service implementations
│   ├── workflows/          # Temporal workflow definitions
│   └── shared/             # DataHub client, shared models, LLM integration
├── docker-images/          # Dockerfiles (multi-stage builds per service)
├── tests/                  # Unit, integration, and E2E test suites
├── migrations/             # Alembic database migrations
└── ref/                    # External source for AI reference (gitignored)
```

## Environment Variables

Two-tier naming convention:

| Prefix | Scope | Who reads it |
|--------|-------|-------------|
| `DATASPOKE_DEV_*` | Dev environment only | `dev_env/*.sh` scripts |
| `DATASPOKE_*` (no `DEV`) | Application runtime | DataSpoke app code |

Key application variables:

| Variable | Purpose |
|----------|---------|
| `DATASPOKE_DATAHUB_GMS_URL` | DataHub GMS endpoint |
| `DATASPOKE_DATAHUB_KAFKA_BROKERS` | Kafka brokers for event streaming |
| `DATASPOKE_POSTGRES_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_DB` | Operational database |
| `DATASPOKE_REDIS_HOST` / `_PORT` / `_PASSWORD` | Cache |
| `DATASPOKE_QDRANT_HOST` / `_HTTP_PORT` / `_GRPC_PORT` | Vector database |
| `DATASPOKE_TEMPORAL_HOST` / `_PORT` / `_NAMESPACE` | Workflow orchestration |
| `DATASPOKE_LLM_PROVIDER` / `_API_KEY` / `_MODEL` | LLM integration |

See [`spec/feature/DEV_ENV.md`](spec/feature/DEV_ENV.md) for the full variable listing.

## Documentation

| Document | Purpose |
|----------|---------|
| [spec/MANIFESTO_en.md](spec/MANIFESTO_en.md) | Product identity, user-group taxonomy |
| [spec/ARCHITECTURE.md](spec/ARCHITECTURE.md) | System architecture, tech stack, deployment |
| [spec/AI_SCAFFOLD.md](spec/AI_SCAFFOLD.md) | Claude Code scaffold conventions and utilities |
| [spec/USE_CASE_en.md](spec/USE_CASE_en.md) | Conceptual scenarios by user group (UC1–UC8) |
| [spec/DATAHUB_INTEGRATION.md](spec/DATAHUB_INTEGRATION.md) | DataHub SDK/API patterns |
| [spec/API_DESIGN_PRINCIPLE_en.md](spec/API_DESIGN_PRINCIPLE_en.md) | REST API conventions |
| [spec/feature/API.md](spec/feature/API.md) | API layer design: routes, auth, middleware, error codes |
| [spec/feature/DEV_ENV.md](spec/feature/DEV_ENV.md) | Dev environment specification |
| [spec/feature/HELM_CHART.md](spec/feature/HELM_CHART.md) | Helm chart specification for production deployment |
| [spec/TESTING.md](spec/TESTING.md) | Testing conventions: toolchain, unit/integration/E2E workflow, dev-env lock protocol |

## License

[Apache License 2.0](LICENSE)
