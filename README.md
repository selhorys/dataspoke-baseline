# DataSpoke

> **Note:** This project is currently under active development and has not been officially released. APIs, features, and documentation are subject to change without notice.

AI-powered sidecar extension for [DataHub](https://datahubproject.io/), built API-first.

DataSpoke is a **loosely coupled sidecar** to DataHub. DataHub stores metadata (the Hub); DataSpoke extends it with five baseline features (the Spokes): **Ingestion Control**, **Validation**, **Ontology Generation**, **Metadata Generation**, and **Governance**. The user-group framing (Data Engineering / Data Analysis / Data Governance) is a UI and API extensibility surface only — `/spoke/dg/` carries baseline governance routes; `/spoke/de/` and `/spoke/da/` are reserved for organization-specific extensions.

This repository delivers two artifacts:

- **Baseline Product** — A foundational data catalog implementation of the five MANIFESTO features. The API contract in [`spec/API.md`](spec/API.md) is the canonical surface; the frontend is a thin reference UI that consumes those routes verbatim.
- **Productized Scaffold** — An **AI Scaffold** (Claude Code conventions, generator/evaluator subagents, PRauto) plus a **Development Scaffold** (scripted Kubernetes dev environment) that together let teams fork this repo and build custom Spokes with AI coding agents.

Fork or copy this repository to create a data catalog for your organization.

## Usage Guide

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A Kubernetes cluster with appropriate capacity
- A **separate DataHub instance** — DataSpoke connects to DataHub as an external dependency

### Deploy to Production

DataSpoke ships as an umbrella Helm chart at `helm-charts/dataspoke/`. The production profile (`values.yaml`) enables all components: frontend, API, workers, and infrastructure (PostgreSQL with pgvector + Apache AGE, Redis, Airflow).

1. **Build and push images**: `docker build -t <registry>/dataspoke/api:latest -f docker-images/api/Dockerfile .` (Workers and Frontend images TBD)
2. **Configure**: Copy `helm-charts/dataspoke/values.yaml` and customize — container images, ingress hosts/TLS, DataHub connection (`config.datahub.gmsUrl`), and secrets (PostgreSQL, Redis, JWT, LLM API key). For production secrets management, consider [External Secrets Operator](https://external-secrets.io/).
3. **Install**:
   ```bash
   helm dependency build ./helm-charts/dataspoke
   helm upgrade --install dataspoke ./helm-charts/dataspoke \
     --namespace dataspoke --create-namespace \
     --values ./your-values.yaml
   ```

**Resource sizing**: Production defaults total ~5.5 CPU / ~8.5 Gi requests, ~11 CPU / ~17 Gi limits. See [`spec/feature/HELM_CHART.md`](spec/feature/HELM_CHART.md) for the full chart reference.

## Development Guide

### Prerequisites

- **kubectl** + **Helm v3** installed and configured
- A Kubernetes cluster (GKE Autopilot recommended; Docker Desktop, minikube, or kind also work) with **8+ CPUs / 16 GB RAM**
- **Python 3.13** and [`uv`](https://github.com/astral-sh/uv)
- **Node.js 18+** (TBD — frontend not yet implemented)

### Dev Environment Setup

The dev environment provisions infrastructure (DataHub, PostgreSQL with pgvector + Apache AGE, Redis, Airflow, example data sources) into a Kubernetes cluster. The API runs **in-cluster** alongside Airflow (for workflow callbacks); frontend runs on the host.

```bash
cp dev_env/.env.example dev_env/.env   # Set your Kubernetes context
cd dev_env && ./install.sh             # ~5-10 min first run
```

> Using Claude Code? Run `/dev-env install` for guided setup.

After install, verify all services are reachable:

```bash
./dev_env/health-check.sh             # Verify all services respond via nginx-ingress
```

Services are accessed via nginx-ingress endpoints — HTTP services use virtual-host routing (`http://<service>.<INGRESS_IP>.nip.io/`) and TCP services use dedicated ports on the ingress IP. See [`dev_env/README.md`](dev_env/README.md) for the full endpoint table, credentials, lock service, namespace architecture, resource budgets, and troubleshooting.

#### Uninstall

```bash
cd dev_env && ./uninstall.sh
```

### Running DataSpoke

```bash
uv sync                                  # Install dependencies
./dev_env/dataspoke-test-mode.sh         # Build image, deploy API in-cluster via Helm
./dev_env/dataspoke-test-mode.sh --stop  # Scale down in-cluster API
```

The API is accessible via nginx-ingress at `http://app.<INGRESS_IP>.nip.io/api/v1/`. For optional host-mode development (no Airflow callbacks): `uv run -m src.cli`. See [`spec/TESTING.md`](spec/TESTING.md) for testing modes.

### Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| API layer (FastAPI) | Done | `src/api/` |
| Backend services | Done | `src/backend/`, `src/shared/` |
| Airflow DAGs | Done | `src/workflows/dags/` |
| Database migrations | Done | `migrations/` |
| Docker image (API) | Done | `docker-images/api/` |
| Helm charts | Done | `helm-charts/dataspoke/` |
| Tests (unit + integration) | Done | `tests/` |
| Frontend (Next.js) | TBD | `src/frontend/` |

### Testing

```bash
uv run pytest tests/unit/                      # Unit tests (no infra needed)
uv run pytest tests/integration/               # Integration tests (requires dev environment with ingress)
uv run python -m tests.integration.util --reset-all  # Seed dummy data (Imazon use-case)
```

See [`spec/TESTING.md`](spec/TESTING.md) for conventions, three-group execution sequence, and the integration test lock protocol.

### Implementation Workflow

Use the plan -> approve -> generate -> evaluate workflow:

1. Read the relevant spec in `spec/feature/`
2. Plan (built-in Plan mode) -> human reviews and approves
3. `backend` -> `reviewer` -> [fix pass if needed]
4. `workflow` -> `reviewer` -> [fix pass if needed]
5. `test` -- write and run tests
6. `frontend` -> `reviewer` -> [fix pass if needed]
7. `k8s-helm` -- containerize and deploy

See [`spec/AI_SCAFFOLD.md`](spec/AI_SCAFFOLD.md) for the full scaffold reference.

### Building a Custom Spoke

Fork this repository and adapt:

1. Revise `spec/MANIFESTO_*.md` -- redefine user groups, features, and product identity
2. Run `/plan-doc` -- update architecture and author feature specs
3. Run `/dev-env install` -- bring up the local environment
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
| [spec/AI_SCAFFOLD.md](spec/AI_SCAFFOLD.md) | Claude Code scaffold: skills, subagents, PRauto |
| [spec/TESTING.md](spec/TESTING.md) | Testing conventions and integration test protocol |
| [spec/feature/](spec/feature/) | Feature specs (BACKEND, BACKEND_SCHEMA, FRONTEND_*, DEV_ENV, HELM_CHART) |

## License

[Apache License 2.0](LICENSE)
