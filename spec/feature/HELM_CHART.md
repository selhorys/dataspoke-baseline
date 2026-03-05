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

`helm-charts/dataspoke/` is an **umbrella Helm chart** that packages all DataSpoke components — application services and infrastructure dependencies — into a single installable unit. Two deployment profiles:

- **Production** (`values.yaml`): All components enabled — frontend, API, workers, plus infrastructure.
- **Dev** (`values-dev.yaml`): Infrastructure only — application subcharts disabled, reduced resources. Used by `dev_env/dataspoke-infra/install.sh`.

```
Production Deployment                    Dev Deployment (dev_env)
┌────────────────────────┐              ┌────────────────────────┐
│  dataspoke namespace   │              │  dataspoke namespace   │
│                        │              │  (infra only)          │
│  frontend  ✓           │              │  frontend  ✗           │
│  api       ✓           │              │  api       ✗           │
│  workers   ✓           │              │  workers   ✗           │
│  temporal  ✓           │              │  temporal  ✓           │
│  qdrant    ✓           │              │  qdrant    ✓           │
│  postgresql ✓          │              │  postgresql ✓          │
│  redis     ✓           │              │  redis     ✓           │
└────────────────────────┘              └────────────────────────┘
                                           ▲
                                           │ port-forward
                                        ┌──┴─────────────────┐
                                        │ Host               │
                                        │ frontend (npm dev) │
                                        │ api (uvicorn)      │
                                        │ workers (python)   │
                                        └────────────────────┘
```

---

## Chart Structure

```
helm-charts/dataspoke/
├── Chart.yaml                  # Umbrella chart (apiVersion: v2, type: application)
├── Chart.lock                  # Locked dependency versions
├── values.yaml                 # Production defaults (all components enabled)
├── values-dev.yaml             # Dev overlay: infra only, reduced resources
├── templates/
│   ├── _helpers.tpl            # Common template helpers (labels, names, selectors)
│   ├── configmap.yaml          # DATASPOKE_* app config from .Values.config
│   ├── secrets.yaml            # Sensitive DATASPOKE_* vars from .Values.secrets
│   └── networkpolicy.yaml      # Cross-namespace egress to DataHub
├── subcharts/
│   ├── frontend/               # Next.js — Deployment + Service + Ingress
│   ├── api/                    # FastAPI — Deployment + Service + Ingress
│   └── workers/                # Temporal worker — Deployment only (no service)
└── charts/                     # Fetched subchart archives (helm dep update)
```

### Dependencies

| Subchart | Source | Version | Condition |
|----------|--------|---------|-----------|
| frontend | `file://subcharts/frontend` | 0.1.0 | `frontend.enabled` |
| api | `file://subcharts/api` | 0.1.0 | `api.enabled` |
| workers | `file://subcharts/workers` | 0.1.0 | `workers.enabled` |
| postgresql | `bitnami/postgresql` | ~18.5.0 | `postgresql.enabled` |
| redis | `bitnami/redis` | ~25.3.0 | `redis.enabled` |
| qdrant | `qdrant/qdrant` | ~1.17.0 | `qdrant.enabled` |
| temporal | `temporalio/temporal` | ~0.73.0 | `temporal.enabled` |

Tilde ranges allow patch-level updates. Exact resolved versions are locked in `Chart.lock`.

---

## Component Matrix

| Component | Type | Prod | Dev | Stateful |
|-----------|------|------|-----|----------|
| frontend | Deployment | enabled | **disabled** | no |
| api | Deployment | enabled | **disabled** | no |
| workers | Deployment | enabled | **disabled** | no |
| postgresql | StatefulSet | enabled | enabled | yes (PV) |
| redis | Deployment | enabled | enabled | no |
| qdrant | StatefulSet | enabled | enabled | yes (PV) |
| temporal | Deployment | enabled | enabled | no (uses PG) |

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

Non-sensitive: `DATASPOKE_DATAHUB_GMS_URL`, `DATASPOKE_DATAHUB_KAFKA_BROKERS`, `DATASPOKE_POSTGRES_HOST/PORT/DB`, `DATASPOKE_REDIS_HOST/PORT`, `DATASPOKE_QDRANT_HOST/HTTP_PORT/GRPC_PORT`, `DATASPOKE_TEMPORAL_HOST/PORT/NAMESPACE`, `DATASPOKE_LLM_PROVIDER/MODEL`.

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
- Temporal uses parent chart's PostgreSQL for persistence (Cassandra/MySQL/internal PG disabled)
- Temporal Web UI enabled

### Dev (`values-dev.yaml`)

- Application subcharts disabled — developers run them on the host
- Single replicas, reduced resource limits
- PostgreSQL initdb creates Temporal databases (`temporal`, `temporal_visibility`)
- Temporal schema setup/migration jobs enabled; Web UI disabled
- Redis replicas set to 0
- ConfigMap/Secret not created

### Key design decisions

- **Temporal persistence**: Temporal reuses the parent chart's PostgreSQL instance rather than deploying its own datastore. The dev profile creates the required databases via PostgreSQL `initdb` scripts. Temporal persistence credentials are injected at install time via `--set`.
- **Profile switching**: Dev and production use the same chart — only the values file differs. `dev_env/dataspoke-infra/install.sh` is a thin wrapper that creates K8s secrets from `.env` and runs `helm upgrade --install` with `values-dev.yaml`.

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
| workers | 2 | 500m / 1000m | 1024Mi / 2048Mi | — |
| postgresql | 1 | 500m / 1000m | 1024Mi / 2048Mi | 50Gi |
| redis | 1+1 | 250m / 500m | 256Mi / 512Mi | — |
| qdrant | 1 | 500m / 1000m | 1024Mi / 2048Mi | 50Gi |
| temporal | 1 | 500m / 1000m | 1024Mi / 2048Mi | — |
| **Total** | | **~5500m / ~11000m** | **~8.5Gi / ~17Gi** | **100Gi** |

### Dev Minimums

See [DEV_ENV.md §Resource Budget](DEV_ENV.md#resource-budget). The dev profile uses ~2.8 Gi memory limits / ~1.75 CPU limits for DataSpoke infrastructure alone.

---

## Ingress & Network Policy

### Ingress

Frontend and API each have an `ingress` section in their subchart values supporting:
- `className` (nginx, alb, traefik, etc.)
- TLS via cert-manager annotations
- Customizable host and path rules

In dev, ingress is disabled — services are accessed via port-forward.

### Network Policy

A NetworkPolicy template allows egress from DataSpoke pods to the DataHub namespace (GMS :8080, Kafka :9092). Controlled by `networkPolicy.enabled` (default: `false`) and `networkPolicy.datahubNamespace`. Enable in production clusters with default-deny policies.

---

## Dev Environment Integration

`dev_env/dataspoke-infra/install.sh` consumes this chart with the dev profile. The install flow:

1. Create K8s Secrets from `.env` variables (idempotent via `--dry-run=client`)
2. Register Helm repos (`bitnami`, `qdrant`, `temporal`) and build chart dependencies
3. `helm upgrade --install dataspoke` with `values-dev.yaml`, passing PostgreSQL auth and Temporal persistence credentials via `--set`

This means:
1. The umbrella chart is the **single source of truth** for DataSpoke Kubernetes deployments
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
  --set workers.enabled=true \
  --set config.createConfigMap=true \
  --set secrets.createSecret=true
```

This is **not** the default development workflow — every code change requires a container rebuild and `helm upgrade`. Use only when the user explicitly requests it. For normal development, run application services on the host and connect to port-forwarded infrastructure. See [TESTING.md §Testing Modes](../TESTING.md#testing-modes).

---

## References

- [Helm — Chart Dependencies](https://helm.sh/docs/helm/helm_dependency/) — umbrella chart pattern
- [Bitnami PostgreSQL Chart](https://github.com/bitnami/charts/tree/main/bitnami/postgresql)
- [Bitnami Redis Chart](https://github.com/bitnami/charts/tree/main/bitnami/redis)
- [Qdrant Helm Chart](https://github.com/qdrant/qdrant-helm)
- [Temporal Helm Chart](https://github.com/temporalio/helm-charts)
- [External Secrets Operator](https://external-secrets.io/) — production secrets management
- [DEV_ENV.md](DEV_ENV.md) — Development environment specification
- [ARCHITECTURE.md](../ARCHITECTURE.md) — System architecture and deployment topology
