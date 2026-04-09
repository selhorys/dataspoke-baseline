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

`helm-charts/dataspoke/` is an **umbrella Helm chart** that packages all DataSpoke components — application services and infrastructure dependencies — into a single installable unit. The same chart serves both production and development — only the values file differs:

- **Production** (`values.yaml`): All components enabled — frontend, API, plus infrastructure (including Kestra). Deploy with `helm upgrade --install` and a customized values file for your environment.
- **Dev** (`values-dev.yaml`): Infrastructure + API server, reduced resources. Used by `dev_env/dataspoke-infra/install.sh`. The API runs in-cluster so Kestra can reach it directly. Frontend and workers are disabled.

```
Production Deployment                    Dev Deployment (dev_env)
┌────────────────────────┐              ┌────────────────────────┐
│  dataspoke namespace   │              │  dataspoke namespace   │
│                        │              │  (infra + api)         │
│  frontend  ✓           │              │  frontend  ✗           │
│  api       ✓           │              │  api       ✓           │
│  event-consumer (opt)  │              │  event-consumer ✗      │
│  kestra    ✓           │              │  kestra    ✓           │
│  qdrant    ✓           │              │  qdrant    ✓           │
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

`helm-charts/dataspoke/` is a standard Helm umbrella chart with `Chart.yaml` (apiVersion v2), `values.yaml` (production), `values-dev.yaml` (dev overlay), `templates/` (configmap, secrets, networkpolicy, helpers), three application `subcharts/` (frontend, api, event-consumer), and `charts/` (fetched dependency archives).

### Dependencies

| Subchart | Source | Version | Condition |
|----------|--------|---------|-----------|
| frontend | `file://subcharts/frontend` | 0.1.0 | `frontend.enabled` |
| api | `file://subcharts/api` | 0.1.0 | `api.enabled` |
| event-consumer | `file://subcharts/event-consumer` | 0.1.0 | `event-consumer.enabled` |
| postgresql | `bitnami/postgresql` | ~18.5.0 | `postgresql.enabled` |
| redis | `bitnami/redis` | ~25.3.0 | `redis.enabled` |
| qdrant | `qdrant/qdrant` | ~1.17.0 | `qdrant.enabled` |
| kestra | `kestra/kestra` | ~1.0.42 | `kestra.enabled` |

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
| qdrant | StatefulSet | enabled | enabled | yes (PV) |
| kestra | Deployment | enabled | enabled | no (uses PG) |

Each component has a `<component>.enabled` toggle in values.

---

## Configuration Flow

Application runtime configuration (`DATASPOKE_*` variables) flows through Helm values into containers:

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

Non-sensitive: `DATASPOKE_DATAHUB_GMS_URL`, `DATASPOKE_DATAHUB_KAFKA_BROKERS`, `DATASPOKE_POSTGRES_HOST/PORT/DB`, `DATASPOKE_REDIS_HOST/PORT`, `DATASPOKE_QDRANT_HOST/HTTP_PORT/GRPC_PORT`, `DATASPOKE_KESTRA_HOST/PORT`, `DATASPOKE_LLM_PROVIDER/MODEL`.

### Secret keys

Sensitive: `DATASPOKE_DATAHUB_TOKEN`, `DATASPOKE_POSTGRES_USER/PASSWORD`, `DATASPOKE_REDIS_PASSWORD`, `DATASPOKE_QDRANT_API_KEY`, `DATASPOKE_LLM_API_KEY`.

All application subcharts mount both resources via `envFrom`. In dev, ConfigMap/Secret creation is disabled (`createConfigMap: false`, `createSecret: false`) — the host-running app reads env vars directly from `dev_env/.env`.

---

## Value Profiles

### Production (`values.yaml`)

- All components enabled with multiple replicas for frontend/API
- PV persistence for PostgreSQL (50 Gi) and Qdrant (50 Gi)
- Ingress enabled for frontend and API (nginx class, cert-manager TLS)
- NetworkPolicy for DataHub cross-namespace egress (disabled by default)
- Kestra uses parent chart's PostgreSQL for persistence
- Kestra UI enabled (served on same port as API)

### Dev (`values-dev.yaml`)

- API enabled in-cluster (1 replica, `testMode: true`); frontend/workers disabled
- Single replicas, reduced resource limits
- Kestra minimized for dev: reduced resources, single port (8080) for API + UI
- Redis replicas set to 0
- ConfigMap/Secret created for in-cluster API env vars

### Key design decisions

- **Kestra persistence**: Kestra reuses the parent chart's PostgreSQL instance rather than deploying its own datastore.
- **Profile switching**: Dev and production use the same chart — only the values file differs. `dev_env/dataspoke-infra/install.sh` is a thin wrapper that creates K8s secrets from `.env` and runs `helm upgrade --install` with `values-dev.yaml`.
- **Event consumer separation**: The Kafka event consumer can optionally be deployed as a standalone pod (`event-consumer.enabled`), separate from the API deployment. By default, both processes are co-located in the `api` deployment. Enable the event-consumer subchart for independent scaling and fault isolation in production — Kafka consumers scale by partition count. When `event-consumer.enabled=true`, the API deployment should disable its embedded consumer via `DATASPOKE_KAFKA_CONSUMER_ENABLED=false`.

---

## Secrets Management

### Dev

Secrets come from `dev_env/.env`. The install script creates K8s Secrets before the Helm install:

| Secret | Keys |
|--------|------|
| `dataspoke-postgres-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `dataspoke-redis-secret` | `REDIS_PASSWORD` |
| `dataspoke-qdrant-secret` | `QDRANT_API_KEY` (only if non-empty) |

Infrastructure subcharts reference these via `auth.existingSecret`.

### Production

Two approaches:

- **Option A**: Inject via `helm upgrade --set secrets.*` or a sealed values file.
- **Option B** (recommended): Use [External Secrets Operator](https://external-secrets.io/) to sync from AWS Secrets Manager, Vault, or GCP Secret Manager. Set `secrets.createSecret: false` and reference the externally-managed secret.

---

## Resource Sizing

### Production Defaults

| Component | Replicas | CPU Req / Limit | Mem Req / Limit | PV |
|-----------|---------|-----------------|-----------------|-----|
| frontend | 2 | 250m / 500m | 256Mi / 512Mi | — |
| api | 2 | 500m / 1000m | 512Mi / 1024Mi | — |
| event-consumer† | 1 | 250m / 500m | 512Mi / 1024Mi | — |
| postgresql | 1 | 500m / 1000m | 1024Mi / 2048Mi | 50Gi |
| redis | 1+1 | 250m / 500m | 256Mi / 512Mi | — |
| qdrant | 1 | 500m / 1000m | 1024Mi / 2048Mi | 50Gi |
| kestra | 1 | 500m / 1000m | 1024Mi / 2048Mi | — |
| **Total** | | **~5500m / ~11000m** | **~8.5Gi / ~17Gi** | **100Gi** |

† event-consumer is disabled by default — totals above exclude it. When enabled, add ~250m/500m CPU and ~512Mi/1024Mi memory. Kestra handles execution internally — no separate worker deployment needed.

### Dev Minimums

See [DEV_ENV.md §Resource Budget](DEV_ENV.md#resource-budget). The dev profile uses ~7.9 Gi memory limits / ~3.5 CPU limits for DataSpoke infrastructure alone.

---

## Ingress & Network Policy

### Ingress

Frontend, API, and Kestra each have an `ingress` section in their values supporting:
- `className` (nginx, alb, traefik, etc.)
- TLS via cert-manager annotations
- Customizable host and path rules

In dev, ingress is enabled via `values-dev.yaml` — the nginx-ingress controller (installed separately in `ingress-nginx` namespace) routes traffic to all services. Key ingress resources:

| Resource | Location | Routes |
|----------|----------|--------|
| `templates/api-ingress.yaml` | umbrella chart | `app.<INGRESS_IP>.nip.io/api` → `dataspoke-api:8002` |
| `subcharts/frontend/templates/ingress.yaml` | frontend subchart | `app.<INGRESS_IP>.nip.io/` → `dataspoke-frontend:3000` |
| `kestra.ingress` values | kestra chart (native) | `kestra.<INGRESS_IP>.nip.io/` → `dataspoke-kestra:8080` |
| `dev_env/datahub/gms-ingress.yaml` | kubectl manifest | `datahub.<INGRESS_IP>.nip.io/gms` → `datahub-datahub-gms:8080` |
| `datahub-frontend.ingress` values | DataHub chart (native) | `datahub.<INGRESS_IP>.nip.io/` → `datahub-frontend:9002` |

TCP passthrough (PostgreSQL, Redis, Qdrant, Kafka, Lock) is handled by the nginx-ingress `tcp-services` ConfigMap — no Ingress resource needed for TCP. See [`DEV_ENV.md §Ingress`](DEV_ENV.md#ingress) for the full port map.

### Network Policy

A NetworkPolicy template allows egress from DataSpoke pods to the DataHub namespace (GMS :8080, Kafka :9092). Controlled by `networkPolicy.enabled` (default: `false`) and `networkPolicy.datahubNamespace`. Enable in production clusters with default-deny policies.

---

## Dev Environment Integration

`dev_env/dataspoke-infra/install.sh` consumes this chart with the dev profile. The install flow:

1. Create K8s Secrets from `.env` variables (idempotent via `--dry-run=client`)
2. Register Helm repos (`bitnami`, `qdrant`, `kestra`) and build chart dependencies
3. `helm upgrade --install dataspoke` with `values-dev.yaml`, passing PostgreSQL auth credentials via `--set`

The dev profile enables the API in-cluster (so Kestra callbacks work via cluster DNS) and enables ingress for the API and Kestra (so developers can access them via the nginx-ingress endpoints). Frontend and workers remain disabled. Kestra runs in the cluster in both dev and production.

This means:
1. The umbrella chart is the **single source of truth** for DataSpoke Kubernetes deployments — both production and dev
2. `dev_env/dataspoke-infra/` is a thin wrapper — no duplicate values files or templates
3. Switching from dev to production is changing the values file, not the chart

---

## In-Cluster Testing

For on-demand integration testing where all components run inside Kubernetes (e.g., verifying health probes, ingress routing, network policies, or resource behavior), enable application subcharts on top of the dev profile:

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

The API is already enabled in `values-dev.yaml`. Frontend and workers can be enabled on-demand for full in-cluster testing. Every code change requires a container rebuild and `helm upgrade` — this is automated by `dev_env/dataspoke-test-mode.sh`. See [TESTING.md §Testing Modes](../TESTING.md#testing-modes).

---

## References

- [Helm — Chart Dependencies](https://helm.sh/docs/helm/helm_dependency/) — umbrella chart pattern
- [Bitnami PostgreSQL Chart](https://github.com/bitnami/charts/tree/main/bitnami/postgresql)
- [Bitnami Redis Chart](https://github.com/bitnami/charts/tree/main/bitnami/redis)
- [Qdrant Helm Chart](https://github.com/qdrant/qdrant-helm)
- [Kestra Helm Chart](https://github.com/kestra-io/helm-charts)
- [External Secrets Operator](https://external-secrets.io/) — production secrets management
- [DEV_ENV.md](DEV_ENV.md) — Development environment specification
- [ARCHITECTURE.md](../ARCHITECTURE.md) — System architecture and deployment topology
