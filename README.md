# DataSpoke

AI-powered sidecar extension for [DataHub](https://datahubproject.io/) — organized by user group for Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG).

DataSpoke is a **loosely coupled sidecar** to DataHub. DataHub stores metadata (the Hub); DataSpoke extends it with quality scoring, semantic search, ontology construction, and metrics dashboards (the Spokes).

This repository delivers two artifacts:

- **Baseline Product** — A pre-built implementation of essential features for an AI-era catalog, targeting DE, DA, and DG user groups.
- **AI Scaffold** — Claude Code conventions, development specs, and utilities — including the PRauto autonomous PR system — that enable rapid construction of custom data catalogs with AI coding agents.

Fork or copy this repository to create a data catalog for your organization.

## Usage Guide

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A Kubernetes cluster with appropriate capacity (see resource sizing below)
- A **separate DataHub instance** — DataSpoke connects to DataHub as an external dependency

### Deploy to Production

DataSpoke ships as an umbrella Helm chart at `helm-charts/dataspoke/`. The production profile (`values.yaml`) enables all components: frontend, API, workers, and infrastructure (PostgreSQL, Redis, Qdrant, Kestra).

#### 1. Build and push container images

```bash
# API (only docker image currently available)
docker build -t <your-registry>/dataspoke/api:latest -f docker-images/api/Dockerfile .
docker push <your-registry>/dataspoke/api:latest

# Workers — TBD (docker-images/workers/ not yet created)
# Frontend — TBD (docker-images/frontend/ not yet created; src/frontend/ not yet implemented)
```

#### 2. Configure Helm values

Copy `helm-charts/dataspoke/values.yaml` and customize for your environment. Key sections to review:

**Container images** — point to your registry:

```yaml
api:
  image:
    repository: <your-registry>/dataspoke/api
    tag: <your-tag>
workers:
  image:
    repository: <your-registry>/dataspoke/workers
    tag: <your-tag>
frontend:
  image:
    repository: <your-registry>/dataspoke/frontend
    tag: <your-tag>
```

**Ingress** — set your domain and TLS:

```yaml
frontend:
  ingress:
    hosts:
      - host: dataspoke.example.com
        paths: [{ path: /, pathType: Prefix }]
    tls:
      - secretName: dataspoke-tls
        hosts: [dataspoke.example.com]
api:
  ingress:
    hosts:
      - host: api.dataspoke.example.com
        paths: [{ path: /api, pathType: Prefix }]
```

**DataHub connection** — point to your existing DataHub instance:

```yaml
config:
  datahub:
    gmsUrl: "http://<datahub-gms-host>:8080"
    kafkaBrokers: "<datahub-kafka-host>:9092"
```

**Secrets** — credentials for infrastructure and external services:

```yaml
secrets:
  datahub:
    token: ""          # DataHub personal access token
  postgres:
    user: "dataspoke"
    password: ""       # Must set
  redis:
    password: ""       # Must set
  qdrant:
    apiKey: ""         # Qdrant API key (if auth enabled)
  llm:
    apiKey: ""         # LLM provider API key
  jwt:
    secretKey: ""      # Must set for production auth
```

For production secrets management, consider using [External Secrets Operator](https://external-secrets.io/) to sync from AWS Secrets Manager, Vault, or GCP Secret Manager. Set `secrets.createSecret: false` and reference the externally-managed secret.

**Resource sizing** — production defaults total ~5.5 CPU / ~8.5 Gi requests, ~11 CPU / ~17 Gi limits, plus 100 Gi PV for PostgreSQL and Qdrant. Adjust replicas and limits in `values.yaml` per component. See [`spec/feature/HELM_CHART.md` §Resource Sizing](spec/feature/HELM_CHART.md#resource-sizing) for the full breakdown.

#### 3. Install

```bash
helm dependency build ./helm-charts/dataspoke
helm upgrade --install dataspoke ./helm-charts/dataspoke \
  --namespace dataspoke --create-namespace \
  --values ./your-values.yaml
```

**Optional components**:
- `event-consumer.enabled=true` — deploy Kafka consumer as a separate pod (default: co-located in workers). Enable for independent scaling in production.
- `networkPolicy.enabled=true` — restrict egress to DataHub namespace only. Required in clusters with default-deny policies.

See [`spec/feature/HELM_CHART.md`](spec/feature/HELM_CHART.md) for the full chart reference (structure, configuration flow, secrets management, ingress, network policy).

## Development Guide

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A local Kubernetes cluster (Docker Desktop, minikube, or kind) with **8+ CPUs / 16 GB RAM**
- **Python 3.13** and [`uv`](https://github.com/astral-sh/uv) for Python dependency management
- **Node.js 18+** for running the frontend (TBD — frontend not yet implemented)

### Dev Environment Setup

The dev environment provisions **infrastructure dependencies** (DataHub, PostgreSQL, Redis, Qdrant, Kestra, example data sources) into a local Kubernetes cluster. Application services run on the host or in-cluster depending on the testing mode.

#### 1. Configure

```bash
cp dev_env/.env.example dev_env/.env
```

Edit `dev_env/.env` — the minimum required change is setting your Kubernetes context:

```bash
DATASPOKE_DEV_KUBE_CLUSTER=docker-desktop   # or minikube, kind, etc.
```

The `.env` file contains two tiers of variables:

| Prefix | Scope | Purpose |
|--------|-------|---------|
| `DATASPOKE_DEV_*` | Dev scripts only | Cluster context, namespace names, chart versions, port-forward ports |
| `DATASPOKE_*` (no `DEV`) | App runtime | Infrastructure endpoints — `localhost` in dev, in-cluster addresses in prod |

The dev environment uses the same umbrella Helm chart as production (`helm-charts/dataspoke/`) but with a dev overlay (`values-dev.yaml`) that disables application subcharts and reduces resource limits. Infrastructure Helm values (resource limits, persistence sizes, Kestra configuration) are in `helm-charts/dataspoke/values-dev.yaml`. Credentials (PostgreSQL password, Redis password, etc.) are read from `dev_env/.env` and injected via `--set` at install time.

See [`spec/feature/DEV_ENV.md` §Configuration](spec/feature/DEV_ENV.md#configuration) for the full variable listing.

#### 2. Install

```bash
cd dev_env && ./install.sh    # ~5-10 min first run
```

> Using Claude Code? Run `/dev-env install` for guided setup.

#### 3. Start Port-Forwarding

```bash
dev_env/datahub-port-forward.sh      # DataHub UI + GMS
dev_env/dataspoke-port-forward.sh    # DataSpoke infrastructure
dev_env/dummy-data-port-forward.sh   # Example data sources
dev_env/lock-port-forward.sh         # Dev-env advisory lock service
```

| Service | URL / Address | Credentials |
|---------|--------------|-------------|
| DataHub UI | http://localhost:9002 | `datahub` / `datahub` |
| DataHub GMS | http://localhost:9004 | -- |
| PostgreSQL | localhost:9201 | per `dev_env/.env` |
| Redis | localhost:9202 | per `dev_env/.env` |
| Qdrant | localhost:9203 (HTTP), :9204 (gRPC) | -- |
| Kestra (API + UI) | localhost:9205 | -- |
| Example PostgreSQL | localhost:9102 | `postgres` / `ExampleDev2024!` |
| Example Kafka | localhost:9104 | -- |
| Lock Service | localhost:9221 | -- |

#### 4. Verify

```bash
./dev_env/health-check.sh            # Recommended: checks all services are reachable and responding
```

<details><summary>Manual verification (kubectl)</summary>

```bash
source dev_env/.env
kubectl get pods -n $DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
kubectl get pods -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE
kubectl get pods -n $DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE
```

</details>

#### Uninstall

```bash
cd dev_env && ./uninstall.sh
```

See [`dev_env/README.md`](dev_env/README.md) for lock service, namespace architecture, resource budgets, and troubleshooting.

### Running DataSpoke (Testing Modes)

Integration tests support two execution modes. See [`spec/TESTING.md` §Testing Modes](spec/TESTING.md#testing-modes) for full details.

#### Host Mode (default)

Application services run on the developer's machine, connecting to port-forwarded infrastructure. This is the standard development workflow — fast test-and-fix loop with no container rebuild needed.

```bash
# Install/sync dependencies first:
uv sync

# Start all components (API + Worker + auto-migrate):
uv run -m src.cli

# Backend only (skip frontend when it's added):
uv run -m src.cli --backend-only

# See all options:
uv run -m src.cli --help
```

#### In-Cluster Mode (on-demand)

Deploys all components into the Kubernetes cluster using the umbrella Helm chart with application subcharts enabled. Use only when testing Kubernetes-specific behavior (health probes, ingress routing, resource limits, network policies).

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

This mode is significantly slower to iterate — every code change requires a container rebuild and `helm upgrade`. See [`spec/feature/HELM_CHART.md` §In-Cluster Testing](spec/feature/HELM_CHART.md#in-cluster-testing).

### Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| API layer (FastAPI) | Done | `src/api/` |
| Backend services | Done | `src/backend/`, `src/shared/` |
| Kestra workflows | Done | `src/workflows/` |
| Database migrations | Done | `migrations/` |
| Docker image (API) | Done | `docker-images/api/` |
| Docker image (Workers) | TBD | `docker-images/workers/` (planned) |
| Docker image (Frontend) | TBD | `docker-images/frontend/` (planned) |
| Helm charts | Done | `helm-charts/dataspoke/` |
| Tests (unit + integration) | Done | `tests/` |
| Frontend (Next.js) | TBD | `src/frontend/` (planned) |

### Testing

#### Populate Dummy Data

```bash
# Requires port-forwards for 9102, 9104, 9004
uv run python -m tests.integration.util --reset-all
```

Seeds example PostgreSQL (11 schemas, 17 tables, ~600 rows), Kafka (3 topics, ~45 messages), and DataHub (20 dataset entities) with Imazon use-case data. Idempotent — safe to re-run.

#### Run Tests

```bash
# Unit tests (no dev environment needed)
uv run pytest tests/unit/

# Integration tests (requires port-forwards)
uv run pytest tests/integration/
```

See [`spec/TESTING.md`](spec/TESTING.md) for conventions, toolchain, mocking rules, and the integration test lock protocol.

### Implementation Workflow

Use the plan → approve → generate → evaluate workflow to implement features:

1. Read the relevant spec in `spec/feature/` or `spec/feature/spoke/`
2. Plan (built-in Plan mode) — produce implementation plan with acceptance criteria
3. Human reviews and approves the plan
4. `backend` → `reviewer` → [fix pass if needed]
5. `workflow` → `reviewer` → [fix pass if needed]
6. `test` — write and run tests
7. `frontend` → `reviewer` → [fix pass if needed]
8. `k8s-helm` — containerize and deploy

See [`spec/AI_SCAFFOLD.md`](spec/AI_SCAFFOLD.md) for the full scaffold reference (skills, subagents, permissions, PRauto).

### Building a Custom Spoke

Fork this repository and adapt:

1. Revise `spec/MANIFESTO_*.md` -- redefine user groups, features, and product identity
2. Run `/plan-doc` -- update architecture and author feature specs
3. Run `/dev-env install` -- bring up the local environment
4. Use the plan → approve → generate → evaluate workflow: Plan mode → approve → `backend` → `reviewer` → `test` → `frontend` → `reviewer` → `k8s-helm`

### Key Specs

| Document | Purpose |
|----------|---------|
| [spec/MANIFESTO_en.md](spec/MANIFESTO_en.md) | Product identity, user-group taxonomy |
| [spec/ARCHITECTURE.md](spec/ARCHITECTURE.md) | System architecture, tech stack, repository structure, deployment |
| [spec/AI_SCAFFOLD.md](spec/AI_SCAFFOLD.md) | Claude Code scaffold: skills, subagents, permissions, PRauto |
| [spec/TESTING.md](spec/TESTING.md) | Testing conventions and integration test protocol |
| [spec/DATAHUB_INTEGRATION.md](spec/DATAHUB_INTEGRATION.md) | DataHub SDK/API patterns |
| [spec/API_DESIGN_PRINCIPLE_en.md](spec/API_DESIGN_PRINCIPLE_en.md) | REST API conventions |
| [spec/feature/](spec/feature/) | Feature specs (API, BACKEND, BACKEND_SCHEMA, FRONTEND_*, DEV_ENV, HELM_CHART) |

## License

[Apache License 2.0](LICENSE)
