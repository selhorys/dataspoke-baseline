# HELM_CHART — DataSpoke Umbrella Helm Chart

## Table of Contents
1. [Overview](#overview)
2. [Chart Structure](#chart-structure)
3. [Component Matrix](#component-matrix)
4. [Configuration Flow](#configuration-flow)
5. [Value Profiles](#value-profiles)
6. [Secrets Management](#secrets-management)
7. [Resource Sizing](#resource-sizing)
8. [Ingress](#ingress)
9. [Cross-Namespace Access](#cross-namespace-access)
10. [Dev Environment Integration](#dev-environment-integration)
11. [References](#references)

---

## Overview

`helm-charts/dataspoke/` is an **umbrella Helm chart** that packages all DataSpoke components — application services and infrastructure dependencies — into a single installable unit. It supports two deployment profiles:

- **Production** (`values.yaml`): All components enabled — frontend, API, workers, plus infrastructure (PostgreSQL, Redis, Qdrant, Temporal).
- **Dev** (`values-dev.yaml`): Infrastructure only — application subcharts disabled, reduced replicas and resources. Used by `dev_env/dataspoke-infra/install.sh`.

The chart follows the Helm umbrella pattern: each component is a subchart dependency listed in `Chart.yaml`. Top-level values control which subcharts are enabled, their resource allocations, and shared configuration (injected into ConfigMaps/Secrets).

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
├── Chart.yaml                  # type: application, apiVersion: v2
├── Chart.lock                  # Locked dependency versions
├── values.yaml                 # Production defaults (all components enabled)
├── values-dev.yaml             # Dev overlay: infra only, reduced resources
├── templates/
│   ├── _helpers.tpl            # Common template helpers (labels, names, selectors)
│   ├── configmap.yaml          # DATASPOKE_* app config from .Values.config
│   ├── secrets.yaml            # Sensitive DATASPOKE_* vars from .Values.secrets
│   └── networkpolicy.yaml      # Cross-namespace access to DataHub
└── charts/                     # Subchart dependencies (fetched by helm dep update)
```

### Chart.yaml

```yaml
apiVersion: v2
name: dataspoke
description: DataSpoke — sidecar extension for DataHub
type: application
version: 0.1.0
appVersion: "0.1.0"

dependencies:
  # --- Application subcharts (custom) ---
  - name: frontend
    version: "0.1.0"
    repository: "file://subcharts/frontend"
    condition: frontend.enabled

  - name: api
    version: "0.1.0"
    repository: "file://subcharts/api"
    condition: api.enabled

  - name: workers
    version: "0.1.0"
    repository: "file://subcharts/workers"
    condition: workers.enabled

  # --- Infrastructure subcharts (community) ---
  - name: postgresql
    version: "16.x.x"
    repository: "https://charts.bitnami.com/bitnami"
    condition: postgresql.enabled

  - name: redis
    version: "20.x.x"
    repository: "https://charts.bitnami.com/bitnami"
    condition: redis.enabled

  - name: qdrant
    version: "1.x.x"
    repository: "https://qdrant.github.io/qdrant-helm"
    condition: qdrant.enabled

  - name: temporal
    version: "0.x.x"
    repository: "https://go.temporal.io/helm-charts"
    condition: temporal.enabled
```

> **Note**: Version ranges (`16.x.x` etc.) are placeholders. Pin exact versions in `Chart.lock` after initial `helm dep update`.

### Application Subcharts

Custom subcharts for DataSpoke application services live under `helm-charts/dataspoke/subcharts/`:

```
subcharts/
├── frontend/
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml     # Next.js container
│       ├── service.yaml        # ClusterIP :3000
│       └── hpa.yaml            # Optional HPA
├── api/
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml     # FastAPI (uvicorn) container
│       ├── service.yaml        # ClusterIP :8000
│       └── hpa.yaml
└── workers/
    ├── Chart.yaml
    ├── values.yaml
    └── templates/
        └── deployment.yaml     # Temporal worker container (no service — outbound only)
```

All application subcharts mount the shared ConfigMap (`dataspoke-config`) and Secret (`dataspoke-secrets`) as environment variables via `envFrom`.

---

## Component Matrix

| Component | Subchart | Type | Enabled by Default | Dev Profile | Stateful |
|-----------|----------|------|-------------------|-------------|----------|
| frontend | `subcharts/frontend` | Deployment | yes | **disabled** | no |
| api | `subcharts/api` | Deployment | yes | **disabled** | no |
| workers | `subcharts/workers` | Deployment | yes | **disabled** | no |
| postgresql | `bitnami/postgresql` | StatefulSet | yes | yes | yes (PV) |
| redis | `bitnami/redis` | Deployment | yes | yes | no |
| qdrant | `qdrant/qdrant` | StatefulSet | yes | yes | yes (PV) |
| temporal | `temporalio/temporal` | Deployment | yes | yes | no (uses PG) |

### Enable/Disable Toggles

Each component has an `.enabled` flag at the top level of values:

```yaml
# values.yaml (production — all enabled)
frontend:
  enabled: true
api:
  enabled: true
workers:
  enabled: true
postgresql:
  enabled: true
redis:
  enabled: true
qdrant:
  enabled: true
temporal:
  enabled: true
```

---

## Configuration Flow

Application runtime configuration (`DATASPOKE_*` variables) flows from Helm values into containers:

```
Helm values (.Values.config / .Values.secrets)
    │
    ▼
templates/configmap.yaml ──► K8s ConfigMap (dataspoke-config)
templates/secrets.yaml   ──► K8s Secret   (dataspoke-secrets)
    │
    ▼
Deployment envFrom: ──► Container environment variables
```

### ConfigMap (`dataspoke-config`)

Non-sensitive application configuration. Mounted as environment variables by all application subcharts.

```yaml
# templates/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "dataspoke.fullname" . }}-config
data:
  DATASPOKE_DATAHUB_GMS_URL: {{ .Values.config.datahub.gmsUrl | quote }}
  DATASPOKE_DATAHUB_KAFKA_BROKERS: {{ .Values.config.datahub.kafkaBrokers | quote }}
  DATASPOKE_POSTGRES_HOST: {{ .Values.config.postgres.host | quote }}
  DATASPOKE_POSTGRES_PORT: {{ .Values.config.postgres.port | quote }}
  DATASPOKE_POSTGRES_DB: {{ .Values.config.postgres.db | quote }}
  DATASPOKE_REDIS_HOST: {{ .Values.config.redis.host | quote }}
  DATASPOKE_REDIS_PORT: {{ .Values.config.redis.port | quote }}
  DATASPOKE_QDRANT_HOST: {{ .Values.config.qdrant.host | quote }}
  DATASPOKE_QDRANT_HTTP_PORT: {{ .Values.config.qdrant.httpPort | quote }}
  DATASPOKE_QDRANT_GRPC_PORT: {{ .Values.config.qdrant.grpcPort | quote }}
  DATASPOKE_TEMPORAL_HOST: {{ .Values.config.temporal.host | quote }}
  DATASPOKE_TEMPORAL_PORT: {{ .Values.config.temporal.port | quote }}
  DATASPOKE_TEMPORAL_NAMESPACE: {{ .Values.config.temporal.namespace | quote }}
  DATASPOKE_LLM_PROVIDER: {{ .Values.config.llm.provider | quote }}
  DATASPOKE_LLM_MODEL: {{ .Values.config.llm.model | quote }}
```

### Secret (`dataspoke-secrets`)

Sensitive values. Mounted as environment variables by application subcharts.

```yaml
# templates/secrets.yaml
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "dataspoke.fullname" . }}-secrets
type: Opaque
stringData:
  DATASPOKE_DATAHUB_TOKEN: {{ .Values.secrets.datahub.token | quote }}
  DATASPOKE_POSTGRES_USER: {{ .Values.secrets.postgres.user | quote }}
  DATASPOKE_POSTGRES_PASSWORD: {{ .Values.secrets.postgres.password | quote }}
  DATASPOKE_REDIS_PASSWORD: {{ .Values.secrets.redis.password | quote }}
  DATASPOKE_QDRANT_API_KEY: {{ .Values.secrets.qdrant.apiKey | quote }}
  DATASPOKE_LLM_API_KEY: {{ .Values.secrets.llm.apiKey | quote }}
```

### Values Structure

```yaml
# values.yaml — config section (production defaults)
config:
  datahub:
    gmsUrl: "http://datahub-datahub-gms.datahub.svc.cluster.local:8080"
    kafkaBrokers: "datahub-prerequisites-kafka.datahub.svc.cluster.local:9092"
  postgres:
    host: "dataspoke-postgresql"         # In-cluster service name
    port: "5432"
    db: "dataspoke"
  redis:
    host: "dataspoke-redis-master"
    port: "6379"
  qdrant:
    host: "dataspoke-qdrant"
    httpPort: "6333"
    grpcPort: "6334"
  temporal:
    host: "dataspoke-temporal-frontend"
    port: "7233"
    namespace: "dataspoke"
  llm:
    provider: "gemini"
    model: "gemini-2.0-flash"

secrets:
  datahub:
    token: ""          # Personal access token (empty for local dev)
  postgres:
    user: ""           # Set via --set or external secrets
    password: ""
  redis:
    password: ""
  qdrant:
    apiKey: ""
  llm:
    apiKey: ""
```

In production, secrets are never stored in `values.yaml`. They are injected via `--set`, a separate secrets file, or an External Secrets Operator. See [Secrets Management](#secrets-management).

---

## Value Profiles

### Production (`values.yaml`)

All components enabled with production-grade defaults:
- Multiple replicas for frontend and API (HPA enabled)
- Production resource requests/limits
- PV persistence enabled for PostgreSQL and Qdrant
- NetworkPolicy for DataHub cross-namespace access
- Ingress enabled for frontend and API

### Dev (`values-dev.yaml`)

Infrastructure only, minimal resources. Used by `dev_env/dataspoke-infra/install.sh`.

```yaml
# values-dev.yaml — dev overlay
# Application components: disabled (developers run them locally)
frontend:
  enabled: false
api:
  enabled: false
workers:
  enabled: false

# Infrastructure components: enabled with reduced resources
postgresql:
  enabled: true
  primary:
    resources:
      limits:
        memory: 512Mi
        cpu: 500m
      requests:
        memory: 256Mi
        cpu: 100m
    persistence:
      size: 10Gi

redis:
  enabled: true
  master:
    resources:
      limits:
        memory: 256Mi
        cpu: 250m
      requests:
        memory: 128Mi
        cpu: 50m
  replica:
    replicaCount: 0        # No replicas in dev

qdrant:
  enabled: true
  resources:
    limits:
      memory: 1024Mi
      cpu: 500m
    requests:
      memory: 512Mi
      cpu: 100m
  persistence:
    size: 10Gi

temporal:
  enabled: true
  server:
    resources:
      limits:
        memory: 1024Mi
        cpu: 500m
      requests:
        memory: 512Mi
        cpu: 100m
    replicaCount: 1
  # Temporal uses PostgreSQL for persistence — reuse dataspoke-postgresql
  cassandra:
    enabled: false
  mysql:
    enabled: false
  postgresql:
    enabled: false         # Use the parent chart's PostgreSQL instance

# ConfigMap/Secret: not created in dev (app reads env vars directly)
config:
  createConfigMap: false
secrets:
  createSecret: false
```

### Usage

```bash
# Production (all components)
helm upgrade --install dataspoke helm-charts/dataspoke/ \
  -n dataspoke --create-namespace \
  -f production-secrets.yaml

# Dev (infra only)
helm upgrade --install dataspoke helm-charts/dataspoke/ \
  -f helm-charts/dataspoke/values-dev.yaml \
  -n dataspoke-01 --create-namespace
```

---

## Secrets Management

### Dev Environment

In dev, secrets are managed manually via `dev_env/.env`. The `dataspoke-infra/install.sh` script creates Kubernetes Secrets from these variables before the Helm install:

| Secret | Keys | Source |
|--------|------|--------|
| `dataspoke-postgres-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `DATASPOKE_POSTGRES_*` in `.env` |
| `dataspoke-redis-secret` | `REDIS_PASSWORD` | `DATASPOKE_REDIS_PASSWORD` in `.env` |
| `dataspoke-qdrant-secret` | `QDRANT_API_KEY` | `DATASPOKE_QDRANT_API_KEY` in `.env` |

Infrastructure subcharts reference these existing secrets via `auth.existingSecret`.

### Production

Two recommended approaches:

**Option A: Helm `--set` or sealed values file**

```bash
helm upgrade --install dataspoke helm-charts/dataspoke/ \
  -n dataspoke \
  --set secrets.postgres.password="$PG_PASSWORD" \
  --set secrets.redis.password="$REDIS_PASSWORD" \
  --set secrets.llm.apiKey="$LLM_API_KEY"
```

**Option B: External Secrets Operator (recommended for production)**

Use `ExternalSecret` CRDs to sync secrets from AWS Secrets Manager, HashiCorp Vault, or GCP Secret Manager:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: dataspoke-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: dataspoke-secrets
  data:
    - secretKey: DATASPOKE_POSTGRES_PASSWORD
      remoteRef:
        key: dataspoke/production
        property: postgres_password
    - secretKey: DATASPOKE_REDIS_PASSWORD
      remoteRef:
        key: dataspoke/production
        property: redis_password
    - secretKey: DATASPOKE_LLM_API_KEY
      remoteRef:
        key: dataspoke/production
        property: llm_api_key
```

When using External Secrets Operator, set `secrets.createSecret: false` in values and reference the externally-managed secret name in subchart configurations.

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

Production sizing targets a dedicated Kubernetes cluster. Adjust via `values.yaml` overrides for your environment.

### Dev Minimums

See [DEV_ENV.md §Resource Sizing](DEV_ENV.md#resource-sizing) for dev-specific sizing. The dev profile uses:
- Infrastructure only: ~2.8 Gi memory limits, ~1.75 CPU limits
- Combined with DataHub and examples: ~11.0 Gi total

---

## Ingress

### Frontend

```yaml
# values.yaml — ingress section
frontend:
  ingress:
    enabled: true
    className: "nginx"               # Or "alb", "traefik", etc.
    annotations:
      cert-manager.io/cluster-issuer: "letsencrypt-prod"
    hosts:
      - host: dataspoke.example.com
        paths:
          - path: /
            pathType: Prefix
    tls:
      - secretName: dataspoke-tls
        hosts:
          - dataspoke.example.com
```

### API

```yaml
api:
  ingress:
    enabled: true
    className: "nginx"
    annotations:
      nginx.ingress.kubernetes.io/proxy-body-size: "50m"
      cert-manager.io/cluster-issuer: "letsencrypt-prod"
    hosts:
      - host: api.dataspoke.example.com
        paths:
          - path: /api
            pathType: Prefix
    tls:
      - secretName: dataspoke-api-tls
        hosts:
          - api.dataspoke.example.com
```

In dev, ingress is disabled — services are accessed via port-forward.

---

## Cross-Namespace Access

DataSpoke requires network access to DataHub services (GMS, Kafka) running in a separate namespace. A NetworkPolicy template ensures this connectivity:

```yaml
# templates/networkpolicy.yaml
{{- if .Values.networkPolicy.enabled }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "dataspoke.fullname" . }}-datahub-egress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/instance: {{ .Release.Name }}
  policyTypes:
    - Egress
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .Values.networkPolicy.datahubNamespace }}
      ports:
        - port: 8080       # DataHub GMS
          protocol: TCP
        - port: 9092       # Kafka
          protocol: TCP
{{- end }}
```

Values:

```yaml
networkPolicy:
  enabled: false              # Enable when using strict NetworkPolicies
  datahubNamespace: "datahub" # Namespace where DataHub is deployed
```

In most dev and staging environments, NetworkPolicies are not enforced and this can remain disabled. Enable it in production clusters with default-deny policies.

---

## Dev Environment Integration

The `dev_env/dataspoke-infra/` directory consumes this Helm chart with the dev profile:

```bash
# dev_env/dataspoke-infra/install.sh (simplified)
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../.env"

CHART_DIR="$SCRIPT_DIR/../../helm-charts/dataspoke"

# Create secrets from .env
kubectl create secret generic dataspoke-postgres-secret \
  --from-literal=POSTGRES_USER="$DATASPOKE_POSTGRES_USER" \
  --from-literal=POSTGRES_PASSWORD="$DATASPOKE_POSTGRES_PASSWORD" \
  --from-literal=POSTGRES_DB="$DATASPOKE_POSTGRES_DB" \
  -n "$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic dataspoke-redis-secret \
  --from-literal=REDIS_PASSWORD="$DATASPOKE_REDIS_PASSWORD" \
  -n "$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

# Install with dev profile
helm upgrade --install dataspoke "$CHART_DIR" \
  -f "$CHART_DIR/values-dev.yaml" \
  -n "$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE" \
  --set postgresql.auth.existingSecret=dataspoke-postgres-secret \
  --set redis.auth.existingSecret=dataspoke-redis-secret \
  --timeout 5m --wait
```

This approach means:
1. The umbrella chart is the **single source of truth** for DataSpoke Kubernetes deployments
2. `dev_env/dataspoke-infra/` is a thin wrapper — no duplicate values files or templates
3. Switching from dev to production is changing the values file, not the chart

---

## References

- [Helm — Chart Dependencies](https://helm.sh/docs/helm/helm_dependency/) — umbrella chart pattern
- [Bitnami PostgreSQL Chart](https://github.com/bitnami/charts/tree/main/bitnami/postgresql) — `bitnami/postgresql`
- [Bitnami Redis Chart](https://github.com/bitnami/charts/tree/main/bitnami/redis) — `bitnami/redis`
- [Qdrant Helm Chart](https://github.com/qdrant/qdrant-helm) — `qdrant/qdrant`
- [Temporal Helm Chart](https://github.com/temporalio/helm-charts) — `temporalio/temporal`
- [External Secrets Operator](https://external-secrets.io/) — production secrets management
- [DEV_ENV.md](DEV_ENV.md) — Local development environment specification
- [ARCHITECTURE.md](../ARCHITECTURE.md) — System architecture and deployment topology
