# DataSpoke API

FastAPI service that acts as the single HTTP ingress for all DataSpoke clients — the portal UI and external AI agents.

API prefix: `/api/v1`
Route tiers: `/api/v1/spoke/common/…`, `/api/v1/spoke/dg/…`, `/api/v1/hub/…`, `/api/v1/auth/…`

---

## Prerequisites

- Python **3.13**
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

---

## Local Development

### 1. Install dependencies

```bash
# From the repo root
uv sync --directory src/api
# or
pip install -e "src/api[dev]"
```

### 2. Set environment variables

Copy or export the variables listed in the [Environment Variables](#environment-variables) table.
For local dev, the defaults work when `dev_env/` port-forwards are active.

```bash
# Start port-forwards (from another terminal)
cd dev_env && ./dataspoke-port-forward.sh
```

### 3. Run the server

```bash
# From repo root
uvicorn src.api.main:app --reload --port 8000
# or from src/api/
uvicorn main:app --reload --port 8000
```

The interactive docs are available at:
- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

---

## Environment Variables

All variables use the `DATASPOKE_` prefix (read by `src/api/config.py` via `pydantic-settings`).

| Variable | Default | Description |
|----------|---------|-------------|
| `DATASPOKE_JWT_SECRET_KEY` | `changeme-dev-secret-do-not-use-in-prod` | HMAC secret for JWT signing |
| `DATASPOKE_JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `DATASPOKE_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime (minutes) |
| `DATASPOKE_JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime (days) |
| `DATASPOKE_ADMIN_USERNAME` | `admin` | Stub admin username |
| `DATASPOKE_ADMIN_PASSWORD` | `admin` | Stub admin password |
| `DATASPOKE_CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed CORS origins (JSON list) |
| `DATASPOKE_RATE_LIMIT_PER_MINUTE` | `120` | Max requests per minute per client |
| `DATASPOKE_DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub GMS endpoint |
| `DATASPOKE_DATAHUB_TOKEN` | _(empty)_ | DataHub personal access token |
| `DATASPOKE_DATAHUB_KAFKA_BROKERS` | `localhost:9092` | Kafka broker addresses |
| `DATASPOKE_POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `DATASPOKE_POSTGRES_PORT` | `5432` | PostgreSQL port |
| `DATASPOKE_POSTGRES_USER` | `postgres` | PostgreSQL user |
| `DATASPOKE_POSTGRES_PASSWORD` | `postgres` | PostgreSQL password |
| `DATASPOKE_POSTGRES_DB` | `dataspoke` | PostgreSQL database name |
| `DATASPOKE_REDIS_HOST` | `localhost` | Redis host |
| `DATASPOKE_REDIS_PORT` | `6379` | Redis port |
| `DATASPOKE_REDIS_PASSWORD` | _(empty)_ | Redis password |
| `DATASPOKE_QDRANT_HOST` | `localhost` | Qdrant host |
| `DATASPOKE_QDRANT_HTTP_PORT` | `6333` | Qdrant HTTP port |
| `DATASPOKE_QDRANT_GRPC_PORT` | `6334` | Qdrant gRPC port |
| `DATASPOKE_QDRANT_API_KEY` | _(empty)_ | Qdrant API key |
| `DATASPOKE_TEMPORAL_HOST` | `localhost` | Temporal frontend host |
| `DATASPOKE_TEMPORAL_PORT` | `7233` | Temporal frontend port |
| `DATASPOKE_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `DATASPOKE_LLM_PROVIDER` | `openai` | LLM provider name |
| `DATASPOKE_LLM_API_KEY` | _(empty)_ | LLM provider API key |
| `DATASPOKE_LLM_MODEL` | `gpt-4o` | LLM model identifier |

---

## Running Tests

Unit tests run without a live dev environment (no real DB, DataHub, or Redis needed):

```bash
# From the repo root
pytest tests/unit/api/ -v
```

---

## Linting & Type Checks

```bash
# From the repo root
ruff check src/api tests/unit/api/
ruff format src/api tests/unit/api/
mypy src/api
```
