# DEV_ENV — Local Development Environment

## Table of Contents

1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Architecture](#architecture)
4. [Configuration](#configuration)
5. [Components](#components)
   - [DataHub](#datahub)
   - [DataSpoke Infrastructure](#dataspoke-infrastructure)
   - [Example Data Sources](#example-data-sources)
   - [Lock Service](#lock-service)
6. [Install & Uninstall](#install--uninstall)
7. [Port Forwarding](#port-forwarding)
8. [Dummy Data](#dummy-data)
9. [Running DataSpoke Locally](#running-dataspoke-locally)
10. [Resource Budget](#resource-budget)
11. [Troubleshooting](#troubleshooting)
12. [References](#references)

---

## Overview

`dev_env/` provides a fully scripted local Kubernetes environment for developing and testing DataSpoke. It provisions three namespaces — `datahub-01`, `dataspoke-01`, `dummy-data1` (defaults; see [Configuration](#configuration)) — and installs **infrastructure dependencies** that the DataSpoke application connects to.

The `dataspoke-01` namespace hosts infrastructure services (Temporal, Qdrant, PostgreSQL, Redis) and the dev-env lock service. DataSpoke application components (frontend, API, workers) are **not** installed in the cluster — developers run them locally, connecting to port-forwarded infrastructure services.

DataHub is installed locally only for development and testing. In production, DataHub is deployed separately and DataSpoke connects to it externally.

```
Local Kubernetes Cluster (minikube / docker-desktop)
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  ┌─────────────────────┐   ┌──────────────────────────────┐  │
│  │  datahub-01         │   │  dummy-data1                 │  │
│  │                     │   │                              │  │
│  │  - GMS              │   │  - PostgreSQL (example src)  │  │
│  │  - Frontend         │◄──┤  - Kafka (example src)       │  │
│  │  - MAE/MCE consumer │   │                              │  │
│  │  - Kafka + ZK       │   └──────────────────────────────┘  │
│  │  - Elasticsearch    │                                     │
│  │  - MySQL            │   ┌──────────────────────────────┐  │
│  │                     │   │  dataspoke-01                │  │
│  └─────────────────────┘   │  (infrastructure only)       │  │
│                            │                              │  │
│                            │  - temporal-server           │  │
│                            │  - qdrant                    │  │
│                            │  - postgresql                │  │
│                            │  - redis                     │  │
│                            │  - dev-lock (advisory mutex) │  │
│                            └──────────────────────────────┘  │
│                                                              │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │
│  Host (outside cluster)                                      │
│    dataspoke-frontend  (npm run dev, :3000)                  │
│    dataspoke-api       (uvicorn, :8000)                      │
│    dataspoke-workers   (temporal worker, connects to infra)  │
└──────────────────────────────────────────────────────────────┘
```

---

## Goals & Non-Goals

### Goals

- Single command (`./install.sh`) to stand up infrastructure dependencies for local development
- Clean namespace separation matching the production topology
- DataHub with **Elasticsearch graph backend** for lineage support (Neo4j is not required)
- Example data sources (PostgreSQL + Kafka) in a dedicated namespace for testing ingestion workflows
- Advisory lock service for coordinating multi-tester access to shared dev state
- Idempotent installs — re-running `install.sh` is always safe
- Resource-constrained sizing that fits within ~70% of a typical local cluster (8+ CPU / 16 GB RAM)
- Port-forwarded infrastructure services accessible from host for local app development

### Non-Goals

- Production deployment (use `helm-charts/dataspoke` for production)
- Running DataSpoke application services in-cluster (developers run frontend, API, and workers on the host)
- External data source connectivity (example sources are in-cluster only)
- High availability or data persistence between dev environment resets

---

## Architecture

### Namespaces

| Namespace | Purpose | Managed By |
|-----------|---------|------------|
| `datahub-01` | DataHub platform + all backing services | `datahub/install.sh` via Helm |
| `dataspoke-01` | DataSpoke infrastructure (Temporal, Qdrant, PostgreSQL, Redis) + lock service | `dataspoke-infra/install.sh` via Helm; `dataspoke-lock/install.sh` via kubectl |
| `dummy-data1` | Example PostgreSQL + Kafka for ingestion testing | `dataspoke-example/install.sh` via kubectl |

> **Note**: The namespace names above are the **default values** shipped in `dev_env/.env.example`. All scripts read these names exclusively from environment variables — `$DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`, and `$DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE` — and never hardcode them. You can rename the namespaces freely by editing `.env` before running `install.sh`.
>
> **Naming convention**: `DATASPOKE_DEV_*` marks variables used only by dev environment scripts. `DATASPOKE_*` (without `DEV`) marks application runtime variables read by DataSpoke code — same variable names in dev and prod, different values. See [Configuration](#configuration) for details.

### Directory Structure

```
dev_env/
├── .env.example                          # Template — copy to .env and fill in values
├── .env                                  # All settings (gitignored)
├── README.md                             # Quick-start guide
├── install.sh                            # Top-level: creates namespaces + calls sub-installers
├── uninstall.sh                          # Top-level: tears down all dev_env resources
├── lib/
│   └── helpers.sh                        # Shared shell functions: info(), warn(), error()
│
├── datahub-port-forward.sh               # Port-forward DataHub UI + GMS + Kafka
├── dataspoke-port-forward.sh             # Port-forward DataSpoke infra services
├── dummy-data-port-forward.sh            # Port-forward example PostgreSQL + Kafka
├── lock-port-forward.sh                  # Port-forward lock service
│
├── datahub/
│   ├── install.sh                        # Installs DataHub via Helm (with manual pod polling)
│   ├── uninstall.sh                      # Uninstalls DataHub Helm releases
│   ├── prerequisites-values.yaml         # Kafka, ZK, Elasticsearch, MySQL sizing
│   └── values.yaml                       # DataHub component sizing + service name overrides
│
├── dataspoke-infra/
│   ├── install.sh                        # Installs DataSpoke infra via helm-charts/dataspoke with values-dev.yaml
│   └── uninstall.sh                      # Uninstalls DataSpoke infra Helm release
│
├── dataspoke-lock/
│   ├── install.sh                        # Applies lock service manifests (dataspoke-01 namespace)
│   ├── uninstall.sh                      # Deletes lock service resources
│   └── manifests/
│       └── lock-service.yaml             # ConfigMap (Python script) + Deployment + Service
│
├── dataspoke-example/
│   ├── install.sh                        # Applies manifests and waits for readiness
│   ├── uninstall.sh                      # Deletes manifests
│   └── manifests/
│       ├── kafka.yaml                    # Kafka (KRaft) Deployment + Service + PVC + topic-init Job
│       └── postgres.yaml                 # PostgreSQL 15 Deployment + Service + Secret + PVC
│
├── dummy-data-reset.sh                   # Idempotent reset of dummy data (SQL + Kafka)
└── dummy-data/
    ├── sql/
    │   ├── 00_schemas.sql                # 11 CREATE SCHEMA statements
    │   ├── 01_catalog.sql                # UC1, UC4, UC7 — genre_hierarchy, title_master, editions
    │   ├── 02_orders.sql                 # UC3, UC5, UC7 — order_items, fulfillment, raw_events, eu_purchase_history
    │   ├── 03_customers.sql              # UC5 — eu_profiles (PII: email, name, DOB)
    │   ├── 04_reviews.sql                # UC2 — user_ratings (healthy) + user_ratings_legacy (degraded)
    │   ├── 05_publishers.sql             # UC1 — feed_raw (upstream JSONB payloads)
    │   ├── 06_shipping.sql               # UC3 — carrier_status (UPS/FedEx/DHL)
    │   ├── 07_inventory.sql              # UC4 — book_stock (Imazon warehouses)
    │   ├── 08_marketing.sql              # UC5 — eu_email_campaigns (downstream of PII)
    │   └── 09_ebooknow.sql              # UC4 — eBookNow: digital_catalog, ebook_assets, listing_items
    └── kafka/
        ├── init-topics.sh                # Delete + recreate 3 topics
        └── seed-messages.sh              # Produce ~45 JSON messages
```

---

## Configuration

All scripts source `dev_env/.env`. Copy `dev_env/.env.example` to `dev_env/.env` and fill in your values before first use. The `.env` file is listed in `.gitignore` — do not commit it.

Variables are split into two tiers:

| Prefix | Scope | Who reads it | Where set |
|--------|-------|-------------|-----------|
| `DATASPOKE_DEV_*` | Dev environment only | `dev_env/*.sh` scripts | `dev_env/.env` |
| `DATASPOKE_*` (no `DEV`) | Application runtime | DataSpoke app code (FastAPI, workers, frontend) | `dev_env/.env` (dev), Helm values → K8s ConfigMap/Secret (prod) |

### Dev Environment Variables (`DATASPOKE_DEV_*`)

These variables configure the local Kubernetes cluster and dev tooling. The application code never reads them.

```dotenv
# --- Kubernetes Cluster & Namespaces -----------------------------------------
DATASPOKE_DEV_KUBE_CLUSTER=minikube
DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE=datahub-01
DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE=dataspoke-01
DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE=dummy-data1

# --- Helm Chart Versions -----------------------------------------------------
DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION=0.2.1
DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION=0.8.3

# --- Port-Forward Ports (DataHub) --------------------------------------------
DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_UI_PORT=9002
DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_GMS_PORT=9004
DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_KAFKA_PORT=9005

# --- Port-Forward Ports (DataSpoke Infra) ------------------------------------
DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT=9201
DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT=9202
DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT=9203
DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_GRPC_PORT=9204
DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_TEMPORAL_PORT=9205
DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_DEV_ENV_LOCK_PORT=9221

# --- DataHub MySQL Credentials (dev only) ------------------------------------
DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_ROOT_PASSWORD=<16+ char password>
DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_PASSWORD=<16+ char password>

# --- Example Data Source Credentials (dev only) ------------------------------
DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER=postgres
DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD=ExampleDev2024!
DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB=example_db
DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT=9102
DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARD_PORT=9104
```

### Application Runtime Variables (`DATASPOKE_*`)

These variables are read by DataSpoke application code. In dev, they point to `localhost` (port-forwarded from the cluster). In production, they point to in-cluster services via Helm values → ConfigMap/Secret.

```dotenv
# --- DataHub Connection -------------------------------------------------------
DATASPOKE_DATAHUB_GMS_URL=http://localhost:9004
DATASPOKE_DATAHUB_TOKEN=                          # empty — DataHub doesn't require auth in dev
DATASPOKE_DATAHUB_KAFKA_BROKERS=localhost:9005

# --- PostgreSQL (DataSpoke operational DB) ------------------------------------
DATASPOKE_POSTGRES_HOST=localhost
DATASPOKE_POSTGRES_PORT=9201
DATASPOKE_POSTGRES_USER=dataspoke
DATASPOKE_POSTGRES_PASSWORD=<16+ char password>
DATASPOKE_POSTGRES_DB=dataspoke

# --- Redis --------------------------------------------------------------------
DATASPOKE_REDIS_HOST=localhost
DATASPOKE_REDIS_PORT=9202
DATASPOKE_REDIS_PASSWORD=<16+ char password>

# --- Qdrant -------------------------------------------------------------------
DATASPOKE_QDRANT_HOST=localhost
DATASPOKE_QDRANT_HTTP_PORT=9203
DATASPOKE_QDRANT_GRPC_PORT=9204
DATASPOKE_QDRANT_API_KEY=<optional-api-key>

# --- Temporal -----------------------------------------------------------------
DATASPOKE_TEMPORAL_HOST=localhost
DATASPOKE_TEMPORAL_PORT=9205
DATASPOKE_TEMPORAL_NAMESPACE=dataspoke

# --- LLM API -----------------------------------------------------------------
DATASPOKE_LLM_PROVIDER=gemini
DATASPOKE_LLM_API_KEY=<your-api-key>
DATASPOKE_LLM_MODEL=gemini-2.0-flash
```

Sub-scripts (`datahub/install.sh`, `dataspoke-infra/install.sh`, etc.) source `../.env` relative to their own `SCRIPT_DIR`. The top-level scripts source `./.env`.

**Password policy**: all passwords must be at minimum 15 characters, mixed case with at least one special character (e.g., `DatahubDev2024!`).

**API key policy**: LLM API keys (`DATASPOKE_LLM_API_KEY`) and optional service keys (`DATASPOKE_QDRANT_API_KEY`) must never be committed to version control. The `.env` file is gitignored; for CI/CD, inject these via Kubernetes Secrets or a secrets manager.

---

## Components

### DataHub

#### Helm Chart Versions

| Chart | Version | App Version |
|-------|---------|-------------|
| `datahub/datahub-prerequisites` | 0.2.1 | — |
| `datahub/datahub` | 0.8.3 | v1.4.0 |

#### Why No Neo4j

The upstream DataHub Helm chart ships with `neo4j.enabled: false` and `graph_service_impl: elasticsearch` by default. Elasticsearch now provides full graph backend support including multi-hop lineage traversal. Removing Neo4j saves ~2 Gi RAM + 10 Gi PVC and aligns with upstream defaults. For production environments requiring heavy graph traversal at scale, Neo4j can be re-enabled — see the [DataHub migration guide](https://docs.datahub.com/docs/how/migrating-graph-service-implementation).

#### Kubernetes Secrets

| Secret Name | Namespace | Keys |
|-------------|-----------|------|
| `mysql-secrets` | `$DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` | `mysql-root-password`, `mysql-password` |

Created idempotently using `--dry-run=client -o yaml | kubectl apply -f -`.

#### prerequisites-values.yaml

| Component | Subchart | CPU Req / Limit | Mem Req / Limit | Notes |
|-----------|----------|-----------------|-----------------|-------|
| Kafka broker | `kafka` (bitnami) | 200m / 1000m | 256Mi / 512Mi | `broker.replicaCount: 1`, ZooKeeper mode (KRaft disabled) |
| ZooKeeper | `kafka.zookeeper` (bitnami) | 50m / 200m | 128Mi / 256Mi | `replicaCount: 1` |
| Elasticsearch | `elasticsearch` | 200m / 1000m | 1536Mi / 2560Mi | `esJavaOpts: -Xmx512m -Xms512m` |
| MySQL | `mysql` (bitnami) | 100m / 500m | 256Mi / 768Mi | `auth.existingSecret: mysql-secrets`, persistence disabled |

Schema Registry is **not deployed** — DataHub v1.4.0 uses an internal schema registry (`type: INTERNAL`). Neo4j is **disabled** (upstream default).

**Resource rationale**: ES needs 2560Mi because off-heap usage (Lucene cache, index recovery) OOM-kills at 2Gi during concurrent startup. MySQL needs 768Mi because `mysql_upgrade` (runs on every restart when persistence is disabled) briefly doubles memory. ZooKeeper at 256Mi and Kafka at 512Mi are adequate for single-node dev.

#### values.yaml Key Overrides

Because the prerequisites chart is installed as release name `datahub-prerequisites`, all internal service names get the `datahub-prerequisites-` prefix:

```yaml
global:
  sql.datasource.host: "datahub-prerequisites-mysql:3306"
  kafka.bootstrap.server: "datahub-prerequisites-kafka:9092"
  kafka.zookeeper.server: "datahub-prerequisites-zookeeper:2181"
  elasticsearch.host: "elasticsearch-master"
  graph_service_impl: elasticsearch
```

DataHub component resources:

| Component | CPU Req / Limit | Mem Req / Limit | vs Upstream |
|-----------|-----------------|-----------------|-------------|
| `datahub-gms` | 500m / 1500m | 768Mi / 1536Mi | -25% mem (JVM app, sufficient for dev-scale metadata) |
| `datahub-frontend` | 200m / 500m | 384Mi / 768Mi | -45% mem (single-user React+Play) |
| `datahub-mae-consumer` | 100m / 500m | 256Mi / 512Mi | -67% mem (low event volume in dev) |
| `datahub-mce-consumer` | 100m / 500m | 256Mi / 512Mi | -67% mem (low event volume in dev) |
| `datahub-actions` | 50m / 200m | 128Mi / 256Mi | -50% mem (lightweight Python) |

GMS and frontend have relaxed liveness probes (higher `failureThreshold`) to tolerate transient ES restarts on single-node dev clusters.

#### datahub/install.sh Steps

1. Source `../.env`; verify `kubectl` and `helm`
2. Switch to `$DATASPOKE_DEV_KUBE_CLUSTER` context
3. Add/update `datahub` Helm repo (`https://helm.datahubproject.io/`)
4. Ensure `$DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` namespace exists
5. Create `mysql-secrets` (idempotent)
6. `helm upgrade --install datahub-prerequisites` with `prerequisites-values.yaml`, `--timeout 5m` (no `--wait` — manual polling instead)
7. Wait for each prerequisite pod sequentially: MySQL → Elasticsearch → ZooKeeper → Kafka (custom `wait_for_pod` with 10s poll / 30s progress logging)
8. `helm upgrade --install datahub` with `values.yaml`, `--timeout 15m` (no `--wait`)
9. Wait for setup jobs: `datahub-elasticsearch-setup-job` (120s), `datahub-mysql-setup-job` (120s), `datahub-system-update` (600s)
10. Wait for service pods: GMS, Frontend, Actions (via label lookup + `wait_for_pod`)
11. Print port-forward instructions

> The script does **not** use Helm's `--wait` flag because the `datahub-system-update` hook (a heavy JVM bootstrap job) can take 5-10 minutes, which would cause Helm's hook timeout to fire prematurely. Instead, the script installs without `--wait` and polls for readiness using custom `wait_for_pod` / `wait_for_job` helpers that tolerate transient CrashLoopBackOff during startup.

---

### DataSpoke Infrastructure

The `dataspoke-01` namespace hosts **infrastructure dependencies** that the DataSpoke application connects to. Application services run on the developer's host machine.

#### Components

| Component | Type | Chart Source | CPU Req / Limit | Mem Req / Limit | PV |
|-----------|------|-------------|-----------------|-----------------|-----|
| temporal-server | Deployment | `temporalio/temporal` | — / 500m | — / 1024 Mi | — |
| qdrant | StatefulSet | `qdrant/qdrant` | — / 500m | — / 1024 Mi | 10 Gi |
| postgresql | StatefulSet | `bitnami/postgresql` | — / 500m | — / 512 Mi | 10 Gi |
| redis | Deployment | `bitnami/redis` | — / 250m | — / 256 Mi | — |

Installed via the DataSpoke umbrella Helm chart with the dev profile:

```bash
helm upgrade --install dataspoke ../../helm-charts/dataspoke/ \
  -f ../../helm-charts/dataspoke/values-dev.yaml \
  -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE \
  --set postgresql.auth.existingSecret=dataspoke-postgres-secret \
  --set redis.auth.existingSecret=dataspoke-redis-secret \
  ... # Temporal persistence credentials via --set
  --timeout 5m --wait
```

The `values-dev.yaml` profile disables all application subcharts (frontend, api, workers) and sets single replicas with reduced resources. See [HELM_CHART.md §Value Profiles](HELM_CHART.md#value-profiles) for details.

#### Kubernetes Secrets

| Secret Name | Namespace | Keys |
|-------------|-----------|------|
| `dataspoke-postgres-secret` | `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `dataspoke-redis-secret` | `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` | `REDIS_PASSWORD` |
| `dataspoke-qdrant-secret` | `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` | `QDRANT_API_KEY` (created only if `DATASPOKE_QDRANT_API_KEY` is non-empty) |

Created idempotently using `--dry-run=client -o yaml | kubectl apply -f -`.

> **Note**: LLM secrets (`DATASPOKE_LLM_*`) are not deployed into the cluster. In dev, the locally-running application reads them directly from `dev_env/.env` or the shell environment.

#### dataspoke-infra/install.sh Steps

1. Source `../.env`; verify `kubectl` and `helm`
2. Ensure `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` namespace exists
3. Create Kubernetes Secrets from `.env` variables (see table above)
4. `helm upgrade --install dataspoke` with `values-dev.yaml` and credential `--set` overrides, `--timeout 5m --wait`
5. Print port-forward instructions

---

### Example Data Sources

Plain Kubernetes manifests (no Helm). Applied with `kubectl apply -f manifests/`.

#### PostgreSQL (`manifests/postgres.yaml`)

| Field | Value |
|-------|-------|
| Image | `postgres:15` |
| User | `$DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER` (default: `postgres`) |
| Database | `$DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB` (default: `example_db`) |
| Password | `$DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD` (default: `ExampleDev2024!`) |
| Memory limit | 256 Mi |
| Storage | 5 Gi PVC at `/var/lib/postgresql/data` |
| Service | ClusterIP, port 5432, name `example-postgres` |

Credentials are sourced from `dev_env/.env` variables. The `install.sh` script creates the `example-postgres-secret` via `kubectl create secret --from-literal` before applying manifests.

#### Kafka (`manifests/kafka.yaml`)

| Field | Value |
|-------|-------|
| Image | `apache/kafka:3.9.0` |
| Mode | KRaft (no ZooKeeper) |
| Memory limit | 512 Mi |
| Storage | 1 Gi PVC at `/var/lib/kafka/data` |
| Service | ClusterIP, port 9092, name `example-kafka` |
| Topic init | Job `example-kafka-topic-init` creates `example_topic` (1 partition, RF 1) |

This Kafka instance is **separate** from DataHub's prerequisites Kafka in `datahub-01`. It simulates an external Kafka data source for ingestion testing.

#### Kubernetes Secrets

| Secret Name | Namespace | Keys |
|-------------|-----------|------|
| `example-postgres-secret` | `$DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |

#### dataspoke-example/install.sh Steps

1. Source `../.env`
2. Ensure `$DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE` namespace exists
3. Create `example-postgres-secret` (idempotent via `--dry-run=client -o yaml | kubectl apply -f -`)
4. `kubectl apply -f ./manifests/`
5. Wait for PostgreSQL: `kubectl rollout status deployment/example-postgres --timeout=3m`
6. Wait for Kafka: `kubectl rollout status deployment/example-kafka --timeout=3m`
7. Wait for topic-init job: `kubectl wait --for=condition=complete job/example-kafka-topic-init --timeout=2m`
8. Print connection details

---

### Lock Service

The lock service provides an advisory mutex for coordinating multi-tester access to the shared dev environment. It runs as a lightweight Python HTTP server in the `dataspoke-01` namespace with no external dependencies (pure stdlib).

Use the lock before any operation that mutates shared state (data resets, schema migrations, ingestion runs). See [TESTING.md §Integration Testing](../TESTING.md#integration-testing) for the full lock protocol.

#### Architecture

| Resource | Details |
|----------|---------|
| ConfigMap | `dev-lock-script` — embeds `lock_service.py` (Python 3.12, pure stdlib) |
| Deployment | `dev-lock` — 1 replica, `python:3.12-slim` image, 64 Mi memory / 100m CPU limit |
| Service | `dev-lock` — ClusterIP, port 8080 |
| Health checks | `/health` endpoint (liveness + readiness probes) |

Lock state is **in-memory only** — it resets to unlocked if the pod restarts. The lock is advisory; it does not block access to infrastructure services directly.

#### API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/lock` | Check current lock status |
| `POST` | `/lock/acquire` | Acquire the lock (body: `{"owner": "...", "message": "..."}`) |
| `POST` | `/lock/release` | Release the lock (body: `{"owner": "..."}`) |
| `DELETE` | `/lock` | Force-release (no owner check) |

Response codes: `200` success, `400` missing owner, `403` release by non-owner, `409` lock already held.

#### dataspoke-lock/install.sh Steps

1. Source `../.env`
2. Ensure `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` namespace exists
3. `kubectl apply -f manifests/` (ConfigMap + Deployment + Service)
4. Wait for rollout: `kubectl rollout status deployment/dev-lock --timeout=2m`
5. Print access info

---

## Install & Uninstall

### install.sh (top-level)

```
./install.sh
  ├── source .env
  ├── check kubectl, helm
  ├── kubectl config use-context $DATASPOKE_DEV_KUBE_CLUSTER
  ├── create namespaces (if not exist):
  │     $DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
  │     $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE
  │     $DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE
  ├── call datahub/install.sh
  ├── call dataspoke-infra/install.sh
  ├── call dataspoke-example/install.sh
  ├── call dataspoke-lock/install.sh
  └── print summary + port-forward instructions
```

The summary prints all four port-forward scripts:
- `./datahub-port-forward.sh` — DataHub UI + GMS + Kafka
- `./dataspoke-port-forward.sh` — DataSpoke infra (PG, Redis, Qdrant, Temporal)
- `./dummy-data-port-forward.sh` — Example data sources
- `./lock-port-forward.sh` — Lock service

### uninstall.sh (top-level)

```
./uninstall.sh [--yes] [--delete-namespaces]
  ├── source .env
  ├── prompt: "Remove all dev_env resources? [y/N]"   (skipped with --yes)
  ├── call dataspoke-lock/uninstall.sh
  ├── call dataspoke-example/uninstall.sh
  ├── call dataspoke-infra/uninstall.sh
  ├── call datahub/uninstall.sh
  └── prompt: "Delete namespaces? [y/N]"              (auto-yes with --delete-namespaces)
```

| Flag | Effect |
|------|--------|
| `--yes` | Skip the "remove all resources?" confirmation |
| `--delete-namespaces` | Skip the "delete namespaces?" prompt and delete them |

### Shell Script Standards

- Shebang: `#!/usr/bin/env bash`
- Error handling: `set -euo pipefail`
- Location: `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`
- Helpers: `info()`, `warn()`, `error()` — defined in `dev_env/lib/helpers.sh`, sourced by all scripts
- All mutating kubectl/helm operations are idempotent

---

## Port Forwarding

All port-forward scripts run in the background, write PIDs to dotfiles (`.datahub-port-forward.pid`, etc.), and support `--stop` to cleanly terminate. They guard against duplicate launches by checking existing PID files.

### Scripts

| Script | Services Forwarded | PID File |
|--------|--------------------|----------|
| `datahub-port-forward.sh` | UI (:9002), GMS (:9004), Kafka (:9005) | `.datahub-port-forward.pid` |
| `dataspoke-port-forward.sh` | PostgreSQL (:9201), Redis (:9202), Qdrant HTTP (:9203), Qdrant gRPC (:9204), Temporal (:9205) | `.dataspoke-port-forward.pid` |
| `dummy-data-port-forward.sh` | example-postgres (:9102), example-kafka (:9104) | `.dummy-data-port-forward.pid` |
| `lock-port-forward.sh` | dev-lock (:9221) | `.lock-port-forward.pid` |

### Usage

```bash
./datahub-port-forward.sh          # start
./datahub-port-forward.sh --stop   # stop
```

### Full Port Map

| Service | Cluster Address | Host Address | Port Variable |
|---------|----------------|--------------|---------------|
| DataHub UI | `datahub-frontend:9002` | `localhost:9002` | `DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_UI_PORT` |
| DataHub GMS | `datahub-datahub-gms:8080` | `localhost:9004` | `DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_GMS_PORT` |
| DataHub Kafka | `datahub-prerequisites-kafka:9092` | `localhost:9005` | `DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_KAFKA_PORT` |
| PostgreSQL (dataspoke) | `dataspoke-postgresql:5432` | `localhost:9201` | `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT` |
| Redis | `dataspoke-redis-master:6379` | `localhost:9202` | `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT` |
| Qdrant HTTP | `dataspoke-qdrant:6333` | `localhost:9203` | `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT` |
| Qdrant gRPC | `dataspoke-qdrant:6334` | `localhost:9204` | `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_GRPC_PORT` |
| Temporal | `dataspoke-temporal-frontend:7233` | `localhost:9205` | `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_TEMPORAL_PORT` |
| Lock API | `dev-lock:8080` | `localhost:9221` | `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_DEV_ENV_LOCK_PORT` |
| example-postgres | `example-postgres:5432` | `localhost:9102` | `DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT` |
| example-kafka | `example-kafka:9092` | `localhost:9104` | `DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARD_PORT` |

The application runtime variables (`DATASPOKE_*_HOST`, `DATASPOKE_*_PORT`) in `.env` point to these localhost addresses, so the locally-running app connects transparently.

---

## Dummy Data

The `dummy-data-reset.sh` script populates `example-postgres` and `example-kafka` with realistic sample data based on Imazon use-case scenarios. It is **idempotent**: every run drops all custom schemas CASCADE and recreates them, and deletes+recreates Kafka topics.

### Prerequisites

- Dev environment cluster running (`install.sh` completed)
- `example-postgres` and `example-kafka` deployments are Ready in `$DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE`

### Usage

```bash
cd dev_env && ./dummy-data-reset.sh
```

### PostgreSQL Schema Summary (17 tables, ~600 rows)

| Schema | Table | Rows | Primary UC | Key Characteristics |
|--------|-------|------|------------|---------------------|
| `catalog` | `genre_hierarchy` | 15 | UC7 | Self-referencing hierarchy (code PK, parent_code FK) |
| `catalog` | `title_master` | 30 | UC1,UC7 | ~18 cols, isbn+edition_id composite PK, genre_code FK |
| `catalog` | `editions` | 40 | UC1,UC7 | edition_id PK, isbn, format; join path to order_items |
| `orders` | `order_items` | 80 | UC7 | edition_id FK → editions, order_id FK |
| `orders` | `daily_fulfillment_summary` | 30 | UC3 | 30 days; 1 anomalous low-volume day (Jan 15) |
| `orders` | `raw_events` | 100 | UC3 | Event stream: placed/confirmed/shipped/delivered |
| `orders` | `eu_purchase_history` | 30 | UC5 | PII: shipping_address, payment_last4 |
| `customers` | `eu_profiles` | 20 | UC5 | PII: email, full_name, DOB; EU country codes (DE/FR/ES/IT/NL) |
| `reviews` | `user_ratings` | 50 | UC2 | Healthy: rating_score NOT NULL |
| `reviews` | `user_ratings_legacy` | 50 | UC2 | Degraded: ~30% NULL rating_score (15/50 rows) |
| `publishers` | `feed_raw` | 20 | UC1 | Upstream feed with raw_payload JSONB |
| `shipping` | `carrier_status` | 40 | UC3 | UPS/FedEx/DHL, includes delayed and exception statuses |
| `inventory` | `book_stock` | 25 | UC4 | Imazon warehouse stock across WH-East/West/Central |
| `marketing` | `eu_email_campaigns` | 15 | UC5 | Downstream of eu_profiles; customer_ids array |
| `products` | `digital_catalog` | 20 | UC4 | eBookNow: ~30% NULL isbn, free-text creator field |
| `content` | `ebook_assets` | 20 | UC4 | eBookNow: EPUB/PDF/MOBI/COVER/SAMPLE assets |
| `storefront` | `listing_items` | 15 | UC4 | eBookNow: marketplace listings with badges |

### Kafka Topics (3 topics, ~45 messages)

| Topic | Messages | Purpose |
|-------|----------|---------|
| `imazon.orders.events` | 20 | UC3 — order lifecycle events (JSON) |
| `imazon.shipping.updates` | 15 | UC3 — carrier tracking updates (JSON) |
| `imazon.reviews.new` | 10 | UC2 — new review submissions (JSON) |

### Data Design Choices

- **UC2 anomaly**: `user_ratings_legacy` has 15/50 rows with NULL `rating_score` (30% null rate) vs. `user_ratings` which is fully populated — allows testing data quality detection.
- **UC3 SLA**: `daily_fulfillment_summary` has 1 anomalous day (Jan 15) with `row_count=12` vs. a typical ~145 — allows testing freshness/volume anomaly detection.
- **UC4 overlap**: ~70% of `products.digital_catalog` titles match `catalog.title_master` by title/ISBN — tests cross-source lineage matching after eBookNow acquisition.
- **UC5 PII**: Fake but structurally realistic EU names/addresses across DE, FR, ES, IT, NL — tests PII classification and GDPR propagation.
- **UC7 join path**: Full referential integrity across `order_items → editions → title_master → genre_hierarchy` — tests lineage tracing through multi-hop joins.
- **ISBNs**: 978-prefix, obviously fake (e.g., `9780000000001`) — no collision with real books.

### Verification

```bash
# After running dummy-data-reset.sh, port-forward and verify:
psql -h localhost -p 9102 -U postgres -d example_db

# Check tables exist
\dt catalog.*

# Check row counts
SELECT count(*) FROM catalog.title_master;              -- expect 30
SELECT count(*) FROM reviews.user_ratings_legacy
  WHERE rating_score IS NULL;                            -- expect 15

# Check UC7 join path
SELECT g.display_name, t.title, e.format, oi.quantity
FROM orders.order_items oi
JOIN catalog.editions e ON e.edition_id = oi.edition_id
JOIN catalog.title_master t ON t.isbn = e.isbn
JOIN catalog.genre_hierarchy g ON g.code = t.genre_code
LIMIT 5;
```

---

## Running DataSpoke Locally

After `dev_env/install.sh` completes and port-forwarding is active, developers run DataSpoke application services on the host:

### Prerequisites

1. Infrastructure is running: `kubectl get pods -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` shows all pods Ready
2. Port-forwarding is active: `./dataspoke-port-forward.sh` (and `./datahub-port-forward.sh` for DataHub access)
3. Environment variables are loaded: `source dev_env/.env` or use a tool like `direnv`

### Starting Application Services

```bash
# Frontend (Next.js dev server)
cd src/frontend && npm run dev          # http://localhost:3000

# API (FastAPI with uvicorn)
cd src/api && uvicorn main:app --reload --port 8000   # http://localhost:8000

# Workers (Temporal worker process)
cd src/workflows && python -m worker    # Connects to localhost:9205

# Or use the Makefile (when available):
make dev-up       # Starts all three services
make dev-down     # Stops all services
```

### How It Connects

```
┌─────────────────────────────────────────────────────────┐
│ Host                                                    │
│                                                         │
│  dataspoke-api ──── DATASPOKE_POSTGRES_HOST=localhost ──┼──► kubectl port-forward ──► postgresql pod
│                ──── DATASPOKE_REDIS_HOST=localhost ─────┼──► kubectl port-forward ──► redis pod
│                ──── DATASPOKE_QDRANT_HOST=localhost ────┼──► kubectl port-forward ──► qdrant pod
│                ──── DATASPOKE_TEMPORAL_HOST=localhost ──┼──► kubectl port-forward ──► temporal pod
│                ──── DATASPOKE_DATAHUB_GMS_URL ─────────┼──► kubectl port-forward ──► datahub-gms pod
│                ──── DATASPOKE_DATAHUB_KAFKA_BROKERS ───┼──► kubectl port-forward ──► kafka pod
│                                                         │
│  dataspoke-frontend ── calls dataspoke-api on :8000 ───┤
│                                                         │
│  dataspoke-workers ── DATASPOKE_TEMPORAL_HOST ─────────┼──► kubectl port-forward ──► temporal pod
└─────────────────────────────────────────────────────────┘
```

### Database Migrations

```bash
# Apply DataSpoke DB migrations (when available)
cd src && alembic upgrade head
```

---

## Resource Budget

Cluster capacity: **8 CPU / 16 GB RAM / 150 GB storage**.
Target usage: **~69%** → ~11.1 GiB RAM, ~7.75 CPU limits.

### Memory Budget (limits)

> Namespace names are defaults from `.env.example`. Actual values are sourced from environment variables at runtime.

| Component | Namespace | Mem Limit | Notes |
|-----------|-----------|-----------|-------|
| Elasticsearch | datahub-01 | 2560 Mi | 512m heap + off-heap; upstream 1024M is insufficient |
| Kafka (bitnami) | datahub-01 | 512 Mi | Explicit limit (upstream unset) |
| ZooKeeper (bitnami) | datahub-01 | 256 Mi | Explicit limit (upstream unset) |
| MySQL (bitnami) | datahub-01 | 768 Mi | Explicit limit; 512Mi OOM-killed during `mysql_upgrade` |
| datahub-gms | datahub-01 | 1536 Mi | -25% vs upstream 2Gi |
| datahub-frontend | datahub-01 | 768 Mi | -45% vs upstream 1400Mi |
| datahub-mae-consumer | datahub-01 | 512 Mi | -67% vs upstream 1536Mi |
| datahub-mce-consumer | datahub-01 | 512 Mi | -67% vs upstream 1536Mi |
| datahub-actions | datahub-01 | 256 Mi | -50% vs upstream 512Mi |
| temporal-server | dataspoke-01 | 1024 Mi | Workflow orchestration engine |
| qdrant | dataspoke-01 | 1024 Mi | Vector DB for semantic search |
| postgresql (dataspoke) | dataspoke-01 | 512 Mi | Operational DB |
| redis | dataspoke-01 | 256 Mi | Cache, rate limiting |
| dev-lock | dataspoke-01 | 64 Mi | Advisory mutex service |
| example-postgres | dummy-data1 | 256 Mi | Minimal example source |
| example-kafka | dummy-data1 | 512 Mi | KRaft mode, no ZooKeeper |
| **Total** | | **~11.1 Gi** | |

~4.9 GiB headroom remains for Kubernetes system components, Helm setup jobs (up to 2Gi for `datahubSystemUpdate`), and locally-running DataSpoke application services.

### CPU Budget (limits)

| Component | Namespace | CPU Limit |
|-----------|-----------|-----------|
| Elasticsearch | datahub-01 | 1000m |
| Kafka | datahub-01 | 1000m |
| ZooKeeper | datahub-01 | 200m |
| MySQL (prereqs) | datahub-01 | 500m |
| datahub-gms | datahub-01 | 1500m |
| datahub-frontend | datahub-01 | 500m |
| datahub-mae-consumer | datahub-01 | 500m |
| datahub-mce-consumer | datahub-01 | 500m |
| datahub-actions | datahub-01 | 200m |
| temporal-server | dataspoke-01 | 500m |
| qdrant | dataspoke-01 | 500m |
| postgresql (dataspoke) | dataspoke-01 | 500m |
| redis | dataspoke-01 | 250m |
| dev-lock | dataspoke-01 | 100m |
| example-postgres | dummy-data1 | 500m |
| example-kafka | dummy-data1 | 500m |
| **Sum of limits** | | **7750m** |

CPU limits total 7.75 cores. Pods rarely hit limits simultaneously, so actual usage is well within budget. Explicit limits prevent any single component from starving others on a constrained dev cluster.

---

## Troubleshooting

### Elasticsearch OOM-killed during startup

**Symptom**: `elasticsearch-master-0` enters `OOMKilled` or `CrashLoopBackOff` shortly after startup.

**Cause**: ES off-heap usage (plugin loading, Lucene segment cache, index recovery) spikes above 2Gi during concurrent initialization. The upstream default limit of 1024Mi is insufficient.

**Fix**: Already applied — `prerequisites-values.yaml` sets ES memory limit to 2560Mi. If you still see OOM-kills, ensure no other script or Helm override is reducing the limit.

---

### MySQL OOM-killed on restart

**Symptom**: `datahub-prerequisites-mysql-0` enters `OOMKilled` after startup or restart.

**Cause**: With persistence disabled, MySQL runs `mysql_upgrade` on every container start, briefly doubling memory usage beyond 512Mi.

**Fix**: Already applied — `prerequisites-values.yaml` sets MySQL memory limit to 768Mi.

---

### Pod stuck in `Pending`

**Symptom**: A pod remains in `Pending`; `kubectl describe pod <name> -n <ns>` shows `Insufficient memory` or `Insufficient cpu`.

**Fix**:
1. Check node allocatable resources: `kubectl describe node`
2. Stop other resource-heavy workloads or increase Docker Desktop memory allocation (Settings → Resources).
3. The full dev environment requires ~11.1 GiB memory limits and ~7.75 CPU limits — 16 GB / 8+ CPU is the recommended minimum.

---

### `datahub-system-update` job takes a very long time

**Symptom**: `datahub/install.sh` waits on `datahub-system-update` for 5-10 minutes.

**Cause**: Expected on first install. The job bootstraps all DataHub metadata schemas and may pull large container images.

**Fix**: Wait it out — the script polls every 10s and prints progress every 30s. If it exceeds 10 minutes: `kubectl logs -l job-name=datahub-system-update -n $DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE --tail=20`

---

### Port-forward "connection refused" or immediate disconnect

**Symptom**: A port-forward script exits immediately or connections to localhost are refused.

**Cause**: The target pod is not yet `Ready`, or the pod name lookup returned empty.

**Fix**:
1. Verify pods are Running and Ready: `kubectl get pods -n <namespace>`
2. If pods show `0/1 Running`, dependent services may still be starting — wait and retry.
3. Re-run the port-forward script once pods are `1/1 Running`.

---

## References

- [DataHub — Deploying with Kubernetes](https://docs.datahub.com/docs/deploy/kubernetes) — official minimum requirements: 2 CPUs, 8 GB RAM, 2 GB swap
- [DataHub Helm chart defaults (datahub/values.yaml)](https://github.com/acryldata/datahub-helm/blob/master/charts/datahub/values.yaml) — upstream resource settings
- [DataHub prerequisites chart defaults (prerequisites/values.yaml)](https://github.com/acryldata/datahub-helm/blob/master/charts/prerequisites/values.yaml) — ES 1024M limit, Neo4j disabled
- [Migrating Graph Service Implementation](https://docs.datahub.com/docs/how/migrating-graph-service-implementation) — switching between Neo4j and ES graph backends
- [DataHub GMS OOM discussion (GitHub #11147)](https://github.com/datahub-project/datahub/issues/11147) — memory requirements for GMS JVM
- [Elastic — JVM heap size on Kubernetes](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-jvm-heap-size.html) — heap should be ~50% of container memory
- [HELM_CHART.md](HELM_CHART.md) — DataSpoke umbrella Helm chart specification
- [TESTING.md](../TESTING.md) — Testing conventions and dev-env lock protocol
