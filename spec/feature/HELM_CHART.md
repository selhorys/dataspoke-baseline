# HELM_CHART — DataSpoke Umbrella Helm Chart

## Table of Contents
1. [Overview](#overview)
2. [Chart Structure](#chart-structure)
3. [Component Matrix](#component-matrix)
4. [Configuration Flow](#configuration-flow)
5. [Value Profiles](#value-profiles)
6. [Secrets Management](#secrets-management)
7. [Resource Sizing](#resource-sizing)
8. [Ingress & Network Policy](#ingress--network-policy)
9. [Dev Environment Integration](#dev-environment-integration)
10. [In-Cluster Testing](#in-cluster-testing)
11. [References](#references)

---

## Overview

`helm-charts/dataspoke/` is an **umbrella Helm chart** that packages all DataSpoke components —
application services and infrastructure dependencies — into a single installable unit. The same
chart serves both production and development — only the values file differs:

- **Production** (`values.yaml`): All components enabled — frontend, API, plus infrastructure
  (including Airflow). Deploy with `helm upgrade --install` and a customized values file for your
  environment.
- **Dev** (`values-dev.yaml`): Infrastructure + API server, reduced resources. Used by
  `dev_env/dataspoke-infra/install.sh`. The API runs in-cluster so Airflow can reach it directly.
  Frontend and workers are disabled.

```
Production Deployment                    Dev Deployment (dev_env)
┌────────────────────────┐              ┌────────────────────────┐
│  dataspoke namespace   │              │  dataspoke namespace   │
│                        │              │  (infra + api)         │
│  frontend  ✓           │              │  frontend  ✗           │
│  api       ✓           │              │  api       ✓           │
│  event-consumer (opt)  │              │  event-consumer ✗      │
│  airflow   ✓           │              │  airflow   ✓           │
│  postgresql ✓          │              │  postgresql ✓          │
│  redis     ✓           │              │  redis     ✓           │
└────────────────────────┘              └────────────────────────┘
                                           ▲
                                           │ nginx-ingress
                                        ┌──┴─────────────────┐
                                        │ Host               │
                                        │ frontend (npm dev) │
                                        │ (api via ingress)  │
                                        └────────────────────┘
```

---

## Chart Structure

`helm-charts/dataspoke/` is a standard Helm umbrella chart with `Chart.yaml` (apiVersion v2),
`values.yaml` (production), `values-dev.yaml` (dev overlay), `templates/` (configmap, secrets,
networkpolicy, helpers), three application `subcharts/` (frontend, api, event-consumer), and
`charts/` (fetched dependency archives).

### Dependencies

| Subchart | Source | Version | Condition |
|----------|--------|---------|-----------|
| frontend | `file://subcharts/frontend` | 0.1.0 | `frontend.enabled` |
| api | `file://subcharts/api` | 0.1.0 | `api.enabled` |
| event-consumer | `file://subcharts/event-consumer` | 0.1.0 | `event-consumer.enabled` |
| postgresql | `bitnami/postgresql` | ~18.5.0 | `postgresql.enabled` |
| redis | `bitnami/redis` | ~25.3.0 | `redis.enabled` |
| airflow | `apache-airflow/airflow` | ~1.20.0 | `airflow.enabled` |

Tilde ranges allow patch-level updates. Exact resolved versions are locked in `Chart.lock`.

---

## Component Matrix

| Component | Type | Prod | Dev | Stateful |
|-----------|------|------|-----|----------|
| frontend | Deployment | enabled | **disabled** | no |
| api | Deployment | enabled | **disabled** | no |
| event-consumer | Deployment | **disabled** | **disabled** | no |
| postgresql | StatefulSet | enabled | enabled | yes (PV) |
| redis | Deployment | enabled | enabled | no |
| airflow | Deployment | enabled | enabled | no (uses PG) |

Each component has a `<component>.enabled` toggle in values.

---

## Configuration Flow

Application runtime configuration (`DATASPOKE_*` variables) flows through Helm values into
containers:

```
.Values.config / .Values.secrets
    │
    ▼
ConfigMap (dataspoke-config)  +  Secret (dataspoke-secrets)
    │
    ▼
Deployment envFrom → container env vars
```

### ConfigMap keys

Non-sensitive: `DATASPOKE_DATAHUB_GMS_URL`, `DATASPOKE_DATAHUB_KAFKA_BROKERS`,
`DATASPOKE_POSTGRES_HOST/PORT/DB`, `DATASPOKE_REDIS_HOST/PORT`, `DATASPOKE_AIRFLOW_HOST/PORT`,
`DATASPOKE_LLM_PROVIDER/MODEL`.

### Secret keys

Sensitive: `DATASPOKE_DATAHUB_TOKEN`, `DATASPOKE_POSTGRES_USER/PASSWORD`,
`DATASPOKE_REDIS_PASSWORD`, `DATASPOKE_LLM_API_KEY`.

All application subcharts mount both resources via `envFrom`. In dev, ConfigMap/Secret creation is
disabled (`createConfigMap: false`, `createSecret: false`) — the host-running app reads env vars
directly from `dev_env/.env`.

---

## Value Profiles

### Production (`values.yaml`)

- All components enabled with multiple replicas for frontend/API
- PV persistence for PostgreSQL (50 Gi — hosts relational tables + pgvector embeddings + AGE
  graph data)
- Ingress enabled for frontend and API (nginx class, cert-manager TLS)
- NetworkPolicy for DataHub cross-namespace egress (disabled by default)
- Airflow uses parent chart's PostgreSQL for metadata DB
- Airflow api-server UI enabled

### Dev (`values-dev.yaml`)

- API enabled in-cluster (1 replica, `testMode: true`); frontend/workers disabled
- Single replicas, reduced resource limits
- Airflow 3.1.8 minimized for dev: reduced resources, LocalExecutor, single api-server instance,
  DAGs baked into a custom image built from `docker-images/airflow/Dockerfile`
  (`FROM apache/airflow:3.1.8-python3.13` + `COPY src/workflows/dags/`)
- Redis replicas set to 0
- ConfigMap/Secret created for in-cluster API env vars

### Key design decisions

- **Airflow metadata DB**: Airflow reuses the parent chart's PostgreSQL instance for its metadata
  database rather than deploying its own datastore.
- **Profile switching**: Dev and production use the same chart — only the values file differs.
  `dev_env/dataspoke-infra/install.sh` is a thin wrapper that creates K8s secrets from `.env` and
  runs `helm upgrade --install` with `values-dev.yaml`.
- **Event consumer is opt-in**: The Kafka event consumer is **disabled by default** in both
  prod and dev — baseline UC1–UC5 are schedule-driven via Airflow tier DAGs and do not
  subscribe to DataHub MCL events (see
  [BACKEND.md §Kafka Consumers](BACKEND.md#kafka-consumers-optional-not-enabled-in-baseline)
  and
  [DATAHUB_INTEGRATION §Event Subscription](../DATAHUB_INTEGRATION.md#event-subscription-optional-not-used-by-baseline)).
  Set `event-consumer.enabled=true` only when extending DataSpoke with custom event-driven
  reactions; the consumer then runs as the `dataspoke-event-consumer` Deployment.

---

## Secrets Management

### Dev

Secrets come from `dev_env/.env`. The install script creates K8s Secrets before the Helm install:

| Secret | Keys |
|--------|------|
| `dataspoke-postgres-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `dataspoke-redis-secret` | `REDIS_PASSWORD` |

Infrastructure subcharts reference these via `auth.existingSecret`.

### Production

Two approaches:

- **Option A**: Inject via `helm upgrade --set secrets.*` or a sealed values file.
- **Option B** (recommended): Use [External Secrets Operator](https://external-secrets.io/) to
  sync from AWS Secrets Manager, Vault, or GCP Secret Manager. Set `secrets.createSecret: false`
  and reference the externally-managed secret.

---

## Resource Sizing

### Production Defaults

| Component | Replicas | CPU Req / Limit | Mem Req / Limit | PV |
|-----------|---------|-----------------|-----------------|-----|
| frontend | 2 | 250m / 500m | 256Mi / 512Mi | — |
| api | 2 | 500m / 1000m | 512Mi / 1024Mi | — |
| event-consumer† | 1 | 250m / 500m | 512Mi / 1024Mi | — |
| postgresql | 1 | 1000m / 2000m | 2048Mi / 6144Mi | 50Gi |
| redis | 1+1 | 250m / 500m | 256Mi / 512Mi | — |
| airflow (api-server + scheduler + triggerer) | 1+1+1 | 250m / 500m | 512Mi / 1024Mi | DAGs baked into a custom image |
| **Total** | | **~5000m / ~10000m** | **~9.5Gi / ~22Gi** | **50Gi** |

† event-consumer is disabled by default — totals above exclude it. When enabled, add ~250m/500m
CPU and ~512Mi/1024Mi memory. Airflow uses LocalExecutor — no separate Celery worker needed.

### Dev Minimums

See [DEV_ENV.md §Resource Budget](DEV_ENV.md#resource-budget). The dev profile uses ~7.9 Gi memory
limits / ~3.5 CPU limits for DataSpoke infrastructure alone.

---

## Ingress & Network Policy

### Ingress

Frontend, API, and Airflow each have an `ingress` section in their values supporting:
- `className` (nginx, alb, traefik, etc.)
- TLS via cert-manager annotations
- Customizable host and path rules

In dev, ingress is enabled via `values-dev.yaml` — the nginx-ingress controller (installed
separately in `ingress-nginx` namespace) routes traffic to all services. Key ingress resources:

| Resource | Location | Routes |
|----------|----------|--------|
| `templates/api-ingress.yaml` | umbrella chart | `app.<INGRESS_IP>.nip.io/api` → `dataspoke-api:8002` |
| `subcharts/frontend/templates/ingress.yaml` | frontend subchart | `app.<INGRESS_IP>.nip.io/` → `dataspoke-frontend:3000` |
| `airflow.ingress` values | airflow chart (native) | `airflow.<INGRESS_IP>.nip.io/` → `dataspoke-airflow-api-server:8080` |
| `dev_env/datahub/gms-ingress.yaml` | kubectl manifest | `datahub.<INGRESS_IP>.nip.io/gms` → `datahub-datahub-gms:8080` |
| `datahub-frontend.ingress` values | DataHub chart (native) | `datahub.<INGRESS_IP>.nip.io/` → `datahub-frontend:9002` |

TCP passthrough (PostgreSQL, Redis, Kafka, Lock) is handled by the nginx-ingress `tcp-services`
ConfigMap — no Ingress resource needed for TCP. See [`DEV_ENV.md §Ingress`](DEV_ENV.md#ingress)
for the full port map.

### Network Policy

A NetworkPolicy template allows egress from DataSpoke pods to the DataHub namespace (GMS :8080,
Kafka :9092). Controlled by `networkPolicy.enabled` (default: `false`) and
`networkPolicy.datahubNamespace`. Enable in production clusters with default-deny policies.

---

## Dev Environment Integration

`dev_env/dataspoke-infra/install.sh` consumes this chart with the dev profile. The install flow:

1. Create K8s Secrets from `.env` variables (idempotent via `--dry-run=client`)
2. Register Helm repos (`bitnami`, `apache-airflow`) and build chart dependencies
3. `helm upgrade --install dataspoke` with `values-dev.yaml`, passing PostgreSQL image and auth
   credentials via `--set`/`--set-string`

The dev profile enables the API in-cluster (so Airflow callbacks work via cluster DNS) and
enables ingress for the API and Airflow (so developers can access them via the nginx-ingress
endpoints). Frontend and workers remain disabled. Airflow runs in the cluster in both dev and
production.

This means:
1. The umbrella chart is the **single source of truth** for DataSpoke Kubernetes deployments —
   both production and dev
2. `dev_env/dataspoke-infra/` is a thin wrapper — no duplicate values files or templates
3. Switching from dev to production is changing the values file, not the chart

---

## In-Cluster Testing

For on-demand integration testing where all components run inside Kubernetes (e.g., verifying
health probes, ingress routing, network policies, or resource behavior), enable application
subcharts on top of the dev profile:

```bash
helm upgrade --install dataspoke ./helm-charts/dataspoke \
  --namespace "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}" \
  --values ./helm-charts/dataspoke/values-dev.yaml \
  --set frontend.enabled=true \
  --set api.enabled=true \
  --set config.createConfigMap=true \
  --set secrets.createSecret=true
  # Optionally add: --set event-consumer.enabled=true
```

The API is already enabled in `values-dev.yaml`. Frontend and workers can be enabled on-demand
for full in-cluster testing. Every code change requires a container rebuild and `helm upgrade` —
this is automated by `dev_env/dataspoke-test-mode.sh`. See
[TESTING.md §Testing Modes](../TESTING.md#testing-modes).

---

## References

- [Helm — Chart Dependencies](https://helm.sh/docs/helm/helm_dependency/) — umbrella chart
  pattern
- [Bitnami PostgreSQL Chart](https://github.com/bitnami/charts/tree/main/bitnami/postgresql)
- [Bitnami Redis Chart](https://github.com/bitnami/charts/tree/main/bitnami/redis)
- [Apache Airflow Helm Chart](https://github.com/apache/airflow/tree/main/chart)
- [External Secrets Operator](https://external-secrets.io/) — production secrets management
- [DEV_ENV.md](DEV_ENV.md) — Development environment specification
- [ARCHITECTURE.md](../ARCHITECTURE.md) — System architecture and deployment topology
