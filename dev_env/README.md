# DataSpoke Development Environment

A fully scripted Kubernetes-based environment for developing and testing DataSpoke. Three namespaces are provisioned: `datahub-01` (DataHub), `dataspoke-01` (infrastructure), and `dataspoke-dummy-data-01` (example data sources).

The cluster hosts only **infrastructure dependencies**. DataSpoke application services (frontend, API, workers) run on your host machine, connecting to port-forwarded infrastructure.

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- A Kubernetes cluster (e.g. Docker Desktop, minikube, kind, or a remote cluster) with **8+ CPUs / 16 GB RAM**

## Quick Start

### 0. If you use Claude Code

Just run skill: `/dev-env install`

### 1. Configure your cluster

Copy the example and edit to match your cluster:

```bash
cp .env.example .env
```

Then edit `.env`:

```bash
# Set your Kubernetes context
DATASPOKE_DEV_KUBE_CLUSTER=minikube
```

To list available contexts:

```bash
kubectl config get-contexts
```

### 2. Install everything

From the `dev_env/` directory:

```bash
chmod +x install.sh uninstall.sh \
  datahub/install.sh datahub/uninstall.sh \
  dataspoke-infra/install.sh dataspoke-infra/uninstall.sh \
  dataspoke-example/install.sh dataspoke-example/uninstall.sh \
  dataspoke-lock/install.sh dataspoke-lock/uninstall.sh \
  datahub-port-forward.sh dataspoke-port-forward.sh \
  dummy-data-port-forward.sh lock-port-forward.sh

./install.sh
```

This takes approximately 5-10 minutes on the first run while container images are pulled.

### 3. Access DataHub (UI + GMS API)

```bash
./datahub-port-forward.sh          # start both forwards in background
./datahub-port-forward.sh --stop   # stop both and clean up PIDs
```

| Endpoint | Forwarded URL | Purpose |
|----------|---------------|---------|
| DataHub UI | http://localhost:9002 | Web UI, GraphiQL |
| DataHub GMS | http://localhost:9004 | REST API, Swagger UI, SDK target |

Credentials: `datahub` / `datahub`

### 4. Access DataSpoke infrastructure

```bash
./dataspoke-port-forward.sh        # start all infra forwards in background
./dataspoke-port-forward.sh --stop # stop all and clean up PIDs
```

| Service | Forwarded Address | Purpose |
|---------|-------------------|---------|
| PostgreSQL | localhost:9201 | DataSpoke operational DB |
| Redis | localhost:9202 | Cache, rate limiting |
| Qdrant HTTP | localhost:9203 | Vector DB REST API |
| Qdrant gRPC | localhost:9204 | Vector DB gRPC API |
| Temporal | localhost:9205 | Workflow orchestration |
| Lock API | localhost:9221 | Dev-env mutex (see §5) |

### 5. Lock the dev environment (multi-tester coordination)

When multiple testers share a single machine, use the lock service to coordinate exclusive access before running destructive operations (data resets, schema migrations, ingestion tests, etc.).

```bash
./lock-port-forward.sh          # start lock API forward in background
./lock-port-forward.sh --stop   # stop it
```

| Endpoint | Forwarded URL | Purpose |
|----------|---------------|---------|
| Lock API | http://localhost:9221 | Dev-env mutex REST API |

**API reference:**

```bash
# Check current lock status
curl http://localhost:9221/lock

# Acquire the lock (required fields: owner)
curl -s -X POST http://localhost:9221/lock/acquire \
  -H "Content-Type: application/json" \
  -d '{"owner": "alice", "message": "running ingestion test"}'

# Release the lock (owner must match)
curl -s -X POST http://localhost:9221/lock/release \
  -H "Content-Type: application/json" \
  -d '{"owner": "alice"}'

# Force-release (admin use — no owner check)
curl -s -X DELETE http://localhost:9221/lock
```

**Response codes:**
- `200` — success (acquired, released, or already unlocked)
- `400` — missing `owner` field
- `403` — release attempted by non-owner
- `409` — lock already held by another tester

**Notes:**
- Lock state is in-memory; it resets to unlocked if the pod restarts.
- The lock is advisory — it does not block access to infra services directly.
  Testers are expected to check the lock before starting sensitive operations.
- If the previous holder's session crashed, use force-release (`DELETE /lock`).

### 6. Run DataSpoke application services

Load environment variables and start services on the host:

```bash
source .env

# Install Python dependencies (from repo root)
cd .. && uv sync

# Frontend
cd src/frontend && npm run dev          # http://localhost:3000

# API (from repo root)
uv run uvicorn src.api.main:app --reload --port 8000

# Workers (from repo root)
uv run python -m src.workflows.worker
```

The `DATASPOKE_*` variables in `.env` point to `localhost` — the port-forwards connect them to the in-cluster infrastructure transparently.

### 7. Access example data sources

Forward example PostgreSQL and Kafka:

```bash
./dummy-data-port-forward.sh        # start both forwards in background
./dummy-data-port-forward.sh --stop # stop and clean up PIDs
```

| Service | Forwarded Address | Credentials |
|---------|-------------------|-------------|
| PostgreSQL | localhost:9102 | `postgres` / `ExampleDev2024!` (database: `example_db`) |
| Kafka | localhost:9104 | — |

### 8. Populate dummy data and register in DataHub

Populate `example-postgres` and `example-kafka` with realistic Imazon use-case data and register tables in DataHub:

```bash
# From repo root (requires port-forwards for 9102, 9104, 9004):
uv run python -m tests.integration.util --reset-all
```

This is **idempotent** — every run drops all custom schemas CASCADE and recreates them, deletes+recreates Kafka topics, and re-registers all DataHub datasets. Safe to re-run at any time.

**What gets created:**

- **PostgreSQL**: 11 schemas, 17 tables, ~600 rows covering UC1-UC7 scenarios (catalog, orders, customers, reviews, publishers, shipping, inventory, marketing, eBookNow products/content/storefront)
- **Kafka**: 3 topics (`imazon.orders.events`, `imazon.shipping.updates`, `imazon.reviews.new`) with ~45 JSON messages
- **DataHub**: 17 dataset entities with `DatasetProperties` + `SchemaMetadata` aspects (137 columns total)

**Verify:**

```bash
# Connect via port-forward (start dummy-data-port-forward.sh first)
psql -h localhost -p 9102 -U postgres -d example_db

\dt catalog.*                                              -- list catalog tables
SELECT count(*) FROM catalog.title_master;                 -- expect 30
SELECT count(*) FROM reviews.user_ratings_legacy
  WHERE rating_score IS NULL;                              -- expect 15 (~30% null rate)
```

See `spec/feature/DEV_ENV.md §Dummy Data` for full schema details and data design choices.

## Verify Installation

```bash
source .env
kubectl get pods -n $DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
kubectl get pods -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE
kubectl get pods -n $DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE
helm list -n $DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
helm list -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE
```

## Uninstall

```bash
./uninstall.sh
```

You will be prompted before any destructive operation.

Claude Code skill: `/dev-env uninstall`

## Namespace Architecture

| Namespace | Purpose | Managed By |
|-----------|---------|------------|
| `datahub-01` | DataHub platform + all backing services | `datahub/install.sh` via Helm |
| `dataspoke-01` | DataSpoke infrastructure (PostgreSQL, Redis, Qdrant, Temporal) + lock service | `dataspoke-infra/install.sh` via Helm; `dataspoke-lock/install.sh` via kubectl |
| `dataspoke-dummy-data-01` | Example PostgreSQL + Kafka for ingestion testing | `dataspoke-example/install.sh` via kubectl |

## Directory Structure

```
dev_env/
├── .env                          # All settings (gitignored, copy from .env.example)
├── install.sh / uninstall.sh     # Top-level orchestrators
├── lib/helpers.sh                # Shared shell functions: info(), warn(), error()
├── datahub-port-forward.sh       # Port-forward DataHub UI + GMS + Kafka
├── dataspoke-port-forward.sh     # Port-forward DataSpoke infra services
├── lock-port-forward.sh          # Port-forward lock service (localhost:9221)
├── datahub/                      # DataHub Helm install (prerequisites + datahub charts)
├── dataspoke-infra/              # DataSpoke infra via umbrella chart (values-dev.yaml)
├── dataspoke-lock/               # Lock service (plain K8s manifests, dataspoke-01 ns)
├── dataspoke-example/            # Example data sources (plain K8s manifests)
└── dummy-data-port-forward.sh    # Port-forward example PostgreSQL + Kafka
```

## Environment Variables

Two-tier naming convention in `.env`:

| Prefix | Scope | Example |
|--------|-------|---------|
| `DATASPOKE_DEV_*` | Dev scripts only | `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` |
| `DATASPOKE_*` (no `DEV`) | App runtime | `DATASPOKE_POSTGRES_HOST`, `DATASPOKE_REDIS_HOST` |

App runtime variables point to `localhost` in dev (via port-forward) and to in-cluster services in production (via Helm values).

## Resource Budget

This environment targets ~11.1 GiB memory limits on an 8+ CPU / 16 GB RAM cluster (~69% utilization). See `spec/feature/DEV_ENV.md` for field-tested rationale per component.

| Component | Namespace | Memory Limit |
|-----------|-----------|-------------|
| Elasticsearch | datahub-01 | 2560 Mi |
| Kafka (bitnami) | datahub-01 | 512 Mi |
| ZooKeeper (bitnami) | datahub-01 | 256 Mi |
| MySQL (prerequisites) | datahub-01 | 768 Mi |
| datahub-gms | datahub-01 | 1536 Mi |
| datahub-frontend | datahub-01 | 768 Mi |
| datahub-mae-consumer | datahub-01 | 512 Mi |
| datahub-mce-consumer | datahub-01 | 512 Mi |
| datahub-actions | datahub-01 | 256 Mi |
| temporal-server | dataspoke-01 | 1024 Mi |
| qdrant | dataspoke-01 | 1024 Mi |
| postgresql (dataspoke) | dataspoke-01 | 512 Mi |
| redis | dataspoke-01 | 256 Mi |
| dev-lock | dataspoke-01 | 64 Mi |
| example-postgres | dataspoke-dummy-data-01 | 256 Mi |
| example-kafka | dataspoke-dummy-data-01 | 512 Mi |
| **Total** | | **~11.1 Gi** |

## Troubleshooting

See [spec/feature/DEV_ENV.md §Troubleshooting](../spec/feature/DEV_ENV.md#troubleshooting) for detailed solutions to common issues including Elasticsearch OOM, MySQL OOM, pending pods, and port-forward failures.
