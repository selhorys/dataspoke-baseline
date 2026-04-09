# DEV_ENV — Development Environment

## Table of Contents

1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Architecture](#architecture)
4. [Configuration](#configuration)
5. [Components](#components)
6. [Install & Uninstall](#install--uninstall)
7. [Ingress](#ingress)
8. [Dummy Data](#dummy-data)
9. [Resource Budget](#resource-budget)
10. [Troubleshooting](#troubleshooting)
11. [References](#references)

---

## Overview

`dev_env/` provides a fully scripted Kubernetes-based environment for developing and testing DataSpoke. It provisions three namespaces and installs **infrastructure dependencies** that the DataSpoke application connects to.

The API server is deployed **in-cluster** alongside Kestra so that workflow callbacks work directly via cluster DNS (`http://dataspoke-api:8002`). Developers access the API via the nginx-ingress endpoint (`http://app.<INGRESS_IP>.nip.io/api/v1/`) for testing. Frontend and workers are not installed in the dev cluster. See [TESTING.md §Testing Modes](../TESTING.md#testing-modes).

DataHub is installed in the dev cluster **for convenience**; in production it is an external dependency deployed and managed separately.

**Dev Architecture:**

```
Kubernetes Cluster (GKE Autopilot or any compatible cluster)
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  ┌──────────────────┐  nginx-ingress  LoadBalancer IP        │
│  │ ingress-nginx    │◄─────────────────────────────────────  │
│  │ (controller)     │  HTTP :80 (virtual hosts)              │
│  └──────────────────┘  TCP :9201-9204, :9005, :9102,        │
│         │              :9104, :9221                          │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │ Routes                                                   │ │
│  │  datahub.<IP>.nip.io       → datahub-01/GMS + Frontend  │ │
│  │  app.<IP>.nip.io/api       → dataspoke-01/API           │ │
│  │  kestra.<IP>.nip.io        → dataspoke-01/Kestra        │ │
│  │  <IP>:9201                 → dataspoke-01/PostgreSQL     │ │
│  │  <IP>:9202                 → dataspoke-01/Redis          │ │
│  │  <IP>:9203/<IP>:9204       → dataspoke-01/Qdrant         │ │
│  │  <IP>:9005                 → datahub-01/Kafka            │ │
│  │  <IP>:9102/<IP>:9104       → dummy-data-01/PG+Kafka      │ │
│  │  <IP>:9221                 → dataspoke-01/Lock           │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─────────────────────┐   ┌──────────────────────────────┐  │
│  │  datahub-01         │   │  dataspoke-dummy-data-01     │  │
│  │  - GMS              │   │  - PostgreSQL (example src)  │  │
│  │  - Frontend         │   │  - Kafka (example src)       │  │
│  │  - MAE/MCE consumer │   └──────────────────────────────┘  │
│  │  - Kafka + ZK       │                                     │
│  │  - Elasticsearch    │   ┌──────────────────────────────┐  │
│  │  - MySQL            │   │  dataspoke-01                │  │
│  └─────────────────────┘   │  - api (in-cluster)          │  │
│                            │  - kestra                    │  │
│                            │  - qdrant                    │  │
│                            │  - postgresql                │  │
│                            │  - redis                     │  │
│                            │  - dev-lock (advisory mutex) │  │
│                            └──────────────────────────────┘  │
│                                                              │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │
│  Host (outside cluster)                                      │
│    dataspoke-frontend  (npm run dev, :3000)                  │
│    (API, Kestra, all infra accessed via nginx-ingress)       │
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
- Resource-constrained sizing that fits within ~70% of a typical dev cluster (8+ CPU / 24 GB RAM)

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
| `ingress-nginx` | nginx-ingress controller (single LoadBalancer for all namespaces) | `nginx-ingress/install.sh` via Helm |
| `datahub-01` | DataHub platform + all backing services | `datahub/install.sh` via Helm |
| `dataspoke-01` | DataSpoke infrastructure (API, Kestra, Qdrant, PostgreSQL, Redis) + lock service | `dataspoke-infra/install.sh` via Helm; `dataspoke-lock/install.sh` via kubectl |
| `dataspoke-dummy-data-01` | Example PostgreSQL + Kafka for ingestion testing | `dataspoke-example/install.sh` via kubectl |

> Namespace names are **defaults** from `.env.example`. All scripts read them from environment variables and never hardcode them.

### Directory Layout

`dev_env/` contains: top-level orchestrators (`install.sh`, `uninstall.sh`), shared helpers (`lib/helpers.sh`), and five sub-installers: `nginx-ingress/` (ingress controller), `datahub/` (Helm), `dataspoke-infra/` (umbrella chart), `dataspoke-lock/` (plain K8s manifests), `dataspoke-example/` (plain K8s manifests). Configuration in `.env` (copied from `.env.example`).

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
| Cluster & namespaces | `DATASPOKE_DEV_KUBE_CLUSTER`, `*_NAMESPACE` | Kubernetes context and namespace names (includes `ingress-nginx`) |
| Ingress | `DATASPOKE_DEV_INGRESS_IP`, `DATASPOKE_DEV_INGRESS_DOMAIN` | Written by `nginx-ingress/install.sh`; domain defaults to `dev.dataspoke.example.com` (use `<IP>.nip.io` for automatic DNS) |
| Helm chart versions | `*_CHART_VERSION` | DataHub prerequisites 0.2.1, DataHub 0.8.21 |
| DataHub MySQL creds | `*_MYSQL_ROOT_PASSWORD`, `*_MYSQL_PASSWORD` | Dev-only, 16+ chars |
| Example data creds | `*_EXAMPLE_PG_HOST`, `*_EXAMPLE_PG_PORT`, `*_EXAMPLE_KAFKA_BROKERS` | Dev-only; host and port resolve via ingress IP |
| DataHub connection | `DATASPOKE_DATAHUB_GMS_URL`, `*_TOKEN`, `*_KAFKA_BROKERS` | App runtime — ingress URL in dev |
| Infrastructure | `DATASPOKE_POSTGRES_HOST/PORT`, `*_REDIS_*`, `*_QDRANT_*`, `*_KESTRA_URL` | App runtime — ingress IP (TCP) or URL (HTTP) in dev |
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
| `datahub/datahub` | 0.8.21 | v1.4.0.3 |

**Key decisions**:

- **No Neo4j**: Elasticsearch provides full graph backend support including multi-hop lineage. Saves ~2 Gi RAM + 10 Gi PVC. Aligns with upstream defaults.
- **No Schema Registry**: DataHub v1.4.0.3 uses an internal schema registry (`type: INTERNAL`).
- **No `--wait` on Helm install**: The `datahub-system-update` bootstrap job takes 5-10 minutes. Scripts use custom poll-based readiness checks instead.
- **Relaxed liveness probes** on GMS and frontend to tolerate transient ES restarts.

Prerequisites resource sizing:

| Component | Mem Limit | Notes |
|-----------|-----------|-------|
| Elasticsearch | 2560 Mi | Off-heap usage OOM-kills at 2Gi during startup |
| Kafka | 768 Mi | 512m heap cap + reduced threads/retention |
| ZooKeeper | 256 Mi | Adequate for single-node dev |
| MySQL | 768 Mi | `mysql_upgrade` briefly doubles memory when persistence disabled |

DataHub component sizing: GMS 1536 Mi (-25% vs upstream), Frontend 768 Mi (-45%), MAE/MCE consumers 512 Mi each (-67%), Actions 256 Mi (-50%).

Service name prefix `datahub-prerequisites-` applies to all prerequisite services (MySQL, Kafka, ZooKeeper) because the prerequisites chart is installed as its own Helm release.

---

### DataSpoke Infrastructure

Infrastructure dependencies installed via the DataSpoke umbrella Helm chart with the dev profile (`values-dev.yaml`). See [HELM_CHART.md](HELM_CHART.md) for chart details.

| Component | Type | Mem Limit | PV |
|-----------|------|-----------|-----|
| kestra | Deployment | 6 Gi | — |
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

Plain Kubernetes manifests (no Helm) in the `dataspoke-dummy-data-01` namespace.

| Component | Image | Mem Limit | Storage | Service |
|-----------|-------|-----------|---------|---------|
| PostgreSQL | `postgres:15` | 256 Mi | 5 Gi PVC | `example-postgres:5432` |
| Kafka | `apache/kafka:3.9.0` (KRaft) | 512 Mi | 1 Gi PVC | `example-kafka:9092` (internal), `:9094` (EXTERNAL) |

This Kafka instance is **separate** from DataHub's prerequisites Kafka. It simulates an external data source for ingestion testing. Like DataHub Kafka, it exposes an EXTERNAL listener (port 9094) that advertises `<INGRESS_IP>:9104` for host-side access via TCP passthrough on the nginx-ingress controller.

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

Top-level orchestrator: sources `.env`, verifies `kubectl`/`helm`, switches kube context, creates namespaces, then calls sub-installers in order: `nginx-ingress/` → `datahub/` → `dataspoke-infra/` → `dataspoke-example/` → `dataspoke-lock/`. Prints ingress endpoint summary on completion.

### uninstall.sh

Reverse order: `dataspoke-lock/` → `dataspoke-example/` → `dataspoke-infra/` → `datahub/` → `nginx-ingress/`. Prompts before destructive operations.

| Flag | Effect |
|------|--------|
| `--yes` | Skip "remove all resources?" confirmation |
| `--delete-namespaces` | Also delete the three namespaces |

### Shell conventions

All scripts use `#!/usr/bin/env bash`, `set -euo pipefail`, and source shared helpers from `lib/helpers.sh`. All mutating kubectl/helm operations are idempotent.

---

## Ingress

All services are accessed via the nginx-ingress controller deployed in the `ingress-nginx` namespace. The controller acquires a single external LoadBalancer IP (`DATASPOKE_DEV_INGRESS_IP`) that is written to `dev_env/.env` by `nginx-ingress/install.sh`.

### Tier A — HTTP Virtual Hosts

HTTP services are accessed by virtual-host name on port 80. The `nip.io` suffix provides automatic wildcard DNS resolution — no `/etc/hosts` entries needed.

| Service | Cluster Address | Ingress URL |
|---------|----------------|-------------|
| DataHub UI | `datahub-01/datahub-frontend:9002` | `http://datahub.<INGRESS_IP>.nip.io/` |
| DataHub GMS | `datahub-01/datahub-datahub-gms:8080` | `http://datahub.<INGRESS_IP>.nip.io/gms/` |
| DataSpoke API | `dataspoke-01/dataspoke-api:8002` | `http://app.<INGRESS_IP>.nip.io/api/v1/` |
| Kestra UI | `dataspoke-01/dataspoke-kestra:8080` | `http://kestra.<INGRESS_IP>.nip.io/` |

### Tier B — TCP Passthrough

TCP services are accessed directly on dedicated ports via the ingress LoadBalancer IP. The nginx-ingress `tcp-services` ConfigMap maps each external port to the target cluster service.

| Service | Cluster Address | External Port |
|---------|----------------|---------------|
| DataSpoke PostgreSQL | `dataspoke-01/dataspoke-postgresql:5432` | `<INGRESS_IP>:9201` |
| Redis | `dataspoke-01/dataspoke-redis-master:6379` | `<INGRESS_IP>:9202` |
| Qdrant HTTP | `dataspoke-01/dataspoke-qdrant:6333` | `<INGRESS_IP>:9203` |
| Qdrant gRPC | `dataspoke-01/dataspoke-qdrant:6334` | `<INGRESS_IP>:9204` |
| DataHub Kafka | `datahub-01/datahub-prerequisites-kafka:9095` (EXTERNAL) | `<INGRESS_IP>:9005` |
| Lock API | `dataspoke-01/dev-lock:8080` | `<INGRESS_IP>:9221` |
| example-postgres | `dataspoke-dummy-data-01/example-postgres:5432` | `<INGRESS_IP>:9102` |
| example-kafka | `dataspoke-dummy-data-01/example-kafka:9094` (EXTERNAL) | `<INGRESS_IP>:9104` |

The `DATASPOKE_*_HOST/PORT` app runtime variables in `.env` point to these ingress addresses. Kafka Tier B services advertise `<INGRESS_IP>:<port>` as their EXTERNAL listener so that host-side producers and consumers can connect through the ingress.

---

## Dummy Data

The `dataspoke-dummy-data-01` namespace provides example PostgreSQL and Kafka instances populated with Imazon use-case data (11 schemas, 17 tables, ~600 rows; 3 Kafka topics, ~45 messages). Both PG tables and Kafka topics are registered as DataHub dataset entities (20 total) with `DatasetProperties` and `SchemaMetadata` aspects. Seed files, ingestion logic, and data design details live in `tests/integration/util/` — see [`TESTING.md §Test Data Design`](../TESTING.md#test-data-design) for the full reference.

---

## Resource Budget

Cluster capacity: **8 CPU / 24 GB RAM / 150 GB storage**. Target usage: **~70%** → ~16.8 GiB RAM, ~7.75 CPU limits.

### Memory Budget (limits)

| Component | Namespace | Mem Limit | Notes |
|-----------|-----------|-----------|-------|
| Elasticsearch | datahub-01 | 2560 Mi | 512m heap + off-heap |
| Kafka (bitnami) | datahub-01 | 768 Mi | 512m heap cap + reduced threads/retention |
| ZooKeeper (bitnami) | datahub-01 | 256 Mi | |
| MySQL (bitnami) | datahub-01 | 768 Mi | `mysql_upgrade` doubles memory |
| datahub-gms | datahub-01 | 1536 Mi | -25% vs upstream |
| datahub-frontend | datahub-01 | 768 Mi | -45% vs upstream |
| datahub-mae-consumer | datahub-01 | 512 Mi | -67% vs upstream |
| datahub-mce-consumer | datahub-01 | 512 Mi | -67% vs upstream |
| datahub-actions | datahub-01 | 256 Mi | -50% vs upstream |
| kestra | dataspoke-01 | 6 Gi | 1g–4g heap + G1GC; polling/cleaner/telemetry tuned for dev |
| qdrant | dataspoke-01 | 1024 Mi | |
| postgresql (dataspoke) | dataspoke-01 | 512 Mi | |
| redis | dataspoke-01 | 256 Mi | |
| dev-lock | dataspoke-01 | 64 Mi | |
| example-postgres | dataspoke-dummy-data-01 | 256 Mi | |
| example-kafka | dataspoke-dummy-data-01 | 1024 Mi | |
| **Total** | | **~16.8 Gi** | |

~7.2 GiB headroom for K8s system components, Helm setup jobs, and host-running app services.

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
**Fix**: Check `kubectl describe node`. The full environment requires ~16.8 GiB / ~7.75 CPU — 24 GB / 8+ CPU recommended.

### datahub-system-update takes 5-10 minutes

**Cause**: Expected on first install — bootstraps all DataHub metadata schemas.
**Fix**: Wait. The script polls every 10s with progress logging.

### MAE consumer stalled after restart

**Cause**: The embedded MAE consumer in GMS crashes when processing stale MCL messages accumulated from previous runs. The Spring Kafka error handler shuts down the consumer permanently, leaving timeseries aspects unindexed in Elasticsearch.
**Fix**: Already automated in `datahub/install.sh` — detects stalled consumer group, resets offsets to latest, and restarts GMS. If it recurs outside install, manually reset offsets on `MetadataChangeLog_Timeseries_v1` and `MetadataChangeLog_Versioned_v1` for group `generic-mae-consumer-job-client`, then restart the GMS pod.

### Service unreachable via ingress

**Cause**: Target pod not yet Ready, or the nginx-ingress controller has not yet received an external IP.
**Fix**: Verify the ingress controller is running (`kubectl get pods -n ingress-nginx`) and has an external IP (`kubectl get svc -n ingress-nginx`). Then verify the target pod is `1/1 Running` in its namespace. Re-run `./dev_env/health-check.sh` once pods are ready.

---

## Open Questions

- [ ] When DataSpoke exposes a redefined dataset registration API (blended API/UI), `tests/integration/util/datahub.py` could be replaced by calls to that API for integration test setup. This would simplify the test workflow and exercise the redefined API as part of every test run.

---

## References

- [DataHub — Deploying with Kubernetes](https://docs.datahub.com/docs/deploy/kubernetes) — minimum: 2 CPUs, 8 GB RAM
- [DataHub Helm chart defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/datahub/values.yaml)
- [DataHub prerequisites defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/prerequisites/values.yaml)
- [Migrating Graph Service Implementation](https://docs.datahub.com/docs/how/migrating-graph-service-implementation)
- [HELM_CHART.md](HELM_CHART.md) — DataSpoke umbrella Helm chart specification
- [TESTING.md](../TESTING.md) — Testing conventions and dev-env lock protocol
