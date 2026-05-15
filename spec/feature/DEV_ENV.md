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

`dev_env/` provides a fully scripted Kubernetes-based environment for developing and testing
DataSpoke. It provisions four namespaces and installs **infrastructure dependencies** that the
DataSpoke application connects to.

The API server is deployed **in-cluster** alongside Airflow so that workflow callbacks work
directly via cluster DNS (`http://dataspoke-api:8002`). Developers access the API via the
nginx-ingress endpoint (`http://app.<INGRESS_IP>.nip.io/api/v1/`) for testing. Frontend and
event-consumer are not installed in the dev cluster.
See [TESTING.md §Testing Modes](../TESTING.md#testing-modes).

DataHub is installed in the dev cluster **for convenience**; in production it is an external
dependency deployed and managed separately.

**Dev Architecture**: a single nginx-ingress controller (`ingress-nginx` namespace) owns the
cluster's LoadBalancer IP and serves both HTTP virtual hosts (port 80 — DataHub UI/GMS, DataSpoke
API, Airflow, Langfuse) and TCP passthrough (ports 9005, 9102, 9104, 9201–9202, 9221 — Kafka,
PostgreSQL, Redis, Lock). Four application namespaces sit behind it: `datahub-01` (GMS, Frontend,
MAE/MCE consumers, Kafka KRaft, OpenSearch, MySQL), `dataspoke-01` (in-cluster API, Airflow,
PostgreSQL, Redis, dev-lock), `langfuse-01` (Langfuse web/worker + bundled Postgres, Redis,
ClickHouse, MinIO), and `dataspoke-dummy-data-01` (example PostgreSQL + Kafka). The frontend runs
on the host (`npm run dev`, :3000); all other components are reached through the ingress. Full
route/port mappings are in `dev_env/README.md §Ingress Endpoints`.

---

## Goals & Non-Goals

### Goals

- Single command (`./install.sh`) to stand up infrastructure dependencies for development
- Clean namespace separation matching the production topology
- DataHub with **OpenSearch graph backend** for lineage support (Neo4j is not required)
- Example data sources (PostgreSQL + Kafka) in a dedicated namespace for testing ingestion workflows
- Advisory lock service for coordinating multi-tester access to shared dev state
- Idempotent installs — re-running `install.sh` is always safe
- Resource-constrained sizing that fits within ~70% of a typical dev cluster (8+ CPU / 24 GB RAM)

### Non-Goals

- Production deployment (use `helm-charts/dataspoke` for production)
- Running the **frontend** or the **event-consumer** in-cluster (frontend runs on the host
  via `npm run dev`; the event-consumer is opt-in for organisations adding event-driven
  extensions and is not deployed in dev)
- External data source connectivity (example sources are in-cluster only)
- High availability or data persistence between dev environment resets

---

## Architecture

### Namespaces

| Namespace | Purpose | Managed By |
|-----------|---------|------------|
| `ingress-nginx` | nginx-ingress controller (single LoadBalancer for all namespaces) | `nginx-ingress/install.sh` via Helm |
| `datahub-01` | DataHub platform + all backing services | `datahub/install.sh` via Helm |
| `dataspoke-01` | DataSpoke infrastructure (API, Airflow, PostgreSQL, Redis) + lock service | `dataspoke-infra/install.sh` via Helm; `dataspoke-lock/install.sh` via kubectl |
| `langfuse-01` | Langfuse LLM observability (web, worker, bundled Postgres, Redis, ClickHouse, MinIO) | `langfuse/install.sh` via Helm |
| `dataspoke-dummy-data-01` | Example PostgreSQL + Kafka for ingestion testing | `dataspoke-example/install.sh` via kubectl |

> Namespace names are **defaults** from `.env.example`. All scripts read them from environment
> variables and never hardcode them.

### Directory Layout

`dev_env/` contains: top-level orchestrators (`install.sh`, `uninstall.sh`), shared helpers
(`lib/helpers.sh`), and six sub-installers: `nginx-ingress/` (ingress controller), `datahub/`
(Helm), `dataspoke-infra/` (umbrella chart), `langfuse/` (Langfuse LLM observability — own
namespace with bundled subcharts), `dataspoke-lock/` (plain K8s manifests), `dataspoke-example/`
(plain K8s manifests). Configuration in `.env` (copied from `.env.example`).

---

## Configuration

All scripts source `dev_env/.env`. Copy `.env.example` to `.env` and edit before first use.
The `.env` file is gitignored.

### Two-tier naming convention

| Prefix | Scope | Who reads it |
|--------|-------|-------------|
| `DATASPOKE_DEV_*` | Dev environment only | `dev_env/*.sh` scripts |
| `DATASPOKE_*` (no `DEV`) | Application runtime | DataSpoke app code (same names in dev and prod, different values) |

### Variable categories

See `.env.example` for the complete listing with comments. Key categories:

| Category | Example variables | Notes |
|----------|-------------------|-------|
| Cluster & namespaces | `DATASPOKE_DEV_KUBE_CLUSTER`, `*_NAMESPACE` | Kubernetes context and namespace names (includes `ingress-nginx`); `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE=langfuse-01` for the Langfuse namespace |
| Ingress | `DATASPOKE_DEV_INGRESS_IP`, `DATASPOKE_DEV_INGRESS_DOMAIN` | Written by `nginx-ingress/install.sh`; domain defaults to `dev.dataspoke.example.com` (use `<IP>.nip.io` for automatic DNS) |
| Helm chart versions | `*_CHART_VERSION` | DataHub prerequisites 0.3.0, DataHub 0.9.10 |
| DataHub MySQL creds | `*_MYSQL_ROOT_PASSWORD`, `*_MYSQL_PASSWORD` | Dev-only, 16+ chars |
| Example data creds | `*_EXAMPLE_PG_HOST`, `*_EXAMPLE_PG_PORT`, `*_EXAMPLE_KAFKA_BROKERS` | Dev-only; host and port resolve via ingress IP |
| DataHub connection | `DATASPOKE_DATAHUB_GMS_URL`, `*_TOKEN`, `*_KAFKA_BROKERS` | App runtime — ingress URL in dev |
| Infrastructure | `DATASPOKE_POSTGRES_HOST/PORT`, `*_REDIS_*`, `*_AIRFLOW_URL` | App runtime — ingress IP (TCP) or URL (HTTP) in dev |
| LLM | `DATASPOKE_LLM_PROVIDER`, `*_API_KEY`, `*_MODEL` | App runtime |

### Policies

- **Password policy**: All passwords must be 15+ characters, mixed case, at least one special
  character.
- **API key policy**: LLM and service API keys must never be committed. The `.env` file is
  gitignored; for CI/CD, inject via Kubernetes Secrets or a secrets manager.

---

## Components

### DataHub

| Chart | Version | App Version |
|-------|---------|-------------|
| `datahub/datahub-prerequisites` | 0.3.0 | — |
| `datahub/datahub` | 0.9.10 | v1.5.0.2 (pinned via `global.datahub.version` override; chart default is v1.5.0.1) |

**Key decisions**:

- **OpenSearch over Elasticsearch**: Prerequisites chart 0.3.0 ships OpenSearch 2.19.5 as the
  default search engine. DataHub GMS uses the same ES-client wire protocol, so migration is
  transparent.
- **Kafka in KRaft mode (no Zookeeper)**: Prerequisites chart 0.3.0 runs a single controller pod
  that also serves as broker (`controller.controllerOnly=false`, `broker.replicaCount=0`),
  eliminating the Zookeeper dependency.
- **No Neo4j**: OpenSearch provides full graph backend support including multi-hop lineage.
  Saves ~2 Gi RAM + 10 Gi PVC. Aligns with upstream defaults.
- **No Schema Registry**: DataHub uses an internal schema registry (`type: INTERNAL`).
- **No `--wait` on Helm install**: The `datahub-system-update` bootstrap job takes 5-10 minutes.
  Scripts use custom poll-based readiness checks instead.
- **Relaxed liveness probes** on GMS and frontend to tolerate transient OpenSearch restarts.
- **Frontend ingress uses `className: "nginx"`**: The `datahub-frontend` subchart (0.3.4) uses
  `className`, not `ingressClassName`; wrong key is silently dropped and GKE falls back to
  provisioning a GCE LoadBalancer.

Prerequisites resource sizing:

| Component | Mem Limit | Notes |
|-----------|-----------|-------|
| OpenSearch | 3072 Mi | 1 Gi JVM heap + off-heap cache; `singleNode: true` skips bootstrap checks |
| Kafka (KRaft controller) | 2048 Mi | 1.5 Gi heap cap; single pod carries both controller and broker roles |
| MySQL | 1536 Mi | `mysql_upgrade` briefly doubles memory on restart |

DataHub component sizing (limits): GMS 3 Gi, Frontend 1 Gi, MAE/MCE consumers 1 Gi each, Actions
512 Mi. Total ~6.5 Gi.

Service name prefix `datahub-prerequisites-` applies to all prerequisite services (MySQL, Kafka
controller) because the prerequisites chart is installed as its own Helm release. The OpenSearch
subchart uses its own release prefix (`opensearch-cluster-master`).

---

### DataSpoke Infrastructure

Infrastructure dependencies installed via the DataSpoke umbrella Helm chart with the dev profile
(`values-dev.yaml`). See [HELM_CHART.md](HELM_CHART.md) for chart details.

PostgreSQL runs a custom image (`${REGISTRY}/postgres:dev`) built from
`docker-images/postgres/Dockerfile` — a Bitnami PostgreSQL 17 runtime base with Apache AGE
(graph) and pgvector (vector search) extensions compiled in. Both extensions are connected to
the `dataspoke` database; `CREATE EXTENSION` runs idempotently on initdb and on every Alembic
migration deploy. AGE is reserved infrastructure available to any service that opts in.
`dev_env/dataspoke-infra/install.sh` runs `dev_env/dataspoke-postgres/build.sh` automatically
unless `SKIP_POSTGRES_BUILD=1`.

| Component | Type | Mem Limit | PV |
|-----------|------|-----------|-----|
| dataspoke-api | Deployment | 1 Gi | — |
| airflow (api-server + scheduler + triggerer + dag-processor) | Deployment + StatefulSets | 4 × 1 Gi + 3 × 512 Mi logGroomer sidecars ≈ 5.5 Gi | — |
| postgresql | StatefulSet | 4 Gi | 10 Gi |
| redis | Deployment | 512 Mi | — |

> **Airflow 3.x note**: `dag-processor` is a standalone Airflow 3.x component (not present in 2.x).

> **Airflow DAGs are baked into a custom image.** The chart pulls
> `${DATASPOKE_DEV_IMAGE_REGISTRY}/airflow:dev` (built by
> `dev_env/dataspoke-airflow/build.sh` from `docker-images/airflow/Dockerfile`,
> which does `FROM apache/airflow:3.1.8-python3.13` + `COPY src/workflows/dags/
> /opt/airflow/dags/`). `dev_env/dataspoke-infra/install.sh` runs the build
> automatically unless `SKIP_AIRFLOW_BUILD=1`. No PVC, no gitSync. Updating a
> DAG requires a rebuild + `kubectl rollout restart` of the Airflow workloads.

> **API image is built from source.** `dev_env/dataspoke-api/build.sh` builds
> the `api:dev` image from `docker-images/api/Dockerfile`. It is invoked by
> `dev_env/dataspoke-test-mode.sh` (use `--skip-build` to reuse the existing
> image). Code changes to `src/api`, `src/backend`, or `src/shared` require a
> rebuild + `helm upgrade`, both of which `dataspoke-test-mode.sh` runs end-to-end.

**Kubernetes Secrets** (created by `dataspoke-infra/install.sh` before Helm install):

| Secret Name | Keys |
|-------------|------|
| `dataspoke-postgres-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `dataspoke-redis-secret` | `REDIS_PASSWORD` |
| `dataspoke-internal-auth` | `token` — auto-generated shared secret for Airflow → API internal calls |

> LLM secrets are not deployed into the cluster. The host-running app reads them directly from
> `.env`.

---

### Example Data Sources

Plain Kubernetes manifests (no Helm) in the `dataspoke-dummy-data-01` namespace.

| Component | Image | Mem Limit | Storage | Service |
|-----------|-------|-----------|---------|---------|
| PostgreSQL | `postgres:15` | 512 Mi | 5 Gi PVC | `example-postgres:5432` |
| Kafka | `apache/kafka:3.9.0` (KRaft) | 512 Mi | 4 Gi PVC | `example-kafka:9092` (internal), `:9094` (EXTERNAL) |

This Kafka instance is **separate** from DataHub's prerequisites Kafka. It simulates an external
data source for ingestion testing. Like DataHub Kafka, it exposes an EXTERNAL listener (port 9094)
that advertises `<INGRESS_IP>:9104` for host-side access via TCP passthrough on the nginx-ingress
controller.

---

### Lock Service

Advisory mutex for coordinating multi-tester access. Lightweight Python HTTP server in the
`dataspoke-01` namespace (pure stdlib, no external dependencies).

| Resource | Details |
|----------|---------|
| Deployment | `dev-lock` — 1 replica, `python:3.13-slim`, 64 Mi / 100m CPU |
| Service | `dev-lock` — ClusterIP, port 8080 |

Lock state is **in-memory only** — resets on pod restart. Full protocol in
[TESTING.md §Integration Testing](../TESTING.md#integration-testing); HTTP API surface (GET/POST
acquire/release + DELETE force-release) documented in
[`dev_env/README.md §Lock service`](../../dev_env/README.md).

---

## Install & Uninstall

### install.sh

Top-level orchestrator: sources `.env`, verifies `kubectl`/`helm`, switches kube context, creates
namespaces (including `langfuse-01`), then calls sub-installers in order: `nginx-ingress/` →
`datahub/` → `langfuse/` → `dataspoke-infra/` → `dataspoke-example/` → `dataspoke-lock/`.
Langfuse runs before `dataspoke-infra` so the umbrella chart's API/Airflow pods find
`dataspoke-langfuse-secret` in `dataspoke-01` on their first start.
Prints ingress endpoint summary on completion.

| Flag | Effect |
|------|--------|
| `--from-component <name>` | Skip components before `<name>`; resume an interrupted install from a known starting point in the dependency-ordered sequence |
| `--help`, `-h` | Print usage and the component list |

### uninstall.sh

Reverse order: `dataspoke-lock/` → `dataspoke-example/` → `dataspoke-infra/` → `langfuse/` →
`datahub/` → `nginx-ingress/`. Prompts before destructive operations.

| Flag | Effect |
|------|--------|
| `--yes` | Skip "remove all resources?" confirmation |
| `--delete-namespaces` | Also delete the four namespaces |

### Component reinstall

There is no dedicated `reinstall.sh`. For application-code iteration cycles
(rebuild image + `helm upgrade` + DAG verification), use
`dev_env/dataspoke-test-mode.sh` — see [TESTING.md §Testing Modes](../TESTING.md#testing-modes).
To reset a single infrastructure component cleanly, run its own
`uninstall.sh` followed by `install.sh` (each sub-installer is idempotent and tears down PVCs +
Helm release for its scope). Example:
`cd dev_env && bash dataspoke-infra/uninstall.sh && bash dataspoke-infra/install.sh`.

To resume an interrupted full install rather than reset a single component, use
`./install.sh --from-component <name>` — it skips earlier components and runs the remainder in
dependency order.

### Shell conventions

All scripts use `#!/usr/bin/env bash`, `set -euo pipefail`, and source shared helpers from
`lib/helpers.sh`. All mutating kubectl/helm operations are idempotent.

---

## Ingress

All services are reached via a single nginx-ingress controller in the `ingress-nginx` namespace.
The controller acquires one external LoadBalancer IP (`DATASPOKE_DEV_INGRESS_IP`) written to
`dev_env/.env` by `nginx-ingress/install.sh`.

Two tiers:

- **Tier A — HTTP virtual hosts** on port 80, keyed by hostname (DataHub UI/GMS at
  `datahub.<IP>.nip.io`, DataSpoke API at `app.<IP>.nip.io/api/v1/`, Airflow UI at
  `airflow.<IP>.nip.io`). The `nip.io` suffix gives automatic wildcard DNS — no `/etc/hosts`
  entries required.
- **Tier B — TCP passthrough** on dedicated ports (9201 PostgreSQL, 9202 Redis, 9005 DataHub
  Kafka, 9102 example PostgreSQL, 9104 example Kafka, 9221 lock service) via the same
  LoadBalancer IP, mapped by the nginx-ingress `tcp-services` ConfigMap. Kafka services advertise
  `<INGRESS_IP>:<port>` as their EXTERNAL listener so host-side producers/consumers reach them
  through the ingress. `DATASPOKE_*_HOST/PORT` app runtime variables in `.env` point to these
  addresses.

Full endpoint table (service ↔ cluster address ↔ ingress URL/port) is the operational reference
in [`dev_env/README.md §Ingress Endpoints`](../../dev_env/README.md).

---

## Dummy Data

The `dataspoke-dummy-data-01` namespace provides example PostgreSQL and Kafka instances populated
with Imazon use-case data (11 schemas, 17 tables, ~600 rows; 3 Kafka topics, ~45 messages). Both
PG tables and Kafka topics are registered as DataHub dataset entities (20 total) with
`DatasetProperties` and `SchemaMetadata` aspects. Seed files, ingestion logic, and data design
details live in `tests/integration/util/` — see
[`TESTING.md §Test Data Design`](../TESTING.md#test-data-design) for the full reference.

---

## Resource Budget

Cluster capacity: **8 CPU / 24 GB RAM / 150 GB storage**. Sum of memory *limits* (~25 GiB) exceeds
cluster capacity; sum of *requests* (~13 GiB) does not — pods rarely hit limits simultaneously, so
limits are set generously to absorb transient spikes (OpenSearch off-heap, `mysql_upgrade`, JVM
GC, etc.).

GKE Autopilot applies a **1 GiB ephemeral-storage request = 1 GiB limit** per container whenever
the container spec omits `resources.{requests,limits}.ephemeral-storage`. Autopilot counts
writable-layer writes, container stdout/stderr logs, emptyDir volume writes (including Airflow's
default `/opt/airflow/logs` emptyDir), and projected-volume mounts against this limit. Chatty
containers — those that produce sustained stdout or write temporary files to emptyDir — exhaust the
1 GiB default within minutes and trigger pod eviction with the message
`Pod ephemeral local storage usage exceeds the total limit of containers`. Explicit
`ephemeral-storage` limits in the budget table below prevent this class of eviction on Autopilot.
Low-log containers (dev-lock, redis, frontend, Airflow logGroomer sidecars) are left at the
Autopilot default — they do not generate sustained log volume and the extra cluster reservation
is not justified.

Autopilot's resource webhook **forces `requests == limits` for ephemeral-storage** at admission
time. Helm values that specify a higher `limits.ephemeral-storage` than `requests.ephemeral-storage`
are silently normalized — the effective limit equals the request. The budget table below lists the
configured limit; on Autopilot the running pod will show that same number for both fields. If
operators need a higher effective limit on Autopilot, raise the request to match.

### Memory Budget (limits)

| Component | Namespace | Mem Limit | Notes |
|-----------|-----------|-----------|-------|
| OpenSearch | datahub-01 | 3072 Mi | 1 Gi JVM heap + off-heap cache |
| Kafka (KRaft controller) | datahub-01 | 2048 Mi | 1.5 Gi heap cap; single pod carries both controller and broker roles |
| MySQL (bitnami) | datahub-01 | 1536 Mi | `mysql_upgrade` doubles memory |
| datahub-gms | datahub-01 | 3 Gi | |
| datahub-frontend | datahub-01 | 1 Gi | |
| datahub-mae-consumer | datahub-01 | 1 Gi | |
| datahub-mce-consumer | datahub-01 | 1 Gi | |
| datahub-actions | datahub-01 | 512 Mi | |
| dataspoke-api | dataspoke-01 | 1 Gi | In-cluster API deployment |
| airflow (api-server + scheduler + triggerer + dag-processor) | dataspoke-01 | 4 × 1 Gi + 3 × 512 Mi logGroomers ≈ 5.5 Gi | Airflow 3.1 LocalExecutor; DAGs baked into `${REGISTRY}/airflow:dev` (built from `docker-images/airflow/Dockerfile`) |
| postgresql (dataspoke) | dataspoke-01 | 4096 Mi | Custom image with pgvector + AGE; built from `docker-images/postgres/Dockerfile` |
| redis | dataspoke-01 | 512 Mi | |
| dev-lock | dataspoke-01 | 64 Mi | |
| example-postgres | dataspoke-dummy-data-01 | 512 Mi | |
| example-kafka | dataspoke-dummy-data-01 | 1024 Mi | 4 Gi PVC (bumped from 1 Gi — broker storage was undersized) |
| **Total (limits)** | | **~25 Gi** | |

### CPU Budget (limits)

~19 CPU total limits across all components. Pods rarely hit limits simultaneously. Explicit
limits prevent starvation on constrained dev clusters. See
`dev_env/datahub/prerequisites-values.yaml` and `helm-charts/dataspoke/values-dev.yaml` for
per-component breakdown.

### Ephemeral Storage Budget (limits)

| Component | Namespace | Limit | Notes |
|-----------|-----------|-------|-------|
| datahub-gms | datahub-01 | 8 Gi | High-log: GMS emits continuous trace logs via Kafka listeners |
| datahub-frontend | datahub-01 | 8 Gi | High-log: Play framework access log per request |
| datahub-mae-consumer | datahub-01 | 8 Gi | High-log: processes every metadata aspect change event |
| datahub-mce-consumer | datahub-01 | 8 Gi | High-log: processes every metadata change proposal |
| datahub-actions | datahub-01 | 4 Gi | Medium-log: event-driven actions framework |
| Kafka KRaft controller | datahub-01 | 4 Gi | Medium-log: broker + controller log segments accumulate in stdout |
| OpenSearch | datahub-01 | 4 Gi | Medium-log: JVM GC + index recovery logs |
| MySQL | datahub-01 | 4 Gi | Medium-log: slow-query and binary log references appear in stdout |
| airflow-api-server | dataspoke-01 | 8 Gi | High-log: FastAPI/uvicorn access log per request |
| airflow-scheduler | dataspoke-01 | 8 Gi | High-log: continuous heartbeat + task scheduling logs |
| airflow-triggerer | dataspoke-01 | 8 Gi | High-log: continuous event-loop logs |
| airflow-dag-processor | dataspoke-01 | 8 Gi | High-log: DAG parsing cycle emits one log line per DAG per interval |
| dataspoke-api | dataspoke-01 | 4 Gi | Medium-log: FastAPI/uvicorn access log per request |
| postgresql (dataspoke) | dataspoke-01 | 4 Gi | Medium-log: WAL and autovacuum progress in stdout |
| example-kafka | dataspoke-dummy-data-01 | 4 Gi | Medium-log: KRaft broker logs |
| example-postgres | dataspoke-dummy-data-01 | 4 Gi | Medium-log: WAL and checkpoint progress in stdout |

---

## Troubleshooting

### Pod evicted: ephemeral local storage usage exceeds limit

**Cause**: GKE Autopilot applies a 1 GiB ephemeral-storage default per container. Chatty
containers (GMS, Airflow scheduler/triggerer/dag-processor, MAE/MCE consumers, etc.) exhaust this
limit within minutes from stdout log accumulation and Airflow's default emptyDir log volume.
**Fix**: Ensure the affected container has an explicit `ephemeral-storage` entry in its
`resources.requests` and `resources.limits` blocks per the §Resource Budget §Ephemeral Storage
Budget table above. If the evicted container is not in the table (i.e., is expected to be
low-log), verify it is not writing unexpectedly large logs and add it to the table if needed.

### OpenSearch OOM-killed during startup

**Cause**: Off-heap usage (Lucene cache, index recovery) spikes above the JVM heap. Upstream
default 1024Mi is insufficient.
**Fix**: Already applied — OpenSearch memory limit set to 3Gi in `prerequisites-values.yaml`.

### MySQL OOM-killed on restart

**Cause**: `mysql_upgrade` runs on every start, briefly doubling memory beyond the chart default.
**Fix**: Already applied — MySQL memory limit set to 1536Mi in `prerequisites-values.yaml`.

### Pod stuck in Pending

**Cause**: Insufficient cluster resources.
**Fix**: Check `kubectl describe node`. The full environment's memory *requests* sum
to ~13 GiB and *limits* to ~25 GiB (see §Resource Budget) on top of ~3.5 CPU; 24 GB /
8+ CPU is the recommended cluster headroom.

### datahub-system-update takes 5-10 minutes

**Cause**: Expected on first install — bootstraps all DataHub metadata schemas.
**Fix**: Wait. The script polls every 10s with progress logging.

### MAE consumer stalled after restart

**Cause**: The embedded MAE consumer in GMS crashes when processing stale MCL messages accumulated
from previous runs. The Spring Kafka error handler shuts down the consumer permanently, leaving
timeseries aspects unindexed in OpenSearch.
**Fix**: Already automated in `datahub/install.sh` — detects stalled consumer group, resets
offsets to latest, and restarts GMS. If it recurs outside install, manually reset offsets on
`MetadataChangeLog_Timeseries_v1` and `MetadataChangeLog_Versioned_v1` for group
`generic-mae-consumer-job-client`, then restart the GMS pod.

### Service unreachable via ingress

**Cause**: Target pod not yet Ready, or the nginx-ingress controller has not yet received an
external IP.
**Fix**: Verify the ingress controller is running (`kubectl get pods -n ingress-nginx`) and has an
external IP (`kubectl get svc -n ingress-nginx`). Then verify the target pod is `1/1 Running` in
its namespace. Re-run `./dev_env/health-check.sh` once pods are ready.

---

## Open Questions

- [ ] When DataSpoke exposes a redefined dataset registration API (blended API/UI),
  `tests/integration/util/datahub.py` could be replaced by calls to that API for integration test
  setup. This would simplify the test workflow and exercise the redefined API as part of every
  test run.

---

## References

- [DataHub — Deploying with Kubernetes](https://docs.datahub.com/docs/deploy/kubernetes) —
  minimum: 2 CPUs, 8 GB RAM
- [DataHub Helm chart defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/datahub/values.yaml)
- [DataHub prerequisites defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/prerequisites/values.yaml)
- [Migrating Graph Service Implementation](https://docs.datahub.com/docs/how/migrating-graph-service-implementation)
- [HELM_CHART.md](HELM_CHART.md) — DataSpoke umbrella Helm chart specification
- [TESTING.md](../TESTING.md) — Testing conventions and dev-env lock protocol
