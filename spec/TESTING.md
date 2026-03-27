# DataSpoke: Testing Conventions

> This document defines testing conventions, toolchains, and workflows for DataSpoke.
> Priority 3 in the spec hierarchy — alongside [`ARCHITECTURE.md`](ARCHITECTURE.md).
> For the technology decisions that motivate the toolchain choices here, see [`ARCHITECTURE.md §Technology Stack`](ARCHITECTURE.md#technology-stack).
> For the dev environment and lock service used in integration/E2E tests, see [`spec/feature/DEV_ENV.md`](feature/DEV_ENV.md).
> For the Imazon use-case scenarios that define test data context, see [`spec/USE_CASE_en.md`](USE_CASE_en.md).

---

## Table of Contents

1. [Toolchain Summary](#toolchain-summary)
2. [Repository Layout](#repository-layout)
3. [Python Environment Setup](#python-environment-setup)
4. [Unit Testing](#unit-testing)
5. [Integration Testing](#integration-testing)
6. [API-Wired Integration Testing](#api-wired-integration-testing)
7. [End-to-End (E2E) Testing](#end-to-end-e2e-testing)
8. [Test Data Design](#test-data-design)
9. [CI Behavior](#ci-behavior)

---

## Toolchain Summary

| Layer | Language | Framework | Static Gates |
|-------|----------|-----------|-------------|
| Backend (API + services) | Python 3.13 | pytest + httpx | mypy, ruff |
| Frontend | TypeScript | Jest + React Testing Library | TypeScript compiler, ESLint |
| E2E | TypeScript | Playwright | — |

> **Do not use the `datahub` CLI** — it requires Python ≤ 3.11 and is incompatible with the project's Python 3.13 runtime. Use Python scripts with the `acryl-datahub` SDK instead (e.g., `tests/integration/util/datahub.py`).

---

## Repository Layout

Tests live under `tests/` at the repo root, mirroring `src/`:

```
tests/
├── unit/
│   ├── api/            # FastAPI route tests (no running server)
│   ├── backend/        # Service logic tests
│   ├── shared/         # DataHub client wrapper, shared model tests
│   ├── workflows/      # Kestra flow and activity endpoint tests
│   └── frontend/       # Jest tests (or co-located in src/frontend/)
├── integration/             # Dev-env-backed integration tests
│   ├── util/                # Dummy-data reset/ingest utilities
│   │   ├── fixtures/sql/    # SQL seed files (10 files: 00_schemas … 09_ebooknow)
│   │   ├── fixtures/kafka/  # Kafka JSONL seed messages (orders, shipping, reviews)
│   │   ├── postgres.py      # PostgreSQL reset functions (asyncpg, port 9102)
│   │   ├── kafka.py         # Kafka topic reset functions (confluent-kafka, KAFKA_PORT_FORWARDED_BROKERS)
│   │   ├── datahub.py       # DataHub ingestion functions (acryl-datahub SDK, port 9004)
│   │   └── kestra.py        # Kestra test helpers (flow lifecycle, ActivityServer)
│   ├── api_wired/           # API-wired integration tests (REST-only)
│   │   ├── spot/            # Individual or small-sequence endpoint tests
│   │   ├── story/           # Multi-step USE_CASE scenario tests (10–100 API calls)
│   │   └── conftest.py      # API-wired-specific fixtures (extends root conftest)
│   ├── conftest.py          # Root conftest: infra fixtures, lock, dummy-data lifecycle
│   └── test_*_integration.py  # Non-API-wired tests (infra clients, Kafka, Kestra, etc.)
└── e2e/                # Playwright end-to-end tests
```

---

## Python Environment Setup

All Python test commands use `uv run` to execute within the project's `.venv` virtual environment. Before running any tests or static gates, ensure dependencies are installed:

```bash
uv sync             # Install production + dev dependencies into .venv/
```

Run `uv sync` again whenever `pyproject.toml` or `uv.lock` changes (e.g., after pulling new commits or adding a dependency). The `uv run` prefix ensures commands execute inside `.venv` without manual activation.

When a backend feature adds or changes dependencies:
1. Edit `pyproject.toml` (add/remove/update the dependency).
2. Run `uv sync` — this updates `uv.lock` and installs into `.venv/`.
3. Commit both `pyproject.toml` and `uv.lock` together.

---

## Unit Testing

### Scope

Unit tests verify business logic in isolation. They **must never** require a running dev environment — no real database, DataHub instance, Redis, Qdrant, Kestra, or Kafka connections.

### Python (Backend / API)

**Toolchain**: pytest, httpx (for FastAPI `TestClient` or async client)

**Naming**: `test_<module>.py` (e.g., `tests/unit/backend/test_quality_score.py`)

**Running**:

```bash
uv run pytest tests/unit/
```

**Mocking rules**:

- Patch all external clients at the module boundary where they are imported (not where they are defined).
- Mock DataHub SDK calls (`DataHubGraph`, `rest_emitter`) — never reach a real GMS.
- Mock all LLM calls — inject deterministic fixture responses.
- Use in-memory or SQLite-backed test fixtures for PostgreSQL-dependent logic when possible; use `unittest.mock` or `pytest-mock` otherwise.

Example pattern: patch external clients at the module boundary (not where defined), inject deterministic fixtures, assert on business outcomes. E.g., mock `get_dataset_profile` to return a profile with 30% null proportion, then assert `compute_quality_score` returns below 80.

**Static gates** (must pass before committing):

```bash
uv run mypy src/
uv run ruff check src/ tests/
```

### TypeScript (Frontend)

**Toolchain**: Jest + React Testing Library (co-located with components or under `tests/unit/frontend/`)

**Naming**: `<component>.test.ts` or `<component>.test.tsx`

**Running** (from `src/frontend/`):

```bash
npm test
```

**Mocking rules**:

- Mock API client calls (`lib/api.ts`) with Jest mocks — no real HTTP requests.
- Use `@testing-library/react` for component rendering; assert on accessible roles, not DOM internals.

**Static gates**:

```bash
npx tsc --noEmit       # from src/frontend/
npx eslint src/        # from src/frontend/
```

---

## Integration Testing

Integration tests run against the dev environment. They exercise real infrastructure: PostgreSQL, DataHub GMS, Qdrant, Kestra, Redis, and the dummy-data sources.

### Testing Modes

Integration tests support two execution modes:

| Mode | App Services | When to Use |
|------|-------------|-------------|
| **Host (default)** | Run on host (`uv run uvicorn`, `npm run dev`); Kestra runs in cluster | Normal development — fast test-and-fix loop |
| **In-cluster (on-demand)** | Deployed via Helm chart into K8s cluster | Testing Kubernetes-specific behavior only — when user explicitly requests it |

**Host mode** is the standard workflow described below. Application services run on the developer's machine and connect to port-forwarded infrastructure. Kestra runs in the cluster and is accessed via port-forward. Reinstalling the Helm chart is not required between test iterations — only the host-running process needs to be restarted. This keeps the test-and-fix loop fast.

**In-cluster mode** deploys all components (including frontend and API) into the Kubernetes cluster using the umbrella Helm chart with application subcharts enabled. This mode is significantly slower to iterate — every code change requires a container rebuild and helm upgrade. Use it only when the user explicitly requests it, for example to verify health probe behavior, ingress routing, resource limits, or network policy under real Kubernetes scheduling. See [`HELM_CHART.md §In-Cluster Testing`](feature/HELM_CHART.md#in-cluster-testing) for the deployment command.

### Workflow

Follow these seven steps in order every time you run integration tests.

> **Automation note:** When running via `uv run pytest tests/integration/`, `conftest.py` automates Steps 2 and 7 (lock acquire/release) at session scope, and Steps 3 and 6 (dummy-data reset) at module scope via the `module_dummy_data` fixture — resetting only schemas/topics declared by each test module. `conftest.py` also loads `dev_env/.env` automatically and runs `alembic upgrade head` to ensure the dataspoke schema is current. The manual commands below are for reference or when running outside pytest.

#### Step 1 — Write test scenarios and code

- Map scenarios to [Imazon](USE_CASE_en.md) domain entities (see [Test Data Design](#test-data-design)).
- **Placement**: if the test exercises only REST API endpoints (no direct Python service imports), place it under `tests/integration/api_wired/` (see [API-Wired Integration Testing](#api-wired-integration-testing)). Otherwise, place it under `tests/integration/`.
- Naming (non-api-wired): `test_<feature>_service_integration.py` for service-level tests, `test_<feature>_integration.py` for infrastructure and cross-cutting tests
- Document any test-specific data additions in the test file's module-level docstring.

#### Step 2 — Acquire the dev-env lock

Multiple testers share a single dev environment. Acquire the advisory lock before any operation that mutates state (data resets, schema migrations, ingestion runs):

```bash
# Start lock port-forward if not already running
./dev_env/lock-port-forward.sh

# Acquire lock
curl -s -X POST http://localhost:9221/lock/acquire \
  -H "Content-Type: application/json" \
  -d '{"owner": "your-name", "message": "integration test: <suite name>"}'
```

**Response codes**:

| Code | Meaning |
|------|---------|
| `200` | Lock acquired — proceed |
| `409` | Lock held by another tester — wait and retry, or coordinate offline |
| `400` | Missing `owner` field |

Do not proceed past this step if you receive `409`. The lock is advisory; bypassing it risks corrupting shared state for other testers.

When an outer process (e.g. prauto) has already acquired the lock, set `DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1` before running pytest so that `conftest.py` skips the lock acquire/release cycle.

#### Step 3 — Reset dummy data

Always reset before running integration tests, even if you believe the data is clean. The previous tester may have crashed mid-test and left the state dirty.

`conftest.py` resets dummy data via Python utilities in `tests/integration/util/` — connecting directly to port-forwarded PostgreSQL (9102), Kafka (via `DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS`), and DataHub GMS (9004).

The reset is idempotent: it drops all custom schemas `CASCADE`, recreates them, deletes and recreates all Kafka topics, and re-seeds ~600 rows and ~45 Kafka messages. The ingest then registers the 17 example-postgres tables and 3 example-kafka topics as DataHub dataset entities with `DatasetProperties` and `SchemaMetadata` aspects.

For manual reset outside pytest:

```bash
uv run python -m tests.integration.util --reset-all   # Full reset: PG + Kafka + DataHub
uv run python -m tests.integration.util --pg           # PostgreSQL only
uv run python -m tests.integration.util --kafka        # Kafka only
uv run python -m tests.integration.util --datahub      # DataHub only
```

See [`spec/feature/DEV_ENV.md §Dummy Data`](feature/DEV_ENV.md#dummy-data) for data details.

#### Step 4 — Extend dummy data if needed

If your test requires rows not provided by the baseline reset, insert them after the reset:

```bash
# Example: add a test-specific title
psql -h localhost -p 9102 -U postgres -d example_db \
  -c "INSERT INTO catalog.title_master (isbn, title, ...) VALUES (...);"
```

Document these additions in the test file's module docstring so the next developer understands what non-baseline state they depend on.

#### Step 5 — Run and iterate

```bash
uv run pytest tests/integration/
```

Fix code and re-run from Step 3 as needed. Do not re-run without resetting — tests that depend on a clean baseline will produce false results against dirty state.

#### Per-Module Dummy-Data Reset

Test modules can declare which schemas/topics/datasets they depend on via module-level constants. An autouse module-scoped fixture resets only the declared components before and after the module's tests:

```python
DUMMY_DATA_SCHEMAS: frozenset[str] = frozenset(["catalog", "orders"])
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])
```

`DUMMY_DATA_DATAHUB_SCHEMAS` triggers DataHub dataset ingestion for the specified schemas and automatically includes those schemas in the PostgreSQL reset (DataHub discovery requires the PG tables to exist).

Modules that declare no constants are no-ops. Module-scoped teardowns reset only the declared schemas/topics, so no session-level full reset is needed.

#### Step 6 — Reset dummy data before exit

Module-scoped teardowns in `conftest.py` restore the baseline for each module's declared schemas/topics. For a full manual reset (e.g. after a crash): `uv run python -m tests.integration.util --reset-all`.

#### Step 7 — Release the lock

```bash
# Normal release (owner must match)
curl -s -X POST http://localhost:9221/lock/release \
  -H "Content-Type: application/json" \
  -d '{"owner": "your-name"}'

# Force-release (if your session crashed and you cannot normal-release)
curl -s -X DELETE http://localhost:9221/lock
```

> See [`dev_env/README.md §5`](../dev_env/README.md#5-lock-the-dev-environment-multi-tester-coordination) for the full lock API reference.

### Prerequisites

Before running integration tests, ensure the dev environment is up and port-forwards are active:

```bash
cd dev_env
./datahub-port-forward.sh
./dataspoke-port-forward.sh
./dummy-data-port-forward.sh
./lock-port-forward.sh
```

Then run the health check to verify all services are actually responding (a port-forward process can be alive while the backing pod is unhealthy):

```bash
./dev_env/health-check.sh
```

The script probes each service at the application layer — PostgreSQL via `pg_isready`, Redis via `PING`, Qdrant via `/healthz`, Kestra via the flows API, DataHub GMS via `/health`, Kafka via metadata request, and the lock service via `/health`. Use `--quick` for TCP-only checks. Do not proceed if any check fails.

If a service is unhealthy, reinstall its subsystem (stop the relevant port-forward first with `--stop`, then uninstall + install, then restart the port-forward):

| Failing service | Subsystem directory |
|---|---|
| dataspoke-postgresql, dataspoke-redis, dataspoke-qdrant, dataspoke-kestra | `dev_env/dataspoke-infra/` |
| datahub-gms, datahub-kafka | `dev_env/datahub/` |
| example-postgres, example-kafka | `dev_env/dataspoke-example/` |
| lock-service | `dev_env/dataspoke-lock/` |

```bash
# Example: reinstall dataspoke infra after Kestra failure
cd dev_env
./dataspoke-port-forward.sh --stop
bash dataspoke-infra/uninstall.sh && bash dataspoke-infra/install.sh
./dataspoke-port-forward.sh
./health-check.sh
```

Integration tests do **not** require a running API server — they use in-process ASGI transport (`httpx.ASGITransport`). Kestra workflow tests call internal activity endpoints directly or use the Kestra REST API via `KestraClient`.

### Kestra Integration Test Pitfalls

Tests that exercise Kestra workflows (via `KestraClient` or by calling internal activity endpoints directly) should follow these guidelines:

#### Kestra connection

The dev-env Kestra instance is accessed via port-forward on port 9205 (set via `DATASPOKE_KESTRA_URL` in `dev_env/.env`). The `conftest.py` `kestra_client` fixture reads this from the environment. If `dev_env/.env` is not loaded (e.g., running from a worktree without it), the client cannot connect.

**Fix**: Ensure `dev_env/.env` is loaded before test collection. `conftest.py` handles this automatically, but tests run in isolation (e.g., via a subagent in a worktree) must source it explicitly.

#### Testing activity endpoints directly

Kestra calls internal activity endpoints at `/internal/activities/*` via HTTP Request tasks. Integration tests can call these endpoints directly via `httpx.AsyncClient` (ASGI transport) without needing Kestra to orchestrate. This is the preferred approach for testing activity logic in isolation.

#### Testing full Kestra flows

To test the full flow orchestration (Kestra triggering activity endpoints), the Kestra instance must be running and the flow YAML must be deployed. Use `KestraClient` to trigger flows and poll for completion. Keep timeouts short (30s max) to avoid hanging tests.

#### Stale flow executions

If a previous test run left a flow execution in `RUNNING` state, subsequent triggers may conflict depending on concurrency settings. Check for and cancel stale executions before starting new ones.

**Recovery**: Cancel stale executions via the Kestra REST API or the Kestra UI at `http://localhost:9205`.

#### Kestra test utilities (`tests/integration/util/kestra.py`)

The `kestra.py` module provides helpers for managing Kestra state during tests:

- `kill_running_executions(client, flow_id)` — kill stale executions and wait for termination
- `cleanup_test_executions(client, flow_id)` — find and delete test executions by label prefix
- `cleanup_flows(client)` — delete all DataSpoke flows from the test namespace
- `ensure_flows_registered(client)` — register all flow YAML via the registry
- `wait_for_execution_terminal(client, execution_id)` — poll until terminal state without raising on failure
- `ActivityServer` — runs a real uvicorn HTTP server (default port 8765) for Kestra activity callbacks during full-flow tests. Patches `make_*` factories in `src.api.routers.internal.activities` so LLM, Qdrant, cache, and notification use test mocks while DataHub and DB use real dev-env connections. Exposes `mock_llm`, `mock_qdrant`, `mock_cache`, `mock_notification` for per-test reconfiguration.

The `kestra_client` fixture (module-scoped) registers all flows and kills stale executions on setup, then cleans up test executions and deletes flows on teardown. The `activity_server` fixture (session-scoped) wraps `ActivityServer` for the entire test run.

### Directory Structure & Classification

Integration tests are split into two groups based on testing approach:

| Group | Location | What It Tests | Approach |
|-------|----------|---------------|----------|
| **Non-API-wired** | `tests/integration/test_*.py` | Infrastructure clients, Kafka consumers, Kestra flows, DB migrations | Direct Python function/SDK calls |
| **API-wired** | `tests/integration/api_wired/` | API + backend as a combined unit | REST API calls only (see [API-Wired Integration Testing](#api-wired-integration-testing)) |

A test belongs in `api_wired/` when its assertions use **only** REST API calls via `httpx.AsyncClient`. If the test also imports and calls backend service methods or infrastructure SDKs directly (beyond data seeding/cleanup), it belongs in the non-api-wired root.

Non-api-wired naming: `test_<feature>_service_integration.py` for service-level tests, `test_<feature>_integration.py` for infrastructure and cross-cutting tests.

**Root `conftest.py` (`tests/integration/conftest.py`) — shared fixtures and helpers:**

- **Infrastructure fixtures** (session/function scope): `integration_db_url`, `async_engine`, `async_session`, `datahub_client`, `redis_client`, `qdrant_manager`, `kestra_client`, `kafka_brokers`, `datahub_kafka_brokers`, `activity_server`
- **Lifecycle fixtures** (autouse): `alembic_at_head`, `acquire_lock`, `dummy_data_reset`, `module_dummy_data`
- **Mock fixtures**: `mock_cache` (AsyncMock Redis with get/set/publish/delete)
- **DI helper**: `override_app(*, datahub, db, redis, llm, qdrant, kestra)` — async context manager that sets FastAPI dependency overrides and yields an `httpx.AsyncClient` via ASGI transport
- **DataHub helpers**: `emit_test_dataset(client, *, urn, name, description, fields, with_ownership, with_tags, wait_seconds)`, `soft_delete_test_dataset(client, urn)`
- **Data helpers**: `make_test_urn(service, suffix)`, `seed_events(session, *, entity_type, entity_id, event_type, count)`, `cleanup_events(session, event_ids)`, `_auth_headers()`

**Kafka broker fixtures**: `conftest.py` provides two distinct Kafka broker fixtures — `kafka_brokers` (example-kafka via `DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS`, for general integration tests) and `datahub_kafka_brokers` (DataHub Kafka on port 9005, only for tests verifying DataHub↔DataSpoke connectivity).

---

## API-Wired Integration Testing

API-wired tests are a subset of integration tests that exercise the **API server and backend services as a combined unit** using only REST API calls. No direct Python service imports are used in the test logic — the test interacts with the system exclusively through HTTP endpoints via `httpx.AsyncClient` (ASGI transport).

### Scope

API-wired tests verify that the full request path works end-to-end within the backend: HTTP routing → dependency injection → service logic → infrastructure → response serialization. They complement non-api-wired integration tests (which test infrastructure clients, Kafka consumers, Kestra flows, etc. via direct Python calls) and E2E tests (which add a real browser).

### Subtypes

| Subtype | Directory | Scale | Purpose |
|---------|-----------|-------|---------|
| **Spot** | `tests/integration/api_wired/spot/` | 1–5 API calls per test | Individual endpoint CRUD, error cases, edge cases |
| **Story** | `tests/integration/api_wired/story/` | 10–100 API calls per test | End-to-end scenario covering a [`USE_CASE`](USE_CASE_en.md) through a realistic sequence of API interactions |

**Spot** tests target a single feature's API surface — e.g., create a metric config, retrieve it, update it, delete it. Each test function is self-contained and focused.

**Story** tests replay a full use-case scenario as a user would experience it through the API. A story test for UC1 (Dataset Discovery) might: search for datasets → view dataset detail → trigger ingestion → poll for completion → verify generated metadata. Story tests reference a specific UC from `spec/USE_CASE_en.md` in their module docstring.

### Naming

- Spot: `test_<feature>.py` (e.g., `test_dataset_service.py`) — the `spot/` directory provides the context, no suffix needed.
- Story: `test_<uc_id>_<short_name>.py` (e.g., `test_uc1_dataset_discovery.py`) — likewise, the `story/` directory provides the context.

### conftest.py Structure

API-wired tests use a dedicated `tests/integration/api_wired/conftest.py` that **extends** the root `tests/integration/conftest.py`. pytest's conftest inheritance means all root fixtures (infrastructure, lifecycle, mock, data helpers) are automatically available in `api_wired/` without re-importing.

The api-wired conftest provides:

- **`auth_headers`** — function-scoped fixture returning the standard JWT auth headers dict, so tests can pass `headers=auth_headers` to every request.

Each test module creates its own `http_client` fixture using the root conftest's `override_app()` context manager with the specific DI overrides needed for that module's tests. This pattern gives each module explicit control over which real infrastructure clients are injected.

Tests in `spot/` and `story/` inherit from both conftest layers.

### Running

```bash
# All integration tests (including api-wired)
uv run pytest tests/integration/

# Only api-wired tests
uv run pytest tests/integration/api_wired/

# Only spot tests
uv run pytest tests/integration/api_wired/spot/

# Only story tests
uv run pytest tests/integration/api_wired/story/
```

### Readability Principle

API-wired tests (spot and story) prioritize **readability over DRY** for HTTP request calls. Each test must show the full request payload inline so a reader can understand the test without jumping to helper definitions.

- **Inline API calls**: Write `http_client.put(…, json={…})` with the full dictionary visible in the test body. Do **not** abstract API calls into helper functions (e.g., `put_config()`).
- **Shared cleanup helpers**: Database cleanup functions (`delete_*_db`), connection constants, and `conftest.py` fixtures **may** be extracted — these are boilerplate that does not carry test intent.
- **Rationale**: The request payload _is_ the test's intent. Hiding it behind a helper obscures what the test actually verifies and forces readers to cross-reference definitions.

### Workflow

API-wired tests follow the same seven-step workflow as other integration tests (see [Integration Testing §Workflow](#workflow)). The same lock protocol, dummy-data reset, and module-level `DUMMY_DATA_*` constants apply.

---

## End-to-End (E2E) Testing

E2E tests verify the full stack through a real browser: frontend → API → backend → infrastructure.

### Toolchain

Playwright (TypeScript). Test files live in `tests/e2e/`.

### Prerequisites

All services must be running:

- Frontend: `http://localhost:3000` (Next.js dev server)
- API: `http://localhost:8000` (FastAPI)
- All port-forwards active (DataHub, DataSpoke infra, dummy-data, lock)

### Lock Protocol

E2E tests mutate dev-env state in the same way integration tests do. Apply the same seven-step workflow (Steps 2–7 from [Integration Testing](#integration-testing)):

1. Acquire lock before test run.
2. Reset dummy data.
3. Run `npx playwright test`.
4. Reset dummy data after run.
5. Release lock.

### Running

```bash
# From tests/e2e/
npx playwright test

# With UI (headed mode for debugging)
npx playwright test --headed
```

---

## Test Data Design

Integration and E2E test scenarios use **Imazon** as the canonical company context. Do not invent alternative test companies — consistency makes test failures easier to interpret.

### Imazon Dummy-Data Reference

The baseline dummy data covers these tables and use cases. Reference these when choosing what to assert against:

| Schema.Table | Rows | Primary UC | Key Characteristic |
|---|---|---|---|
| `catalog.genre_hierarchy` | 15 | UC7 | Self-referencing hierarchy |
| `catalog.title_master` | 30 | UC1, UC7 | ~18 cols, composite PK |
| `catalog.editions` | 40 | UC1, UC7 | Edition/format variants |
| `orders.order_items` | 80 | UC7 | Multi-hop join path |
| `orders.daily_fulfillment_summary` | 30 | UC3 | 1 anomalous low-volume day (Jan 15) |
| `orders.raw_events` | 100 | UC3 | Lifecycle event stream |
| `orders.eu_purchase_history` | 30 | UC5 | PII: shipping_address, payment_last4 |
| `customers.eu_profiles` | 20 | UC5 | PII: email, full_name, DOB |
| `reviews.user_ratings` | 50 | UC2 | Healthy: rating_score NOT NULL |
| `reviews.user_ratings_legacy` | 50 | UC2 | Degraded: ~30% NULL rating_score |
| `publishers.feed_raw` | 20 | UC1 | JSONB raw payload |
| `shipping.carrier_status` | 40 | UC3 | Delayed and exception statuses |
| `inventory.book_stock` | 25 | UC4 | Multi-warehouse stock |
| `marketing.eu_email_campaigns` | 15 | UC5 | Downstream of eu_profiles |
| `products.digital_catalog` | 20 | UC4 | ~30% NULL isbn |
| `content.ebook_assets` | 20 | UC4 | EPUB/PDF/MOBI assets |
| `storefront.listing_items` | 15 | UC4 | Marketplace listings |

Kafka topics: `imazon.orders.events` (20 msgs), `imazon.shipping.updates` (15 msgs), `imazon.reviews.new` (10 msgs). Seed messages are stored as JSONL files in `tests/integration/util/fixtures/kafka/`.

DataHub datasets: All 17 tables above are also registered as DataHub dataset entities (platform `postgres`, env `DEV`) via `tests/integration/util/datahub.py`, with `DatasetProperties` and `SchemaMetadata` aspects (137 columns total). The module discovers schemas/tables/columns from `example-postgres` via `asyncpg`, obtains a DataHub session token (via frontend login if `DATASPOKE_DATAHUB_TOKEN` is empty), and emits `Status`, `DatasetProperties`, and `SchemaMetadata` aspects via `DatahubRestEmitter`. Reset uses soft-delete semantics (separate from PostgreSQL CASCADE drop).

### Data Design Choices

- **UC2 anomaly**: `user_ratings_legacy` has 30% NULL `rating_score` — tests data quality detection.
- **UC3 SLA**: `daily_fulfillment_summary` has 1 anomalous day (Jan 15, `row_count=12` vs typical ~145) — tests freshness/volume anomaly detection.
- **UC4 overlap**: ~70% of `digital_catalog` titles match `title_master` by ISBN — tests cross-source lineage matching.
- **UC5 PII**: Fake but structurally realistic EU PII across DE/FR/ES/IT/NL — tests PII classification and GDPR propagation.
- **UC7 join path**: Full referential integrity `order_items → editions → title_master → genre_hierarchy` — tests multi-hop lineage.
- **ISBNs**: 978-prefix, obviously fake (e.g., `9780000000001`).

### Assertion Principles

- **Never hardcode row counts from memory.** Query actual counts from the DB within the test:
  ```python
  count = db.execute("SELECT count(*) FROM reviews.user_ratings_legacy WHERE rating_score IS NULL").scalar()
  assert count > 10   # degraded table always has significant nulls
  ```
- **Never hardcode surrogate IDs.** Look them up by a stable natural key (ISBN, URN, email).
- **Never assert on wall-clock timestamps.** Assert on relative ordering or freshness windows.

### Extending the Baseline

When a test needs rows not present in the baseline reset, insert them after the reset and document them at the top of the test file:

```python
"""
Integration tests for the validation service against the reviews domain.

Test-specific data extensions (inserted after baseline reset):
  - 5 extra rows in reviews.user_ratings_legacy with rating_score = 0
    to test boundary detection at zero-score threshold.
"""
```

---

## CI Behavior

| Test Type | Runs in CI | Requires Dev Env |
|-----------|-----------|-----------------|
| Unit tests | Yes — on every push | No — mocked dependencies only |
| Integration tests | No (out-of-scope unless a CI-specific dev-env is provisioned) | Yes |
| E2E tests | No (out-of-scope unless a CI-specific dev-env is provisioned) | Yes (full stack) |

**CI pipeline** (GitHub Actions) runs unit tests and static gates on every push and pull request:

```yaml
# Minimal CI gate (conceptual — actual workflow in .github/workflows/)
- run: uv sync
- run: uv run pytest tests/unit/ --tb=short
- run: uv run mypy src/
- run: uv run ruff check src/ tests/
- run: npx tsc --noEmit          # from src/frontend/
- run: npx eslint src/           # from src/frontend/
```

Integration and E2E tests are run manually by developers on their dev environment following the seven-step workflow above, or via a dedicated CI environment (not currently provisioned) when one becomes available.
