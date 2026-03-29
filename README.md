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
- A Kubernetes cluster with appropriate capacity
- A **separate DataHub instance** — DataSpoke connects to DataHub as an external dependency

### Deploy to Production

DataSpoke ships as an umbrella Helm chart at `helm-charts/dataspoke/`. The production profile (`values.yaml`) enables all components: frontend, API, workers, and infrastructure (PostgreSQL, Redis, Qdrant, Kestra).

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
- A local Kubernetes cluster (Docker Desktop, minikube, or kind) with **8+ CPUs / 16 GB RAM**
- **Python 3.13** and [`uv`](https://github.com/astral-sh/uv)
- **Node.js 18+** (TBD — frontend not yet implemented)

### Dev Environment Setup

The dev environment provisions infrastructure (DataHub, PostgreSQL, Redis, Qdrant, Kestra, example data sources) into a local Kubernetes cluster. Application services run on the host by default.

```bash
cp dev_env/.env.example dev_env/.env   # Set your Kubernetes context
cd dev_env && ./install.sh             # ~5-10 min first run
```

> Using Claude Code? Run `/dev-env install` for guided setup.

After install, start port-forwards and verify:

```bash
dev_env/datahub-port-forward.sh       # DataHub UI (9002) + GMS (9004)
dev_env/dataspoke-port-forward.sh     # PostgreSQL (9201), Redis (9202), Qdrant (9203-4), Kestra (9205)
dev_env/dummy-data-port-forward.sh    # Example PostgreSQL (9102), Kafka (9104)
dev_env/lock-port-forward.sh          # Advisory lock (9221)
./dev_env/health-check.sh             # Verify all services respond
```

See [`dev_env/README.md`](dev_env/README.md) for credentials, lock service, namespace architecture, resource budgets, and troubleshooting.

#### Uninstall

```bash
cd dev_env && ./uninstall.sh
```

### Running DataSpoke

```bash
uv sync                    # Install dependencies
uv run -m src.cli          # Start API + auto-migrate (host mode)
uv run -m src.cli --help   # See all options
```

For in-cluster testing (Kubernetes-specific behavior only), see [`spec/feature/HELM_CHART.md` §In-Cluster Testing](spec/feature/HELM_CHART.md#in-cluster-testing).

### Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| API layer (FastAPI) | Done | `src/api/` |
| Backend services | Done | `src/backend/`, `src/shared/` |
| Kestra workflows | Done | `src/workflows/` |
| Database migrations | Done | `migrations/` |
| Docker image (API) | Done | `docker-images/api/` |
| Helm charts | Done | `helm-charts/dataspoke/` |
| Tests (unit + integration) | Done | `tests/` |
| Frontend (Next.js) | TBD | `src/frontend/` |

### Testing

```bash
uv run pytest tests/unit/                      # Unit tests (no infra needed)
uv run pytest tests/integration/               # Integration tests (requires port-forwards)
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
| [spec/MANIFESTO_en.md](spec/MANIFESTO_en.md) | Product identity, user-group taxonomy |
| [spec/ARCHITECTURE.md](spec/ARCHITECTURE.md) | System architecture, tech stack, deployment |
| [spec/AI_SCAFFOLD.md](spec/AI_SCAFFOLD.md) | Claude Code scaffold: skills, subagents, PRauto |
| [spec/TESTING.md](spec/TESTING.md) | Testing conventions and integration test protocol |
| [spec/DATAHUB_INTEGRATION.md](spec/DATAHUB_INTEGRATION.md) | DataHub SDK/API patterns |
| [spec/API_DESIGN_PRINCIPLE_en.md](spec/API_DESIGN_PRINCIPLE_en.md) | REST API conventions |
| [spec/feature/](spec/feature/) | Feature specs (API, BACKEND, FRONTEND, DEV_ENV, HELM_CHART) |

## License

[Apache License 2.0](LICENSE)
