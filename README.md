# DataSpoke

> **Note:** This project is currently under active development and has not been officially released. APIs, features, and documentation are subject to change without notice.

AI-powered sidecar extension for [DataHub](https://datahubproject.io/), built API-first.

DataSpoke is a **loosely coupled sidecar** to DataHub. DataHub stores metadata (the Hub); DataSpoke extends it with five baseline features (the Spokes): **Ingestion Control**, **Validation**, **Ontology Generation**, **Metadata Generation**, and **Governance**. Both UI and API are organised by feature — one function namespace each under `/spoke/`.

This repository delivers two artifacts:

- **Baseline Product** — A foundational data catalog implementation of the five MANIFESTO features. The API contract in [`spec/API.md`](spec/API.md) is the canonical surface; the frontend is a thin reference UI that consumes those routes verbatim.
- **Productized Scaffold** — An **AI Scaffold** (Claude Code conventions, generator/evaluator subagents, PRauto) plus a **Development Scaffold** (scripted Kubernetes dev environment) that together let teams fork this repo and build custom Spokes with AI coding agents.

Fork or copy this repository to create a data catalog for your organization.

📊 [Introduction slides (Korean)](https://docs.google.com/presentation/d/1wQRUagHEkEYmGirdPRGdlYlJHiN00tC880d8rGcoGjw)

## Demo

A walkthrough of DataSpoke — the five baseline features and the AI-driven build workflow — running against the Imazon test estate.

[![DataSpoke demo video](https://img.youtube.com/vi/2Z1Tm16SSaE/maxresdefault.jpg)](https://www.youtube.com/watch?v=2Z1Tm16SSaE)

> ▶️ [Watch the demo on YouTube](https://www.youtube.com/watch?v=2Z1Tm16SSaE)

## Usage Guide

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A Kubernetes cluster with appropriate capacity
- A **separate DataHub instance** — DataSpoke connects to DataHub as an external dependency

### Deploy to Production

DataSpoke ships as an umbrella Helm chart at `helm-charts/dataspoke/`. The production profile (`values.yaml`) enables the application components (frontend, API) and infrastructure (PostgreSQL with pgvector + Apache AGE, Redis, Airflow). The optional event-consumer subchart is shipped disabled — baseline UC1–UC5 are schedule-driven via Airflow rather than event-driven.

1. **Build and push images**: `docker build -t <registry>/dataspoke/api:latest -f docker-images/api/Dockerfile .` and `docker build -t <registry>/dataspoke/frontend:latest -f src/frontend/Dockerfile .` (event-consumer is disabled by default)
2. **Configure**: Copy `helm-charts/dataspoke/values.yaml` and customize — container images, ingress hosts/TLS, DataHub connection (`config.datahub.gmsUrl`), and secrets (PostgreSQL, Redis, JWT, LLM API key). For production secrets management, read [SECRET_RESOLUTION.md](spec/feature/SECRET_RESOLUTION.md).
3. **Install**:
   ```bash
   helm dependency build ./helm-charts/dataspoke
   helm upgrade --install dataspoke ./helm-charts/dataspoke \
     --namespace dataspoke --create-namespace \
     --values ./your-values.yaml
   ```

**Resource sizing**: Production defaults total ~5 CPU / ~10 CPU and ~9.5 Gi / ~22 Gi (requests / limits), excluding the opt-in event-consumer. See [`spec/feature/HELM_CHART.md`](spec/feature/HELM_CHART.md) for the full chart reference.

## Development Guide

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A Kubernetes cluster with **8+ CPUs / 24 GB RAM / 150 GB storage** — either one where DataSpoke installs and owns its nginx-ingress controller (GKE Autopilot, minikube, Docker Desktop, kind; `managed` ingress mode, the default) or one with a pre-existing ingress controller to reuse (e.g. AWS/EKS; `shared` ingress mode)
- **Python 3.13** and [`uv`](https://github.com/astral-sh/uv)
- **Node.js 22+** and [`pnpm`](https://pnpm.io/) — for host frontend development (`--frontend local`)

### Dev Environment Setup

The dev profile installs infrastructure (DataHub, PostgreSQL with pgvector + Apache AGE, Redis, Airflow, self-hosted Langfuse for LLM observability, example data sources) into a Kubernetes cluster via the umbrella Helm chart plus dev peripherals. The API runs **in-cluster** alongside Airflow (for workflow callbacks). The frontend is deployed per the `--frontend` flag (default `none` in dev): `local` writes `src/frontend/.env.local` so a host `pnpm dev` reaches the in-cluster API, `cluster` deploys the containerised UI.

```bash
cp helm-charts/.env.dev.example helm-charts/.env.dev   # Set your Kubernetes context
./helm-charts/bin/install.sh --profile dev              # ~5-10 min first run
```

> Using Claude Code? Run `/k8s-deploy install` for guided setup.

After install, verify all services are reachable:

```bash
./helm-charts/bin/health-check.sh                   # Verify all services respond via nginx-ingress
```

Services are accessed via nginx-ingress endpoints — HTTP services use virtual-host routing (`http://<service>.<INGRESS_DOMAIN>/`). How the controller and domain are provided depends on `DATASPOKE_KUBE_INGRESS_MODE` in `helm-charts/.env.dev`:

- **`managed`** (default) — the install owns an nginx-ingress controller; the domain auto-derives to `<LoadBalancer-IP>.nip.io` (wildcard DNS, no `/etc/hosts` entries) and TCP services (databases, brokers) use dedicated ports on that IP.
- **`shared`** — the install reuses the cluster's pre-existing ingress controller (e.g. AWS/EKS); the operator pre-sets `DATASPOKE_KUBE_INGRESS_DOMAIN`, and TCP services are reached on `127.0.0.1` via `./helm-charts/bin/port-forward.sh`.

See [`helm-charts/README.md`](helm-charts/README.md) for the full endpoint table, credentials, lock service, namespace architecture, resource budgets, and troubleshooting.

#### Uninstall

```bash
./helm-charts/bin/uninstall.sh --profile dev
```

### Running DataSpoke

```bash
uv sync                                                                # Install dependencies
./helm-charts/bin/install.sh --profile dev --components api            # Rebuild + redeploy the API
```

The API is accessible via nginx-ingress at `http://api.<INGRESS_DOMAIN>/api/v1/`. See [`spec/TESTING.md`](spec/TESTING.md) for testing modes.

For the frontend, either iterate on the host against the in-cluster API or rebuild the containerised UI:

```bash
./helm-charts/bin/install.sh --profile dev --frontend local           # Write src/frontend/.env.local
pnpm -C src/frontend install && pnpm -C src/frontend dev              # Host dev server (http://localhost:3000)
./helm-charts/bin/install.sh --profile dev --components frontend       # Rebuild + redeploy the cluster UI
```

### Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| API layer (FastAPI) | Done | `src/api/` |
| Backend services | Done | `src/backend/`, `src/shared/` |
| Airflow DAGs | Done | `src/workflows/dags/` |
| Database migrations | Done | `migrations/` |
| Docker image (API) | Done | `docker-images/api/` |
| Helm charts | Done | `helm-charts/dataspoke/` |
| Tests (unit + integration + E2E) | Done | `tests/` |
| Frontend (Next.js) | Done | `src/frontend/` |

### Testing

```bash
uv run pytest tests/unit/                      # Unit tests (no infra needed)
set -a && source helm-charts/.env.dev && set +a \
  && uv run pytest tests/integration/spot/ \
  && uv run pytest tests/integration/api_wired/  # Integration tests (requires dev environment; run groups separately)
pnpm -C src/frontend test                      # Frontend unit tests (Vitest, mocked)
pnpm -C tests/e2e test                         # Browser E2E (Playwright; requires --frontend cluster)
uv run python -m tests.integration.util --reset-seed  # Seed dummy data (Imazon use-case)
```

See [`spec/TESTING.md`](spec/TESTING.md) for conventions, group execution sequence, and the integration test lock protocol.

### Implementation Workflow

Use the plan -> approve -> generate -> evaluate workflow:

1. Read the relevant spec in `spec/feature/`
2. Plan (built-in Plan mode) -> human reviews and approves
3. `spec` -> `spec-reviewer` -> [fix pass if needed] (when the plan changes specs)
4. `backend` -> `reviewer` -> [fix pass if needed]
5. `airflow-dag` -> `reviewer` -> [fix pass if needed]
6. `test` -> `test-reviewer` -> [fix pass if needed]
7. `frontend` -> `reviewer` -> [fix pass if needed]
8. `k8s-helm` -- containerize and deploy

See [`spec/AI_SCAFFOLD.md`](spec/AI_SCAFFOLD.md) for the full scaffold reference.

### Building a Custom Spoke

Fork this repository and adapt:

1. Revise `spec/MANIFESTO_*.md` -- redefine features and product identity
2. Run `/spec-write` -- update architecture and author feature specs
3. Run `/k8s-deploy install` -- bring up the local environment
4. Use the implementation workflow above

### Key Specs

| Document | Purpose |
|----------|---------|
| [spec/MANIFESTO_en.md](spec/MANIFESTO_en.md) | **Golden** — product identity, five baseline features |
| [spec/API.md](spec/API.md) | **Golden** — route catalogue, auth, middleware, error catalogue |
| [spec/USE_CASE_en.md](spec/USE_CASE_en.md) | **Golden** — five UC scenarios on the Imazon test estate |
| [spec/ARCHITECTURE.md](spec/ARCHITECTURE.md) | System architecture, tech stack, deployment |
| [spec/DATAHUB_INTEGRATION.md](spec/DATAHUB_INTEGRATION.md) | DataHub SDK/API patterns |
| [spec/API_DESIGN_PRINCIPLE_en.md](spec/API_DESIGN_PRINCIPLE_en.md) | REST API conventions |
| [spec/AI_SCAFFOLD.md](spec/AI_SCAFFOLD.md) | Claude Code scaffold: skills, subagents, hooks |
| [spec/AI_PRAUTO.md](spec/AI_PRAUTO.md) | PRauto autonomous PR worker: lifecycle labels, heartbeat, phase state machine |
| [spec/AI_PLUGIN.md](spec/AI_PLUGIN.md) | End-User AI Scaffold: public-API-only Claude Code plugin for consuming a deployed DataSpoke |
| [spec/TESTING.md](spec/TESTING.md) | Testing conventions and integration test protocol |
| [spec/feature/](spec/feature/) | Feature specs (AUTH, BACKEND, BACKEND_LLM, BACKEND_SCHEMA, VALIDATION, SECRET_RESOLUTION, FRONTEND_*, HELM_CHART) |

## License

[Apache License 2.0](LICENSE)
