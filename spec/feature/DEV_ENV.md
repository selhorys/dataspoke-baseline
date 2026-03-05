# DEV_ENV — Development Environment

## Table of Contents

1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Architecture](#architecture)
4. [Configuration](#configuration)
5. [Components](#components)
6. [Install & Uninstall](#install--uninstall)
7. [Port Forwarding](#port-forwarding)
8. [Dummy Data](#dummy-data)
9. [Resource Budget](#resource-budget)
10. [Troubleshooting](#troubleshooting)
11. [References](#references)

---

## Overview

`dev_env/` provides a fully scripted Kubernetes-based environment for developing and testing DataSpoke. It provisions three namespaces and installs **infrastructure dependencies** that the DataSpoke application connects to.

Application components (frontend, API, workers) are **not** installed in the cluster — developers run them on the host, connecting to port-forwarded infrastructure services. DataHub is installed in the dev cluster only for development; in production it is deployed separately.

```
Kubernetes Cluster (e.g. minikube, docker-desktop, or remote)
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

- Single command (`./install.sh`) to stand up infrastructure dependencies for development
- Clean namespace separation matching the production topology
- DataHub with **Elasticsearch graph backend** for lineage support (Neo4j is not required)
- Example data sources (PostgreSQL + Kafka) in a dedicated namespace for testing ingestion workflows
- Advisory lock service for coordinating multi-tester access to shared dev state
- Idempotent installs — re-running `install.sh` is always safe
- Resource-constrained sizing that fits within ~70% of a typical dev cluster (8+ CPU / 16 GB RAM)

### Non-Goals

- Production deployment (use `helm-charts/dataspoke` for production)
- Running DataSpoke application services in-cluster as the default workflow (for on-demand in-cluster testing, use the umbrella Helm chart with application subcharts enabled — see [TESTING.md §Testing Modes](../TESTING.md#testing-modes))
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

> Namespace names are **defaults** from `.env.example`. All scripts read them from environment variables and never hardcode them.

### Directory Structure

```
dev_env/
├── .env.example / .env              # Configuration (see §Configuration)
├── install.sh / uninstall.sh        # Top-level orchestrators
├── lib/helpers.sh                   # Shared shell functions: info(), warn(), error()
├── datahub-port-forward.sh          # Port-forward DataHub UI + GMS + Kafka
├── dataspoke-port-forward.sh        # Port-forward DataSpoke infra services
├── dummy-data-port-forward.sh       # Port-forward example PostgreSQL + Kafka
├── lock-port-forward.sh             # Port-forward lock service
├── datahub/                         # DataHub Helm install (prerequisites + datahub charts)
├── dataspoke-infra/                 # DataSpoke infra via umbrella chart (values-dev.yaml)
├── dataspoke-lock/                  # Lock service (plain K8s manifests)
├── dataspoke-example/               # Example data sources (plain K8s manifests)
├── dummy-data-reset.sh              # Idempotent reset of dummy data (SQL + Kafka)
└── dummy-data/                      # SQL seed files + Kafka topic/message scripts
```

---

## Configuration

All scripts source `dev_env/.env`. Copy `.env.example` to `.env` and edit before first use. The `.env` file is gitignored.

### Two-tier naming convention

| Prefix | Scope | Who reads it |
|--------|-------|-------------|
| `DATASPOKE_DEV_*` | Dev environment only | `dev_env/*.sh` scripts |
| `DATASPOKE_*` (no `DEV`) | Application runtime | DataSpoke app code (same names in dev and prod, different values) |

### Variable categories

See `.env.example` for the complete listing with comments. Key categories:

| Category | Example variables | Notes |
|----------|-------------------|-------|
| Cluster & namespaces | `DATASPOKE_DEV_KUBE_CLUSTER`, `*_NAMESPACE` | Kubernetes context and 3 namespace names |
| Helm chart versions | `*_CHART_VERSION` | DataHub prerequisites 0.2.1, DataHub 0.8.3 |
| Port-forward ports | `*_PORT_FORWARD_*_PORT` | All configurable; defaults in 9xxx range |
| DataHub MySQL creds | `*_MYSQL_ROOT_PASSWORD`, `*_MYSQL_PASSWORD` | Dev-only, 16+ chars |
| Example data creds | `*_DUMMY_DATA_POSTGRES_*` | Dev-only |
| DataHub connection | `DATASPOKE_DATAHUB_GMS_URL`, `*_TOKEN`, `*_KAFKA_BROKERS` | App runtime — `localhost` in dev |
| Infrastructure | `DATASPOKE_POSTGRES_*`, `*_REDIS_*`, `*_QDRANT_*`, `*_TEMPORAL_*` | App runtime — `localhost` in dev |
| LLM | `DATASPOKE_LLM_PROVIDER`, `*_API_KEY`, `*_MODEL` | App runtime |

### Policies

- **Password policy**: All passwords must be 15+ characters, mixed case, at least one special character.
- **API key policy**: LLM and service API keys must never be committed. The `.env` file is gitignored; for CI/CD, inject via Kubernetes Secrets or a secrets manager.

---

## Components

### DataHub

| Chart | Version | App Version |
|-------|---------|-------------|
| `datahub/datahub-prerequisites` | 0.2.1 | — |
| `datahub/datahub` | 0.8.3 | v1.4.0 |

**Key decisions**:

- **No Neo4j**: Elasticsearch provides full graph backend support including multi-hop lineage. Saves ~2 Gi RAM + 10 Gi PVC. Aligns with upstream defaults.
- **No Schema Registry**: DataHub v1.4.0 uses an internal schema registry (`type: INTERNAL`).
- **No `--wait` on Helm install**: The `datahub-system-update` bootstrap job takes 5-10 minutes. Scripts use custom poll-based readiness checks instead.
- **Relaxed liveness probes** on GMS and frontend to tolerate transient ES restarts.

Prerequisites resource sizing:

| Component | Mem Limit | Notes |
|-----------|-----------|-------|
| Elasticsearch | 2560 Mi | Off-heap usage OOM-kills at 2Gi during startup |
| Kafka | 512 Mi | Adequate for single-node dev |
| ZooKeeper | 256 Mi | Adequate for single-node dev |
| MySQL | 768 Mi | `mysql_upgrade` briefly doubles memory when persistence disabled |

DataHub component sizing: GMS 1536 Mi (-25% vs upstream), Frontend 768 Mi (-45%), MAE/MCE consumers 512 Mi each (-67%), Actions 256 Mi (-50%).

Service name prefix `datahub-prerequisites-` applies to all prerequisite services (MySQL, Kafka, ZooKeeper) because the prerequisites chart is installed as its own Helm release.

---

### DataSpoke Infrastructure

Infrastructure dependencies installed via the DataSpoke umbrella Helm chart with the dev profile (`values-dev.yaml`). See [HELM_CHART.md](HELM_CHART.md) for chart details.

| Component | Type | Mem Limit | PV |
|-----------|------|-----------|-----|
| temporal-server | Deployment | 1024 Mi | — |
| qdrant | StatefulSet | 1024 Mi | 10 Gi |
| postgresql | StatefulSet | 512 Mi | 10 Gi |
| redis | Deployment | 256 Mi | — |

**Kubernetes Secrets** (created by `dataspoke-infra/install.sh` before Helm install):

| Secret Name | Keys |
|-------------|------|
| `dataspoke-postgres-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `dataspoke-redis-secret` | `REDIS_PASSWORD` |
| `dataspoke-qdrant-secret` | `QDRANT_API_KEY` (only if non-empty) |

> LLM secrets are not deployed into the cluster. The host-running app reads them directly from `.env`.

---

### Example Data Sources

Plain Kubernetes manifests (no Helm) in the `dummy-data1` namespace.

| Component | Image | Mem Limit | Storage | Service |
|-----------|-------|-----------|---------|---------|
| PostgreSQL | `postgres:15` | 256 Mi | 5 Gi PVC | `example-postgres:5432` |
| Kafka | `apache/kafka:3.9.0` (KRaft) | 512 Mi | 1 Gi PVC | `example-kafka:9092` |

This Kafka instance is **separate** from DataHub's prerequisites Kafka. It simulates an external data source for ingestion testing.

---

### Lock Service

Advisory mutex for coordinating multi-tester access. Lightweight Python HTTP server in the `dataspoke-01` namespace (pure stdlib, no external dependencies).

| Resource | Details |
|----------|---------|
| Deployment | `dev-lock` — 1 replica, `python:3.12-slim`, 64 Mi / 100m CPU |
| Service | `dev-lock` — ClusterIP, port 8080 |

Lock state is **in-memory only** — resets on pod restart. See [TESTING.md §Integration Testing](../TESTING.md#integration-testing) for the full lock protocol.

**API**:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/lock` | Check current lock status |
| `POST` | `/lock/acquire` | Acquire (body: `{"owner": "...", "message": "..."}`) |
| `POST` | `/lock/release` | Release (body: `{"owner": "..."}`) |
| `DELETE` | `/lock` | Force-release (no owner check) |

Response codes: `200` success, `400` missing owner, `403` non-owner release, `409` already held.

---

## Install & Uninstall

### install.sh

Top-level orchestrator: sources `.env`, verifies `kubectl`/`helm`, switches kube context, creates namespaces, then calls sub-installers in order: `datahub/` → `dataspoke-infra/` → `dataspoke-example/` → `dataspoke-lock/`. Prints port-forward instructions on completion.

### uninstall.sh

Reverse order: `dataspoke-lock/` → `dataspoke-example/` → `dataspoke-infra/` → `datahub/`. Prompts before destructive operations.

| Flag | Effect |
|------|--------|
| `--yes` | Skip "remove all resources?" confirmation |
| `--delete-namespaces` | Also delete the three namespaces |

### Shell conventions

All scripts use `#!/usr/bin/env bash`, `set -euo pipefail`, and source shared helpers from `lib/helpers.sh`. All mutating kubectl/helm operations are idempotent.

---

## Port Forwarding

All port-forward scripts run in background, write PIDs to dotfiles, and support `--stop` to terminate cleanly. They guard against duplicate launches.

| Script | Services | PID File |
|--------|----------|----------|
| `datahub-port-forward.sh` | UI (:9002), GMS (:9004), Kafka (:9005) | `.datahub-port-forward.pid` |
| `dataspoke-port-forward.sh` | PG (:9201), Redis (:9202), Qdrant (:9203/:9204), Temporal (:9205) | `.dataspoke-port-forward.pid` |
| `dummy-data-port-forward.sh` | example-postgres (:9102), example-kafka (:9104) | `.dummy-data-port-forward.pid` |
| `lock-port-forward.sh` | dev-lock (:9221) | `.lock-port-forward.pid` |

### Port Map

| Service | Cluster Address | Host Address |
|---------|----------------|--------------|
| DataHub UI | `datahub-frontend:9002` | `localhost:9002` |
| DataHub GMS | `datahub-datahub-gms:8080` | `localhost:9004` |
| DataHub Kafka | `datahub-prerequisites-kafka:9092` | `localhost:9005` |
| PostgreSQL (dataspoke) | `dataspoke-postgresql:5432` | `localhost:9201` |
| Redis | `dataspoke-redis-master:6379` | `localhost:9202` |
| Qdrant HTTP / gRPC | `dataspoke-qdrant:6333/6334` | `localhost:9203/9204` |
| Temporal | `dataspoke-temporal-frontend:7233` | `localhost:9205` |
| Lock API | `dev-lock:8080` | `localhost:9221` |
| example-postgres | `example-postgres:5432` | `localhost:9102` |
| example-kafka | `example-kafka:9092` | `localhost:9104` |

All ports are configurable via `DATASPOKE_DEV_*_PORT` variables. The `DATASPOKE_*_HOST/PORT` app runtime variables in `.env` point to these localhost addresses.

---

## Dummy Data

`dummy-data-reset.sh` populates `example-postgres` and `example-kafka` with realistic Imazon use-case data. **Idempotent**: drops all custom schemas CASCADE and recreates them; deletes and recreates Kafka topics.

### PostgreSQL (17 tables, ~600 rows)

| Schema | Table | Rows | UC | Key Characteristics |
|--------|-------|------|-----|---------------------|
| `catalog` | `genre_hierarchy` | 15 | UC7 | Self-referencing hierarchy |
| `catalog` | `title_master` | 30 | UC1,7 | isbn+edition_id composite PK |
| `catalog` | `editions` | 40 | UC1,7 | Join path to order_items |
| `orders` | `order_items` | 80 | UC7 | edition_id FK → editions |
| `orders` | `daily_fulfillment_summary` | 30 | UC3 | 1 anomalous low-volume day |
| `orders` | `raw_events` | 100 | UC3 | Lifecycle event stream |
| `orders` | `eu_purchase_history` | 30 | UC5 | PII: address, payment_last4 |
| `customers` | `eu_profiles` | 20 | UC5 | PII: email, name, DOB |
| `reviews` | `user_ratings` | 50 | UC2 | Healthy: no NULLs |
| `reviews` | `user_ratings_legacy` | 50 | UC2 | Degraded: ~30% NULL rating_score |
| `publishers` | `feed_raw` | 20 | UC1 | Upstream JSONB payloads |
| `shipping` | `carrier_status` | 40 | UC3 | Includes delayed/exception statuses |
| `inventory` | `book_stock` | 25 | UC4 | Multi-warehouse stock |
| `marketing` | `eu_email_campaigns` | 15 | UC5 | Downstream of eu_profiles |
| `products` | `digital_catalog` | 20 | UC4 | eBookNow: ~30% NULL isbn |
| `content` | `ebook_assets` | 20 | UC4 | EPUB/PDF/MOBI/COVER/SAMPLE |
| `storefront` | `listing_items` | 15 | UC4 | Marketplace listings |

### Kafka (3 topics, ~45 messages)

| Topic | Messages | UC |
|-------|----------|----|
| `imazon.orders.events` | 20 | UC3 |
| `imazon.shipping.updates` | 15 | UC3 |
| `imazon.reviews.new` | 10 | UC2 |

### Data Design Choices

- **UC2 anomaly**: `user_ratings_legacy` has 30% NULL `rating_score` — tests data quality detection.
- **UC3 SLA**: `daily_fulfillment_summary` has 1 anomalous day (Jan 15, `row_count=12` vs typical ~145) — tests freshness/volume anomaly detection.
- **UC4 overlap**: ~70% of `digital_catalog` titles match `title_master` by ISBN — tests cross-source lineage matching.
- **UC5 PII**: Fake but structurally realistic EU PII across DE/FR/ES/IT/NL — tests PII classification and GDPR propagation.
- **UC7 join path**: Full referential integrity `order_items → editions → title_master → genre_hierarchy` — tests multi-hop lineage.
- **ISBNs**: 978-prefix, obviously fake (e.g., `9780000000001`).

---

## Resource Budget

Cluster capacity: **8 CPU / 16 GB RAM / 150 GB storage**. Target usage: **~69%** → ~11.1 GiB RAM, ~7.75 CPU limits.

### Memory Budget (limits)

| Component | Namespace | Mem Limit | Notes |
|-----------|-----------|-----------|-------|
| Elasticsearch | datahub-01 | 2560 Mi | 512m heap + off-heap |
| Kafka (bitnami) | datahub-01 | 512 Mi | |
| ZooKeeper (bitnami) | datahub-01 | 256 Mi | |
| MySQL (bitnami) | datahub-01 | 768 Mi | `mysql_upgrade` doubles memory |
| datahub-gms | datahub-01 | 1536 Mi | -25% vs upstream |
| datahub-frontend | datahub-01 | 768 Mi | -45% vs upstream |
| datahub-mae-consumer | datahub-01 | 512 Mi | -67% vs upstream |
| datahub-mce-consumer | datahub-01 | 512 Mi | -67% vs upstream |
| datahub-actions | datahub-01 | 256 Mi | -50% vs upstream |
| temporal-server | dataspoke-01 | 1024 Mi | |
| qdrant | dataspoke-01 | 1024 Mi | |
| postgresql (dataspoke) | dataspoke-01 | 512 Mi | |
| redis | dataspoke-01 | 256 Mi | |
| dev-lock | dataspoke-01 | 64 Mi | |
| example-postgres | dummy-data1 | 256 Mi | |
| example-kafka | dummy-data1 | 512 Mi | |
| **Total** | | **~11.1 Gi** | |

~4.9 GiB headroom for K8s system components, Helm setup jobs, and host-running app services.

### CPU Budget (limits)

Total: **7750m** across all components. Pods rarely hit limits simultaneously. Explicit limits prevent starvation on constrained dev clusters. See `dev_env/datahub/prerequisites-values.yaml` and `helm-charts/dataspoke/values-dev.yaml` for per-component breakdown.

---

## Troubleshooting

### Elasticsearch OOM-killed during startup

**Cause**: Off-heap usage (Lucene cache, index recovery) spikes above 2Gi. Upstream default 1024Mi is insufficient.
**Fix**: Already applied — ES memory limit set to 2560Mi in `prerequisites-values.yaml`.

### MySQL OOM-killed on restart

**Cause**: With persistence disabled, `mysql_upgrade` runs on every start, briefly doubling memory beyond 512Mi.
**Fix**: Already applied — MySQL memory limit set to 768Mi.

### Pod stuck in Pending

**Cause**: Insufficient cluster resources.
**Fix**: Check `kubectl describe node`. The full environment requires ~11.1 GiB / ~7.75 CPU — 16 GB / 8+ CPU recommended.

### datahub-system-update takes 5-10 minutes

**Cause**: Expected on first install — bootstraps all DataHub metadata schemas.
**Fix**: Wait. The script polls every 10s with progress logging.

### Port-forward connection refused

**Cause**: Target pod not yet Ready.
**Fix**: Verify pods are `1/1 Running` in the target namespace, then re-run the port-forward script.

---

## References

- [DataHub — Deploying with Kubernetes](https://docs.datahub.com/docs/deploy/kubernetes) — minimum: 2 CPUs, 8 GB RAM
- [DataHub Helm chart defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/datahub/values.yaml)
- [DataHub prerequisites defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/prerequisites/values.yaml)
- [Migrating Graph Service Implementation](https://docs.datahub.com/docs/how/migrating-graph-service-implementation)
- [HELM_CHART.md](HELM_CHART.md) — DataSpoke umbrella Helm chart specification
- [TESTING.md](../TESTING.md) — Testing conventions and dev-env lock protocol
