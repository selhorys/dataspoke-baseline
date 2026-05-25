# DataSpoke API

FastAPI service that acts as the single HTTP ingress for all DataSpoke clients — the portal UI and external AI agents.

API prefix: `/api/v1`
Route tiers (per [`spec/API.md`](../../spec/API.md)):
- `/api/v1/spoke/common/…` — baseline ingestion, validation, ontology generation, metadata generation
- `/api/v1/spoke/de/…`, `/api/v1/spoke/da/…` — reserved for org-specific extensions (no baseline routes)
- `/api/v1/spoke/dg/…` — baseline governance (metric, overview)
- `/api/v1/hub/…` — DataHub pass-through
- `/api/v1/auth/…` — JWT issue / refresh / revoke
- `/api/v1/admin/…`, `/internal/…` — admin and internal-token-gated routes

---

## Prerequisites

- Python **3.13**
- [`uv`](https://github.com/astral-sh/uv)

---

## Local Development

### 1. Install dependencies

```bash
# From the repo root
uv sync
```

### 2. Set environment variables

Copy or export the variables listed in the [Environment Variables](#environment-variables) table.
For local dev, the defaults work when the dev environment is installed and services are reachable via nginx-ingress.

```bash
# Verify the dev environment is healthy
./helm-charts/bin/health-check.sh
```

### 3. Access the API

The API runs **in-cluster** by default (deployed via `./helm-charts/bin/install.sh --profile dev --components api`). Access via nginx-ingress:
- API: `http://app.<INGRESS_DOMAIN>/api/v1/`
- ReDoc: `http://app.<INGRESS_DOMAIN>/redoc`

---

## Environment Variables

All variables use the `DATASPOKE_` prefix (read by `src/api/config.py` via `pydantic-settings`).

DataHub, Langfuse, and LLM provider/model/key are **not** env vars on the API pod — they live in the DB `peripheral_config` and `runtime_config` tables, updated via `/api/v1/admin/peripherals/{datahub,langfuse}` and `/api/v1/admin/conf`. See [spec/feature/HELM_CHART.md §Configuration](../../spec/feature/HELM_CHART.md#configuration--four-tier-env-vars).

> Runtime env vars on the API pod come from three sources: K8s Secret (`dataspoke-secrets` — credentials), ConfigMap (`dataspoke-app-config` — connection endpoints), and chart-rendered direct env (`api.testMode`, `api.testLlmReal`, `api.enableStubAuth`, `config.corsOrigins`). See `spec/feature/HELM_CHART.md §Configuration Flow`. Test-side equivalents are in `helm-charts/.env` under the `DATASPOKE_TEST_*` block.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATASPOKE_JWT_SECRET_KEY` | `changeme-dev-secret-do-not-use-in-prod` | HMAC secret for JWT signing |
| `DATASPOKE_JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `DATASPOKE_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime (minutes) |
| `DATASPOKE_JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime (days) |
| `DATASPOKE_ADMIN_EMAIL` | `admin` | Stub admin email |
| `DATASPOKE_ADMIN_PASSWORD` | `admin` | Stub admin password |
| `DATASPOKE_CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed CORS origins (JSON list) |
| `DATASPOKE_RATE_LIMIT_PER_MINUTE` | `120` | Max requests per minute per client |
| `DATASPOKE_POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `DATASPOKE_POSTGRES_PORT` | `5432` | PostgreSQL port |
| `DATASPOKE_POSTGRES_USER` | `postgres` | PostgreSQL user |
| `DATASPOKE_POSTGRES_PASSWORD` | `postgres` | PostgreSQL password |
| `DATASPOKE_POSTGRES_DB` | `dataspoke` | PostgreSQL database name |
| `DATASPOKE_REDIS_HOST` | `localhost` | Redis host |
| `DATASPOKE_REDIS_PORT` | `6379` | Redis port |
| `DATASPOKE_REDIS_PASSWORD` | _(empty)_ | Redis password |
| `DATASPOKE_AIRFLOW_URL` | `http://localhost:8080` | Airflow API base URL |
| `DATASPOKE_AIRFLOW_USER` | _(empty)_ | Airflow basic auth username |
| `DATASPOKE_AIRFLOW_PASSWORD` | _(empty)_ | Airflow basic auth password |

---

## Running Tests

Unit tests run without a live dev environment (no real DB, DataHub, or Redis needed):

```bash
# From the repo root
uv run pytest tests/unit/api/ -v
```

---

## Linting & Type Checks

```bash
# From the repo root
uv run ruff check src/api tests/unit/api/
uv run ruff format src/api tests/unit/api/
uv run mypy src/api
```
