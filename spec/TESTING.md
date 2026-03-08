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
6. [End-to-End (E2E) Testing](#end-to-end-e2e-testing)
7. [Test Data Design](#test-data-design)
8. [CI Behavior](#ci-behavior)

---

## Toolchain Summary

| Layer | Language | Framework | Static Gates |
|-------|----------|-----------|-------------|
| Backend (API + services) | Python 3.13 | pytest + httpx | mypy, ruff |
| Frontend | TypeScript | Jest + React Testing Library | TypeScript compiler, ESLint |
| E2E | TypeScript | Playwright | — |

---

## Repository Layout

Tests live under `tests/` at the repo root, mirroring `src/`:

```
tests/
├── unit/
│   ├── api/            # FastAPI route tests (no running server)
│   ├── backend/        # Service logic tests
│   ├── shared/         # DataHub client wrapper, shared model tests
│   └── frontend/       # Jest tests (or co-located in src/frontend/)
├── integration/        # Dev-env-backed integration tests
└── e2e/                # Playwright end-to-end tests
```

> The `tests/` directory does not yet exist in the repo. It is created when the first tests are written. This spec defines the intended layout in advance.

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

Unit tests verify business logic in isolation. They **must never** require a running dev environment — no real database, DataHub instance, Redis, Qdrant, or Kafka connections.

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

Integration tests run against the dev environment. They exercise real infrastructure: PostgreSQL, DataHub GMS, Qdrant, Temporal, Redis, and the dummy-data sources.

### Testing Modes

Integration tests support two execution modes:

| Mode | App Services | When to Use |
|------|-------------|-------------|
| **Host (default)** | Run on host (`uv run uvicorn`, `npm run dev`, `uv run python -m worker`) | Normal development — fast test-and-fix loop |
| **In-cluster (on-demand)** | Deployed via Helm chart into K8s cluster | Testing Kubernetes-specific behavior only — when user explicitly requests it |

**Host mode** is the standard workflow described below. Application services run on the developer's machine and connect to port-forwarded infrastructure. Reinstalling the Helm chart is not required between test iterations — only the host-running process needs to be restarted. This keeps the test-and-fix loop fast.

**In-cluster mode** deploys all components (including frontend, API, and workers) into the Kubernetes cluster using the umbrella Helm chart with application subcharts enabled. This mode is significantly slower to iterate — every code change requires a container rebuild and helm upgrade. Use it only when the user explicitly requests it, for example to verify health probe behavior, ingress routing, resource limits, or network policy under real Kubernetes scheduling. See [`HELM_CHART.md §In-Cluster Testing`](feature/HELM_CHART.md#in-cluster-testing) for the deployment command.

### Workflow

Follow these seven steps in order every time you run integration tests:

#### Step 1 — Write test scenarios and code

- Map scenarios to [Imazon](USE_CASE_en.md) domain entities (see [Test Data Design](#test-data-design)).
- Place test files under `tests/integration/`, mirroring `src/` structure.
- Naming: `test_<feature>_integration.py`
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

Always reset before running integration tests, even if you believe the data is clean. The previous tester may have crashed mid-test and left the state dirty:

```bash
cd dev_env && ./dummy-data-reset.sh && ./dummy-data-ingest.sh
```

`dummy-data-reset.sh` is idempotent: it drops all custom schemas `CASCADE`, recreates them, deletes and recreates all Kafka topics, and re-seeds ~600 rows and ~45 Kafka messages. `dummy-data-ingest.sh` then registers the 17 example-postgres tables as DataHub dataset entities with `DatasetProperties` and `SchemaMetadata` aspects. Always run both in sequence — the ingest depends on the reset having populated PostgreSQL. See [`spec/feature/DEV_ENV.md §Dummy Data Reset`](feature/DEV_ENV.md#dummy-data-reset) and [`§DataHub Ingestion`](feature/DEV_ENV.md#datahub-ingestion).

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

#### Step 6 — Reset dummy data before exit

Restore the baseline state after your test run so the next tester starts clean:

```bash
cd dev_env && ./dummy-data-reset.sh && ./dummy-data-ingest.sh
```

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

The DataSpoke application services must also be running on the host:

```bash
# API (from repo root)
uv run uvicorn src.api.main:app --reload --port 8000

# Workers (from repo root)
uv run python -m src.workflows.worker
```

### Directory Structure

```
tests/integration/
├── conftest.py         # Shared fixtures: DB connections, API client, env config
├── test_ingestion_integration.py
├── test_validation_integration.py
├── test_search_integration.py
└── test_metrics_integration.py
```

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

All test scenarios use **Imazon** as the canonical company context. Do not invent alternative test companies — consistency makes test failures easier to interpret.

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

Kafka topics: `imazon.orders.events` (20 msgs), `imazon.shipping.updates` (15 msgs), `imazon.reviews.new` (10 msgs).

DataHub datasets: All 17 tables above are also registered as DataHub dataset entities (platform `postgres`, env `PROD`) via `dummy-data-ingest.sh`, with `DatasetProperties` and `SchemaMetadata` aspects (137 columns total).

### Assertion Principles

- **Never hardcode row counts from memory.** Query actual counts from the DB within the test:
  ```python
  count = db.execute("SELECT count(*) FROM reviews.user_ratings_legacy WHERE rating_score IS NULL").scalar()
  assert count > 10   # degraded table always has significant nulls
  ```
- **Never hardcode surrogate IDs.** Look them up by a stable natural key (ISBN, URN, email).
- **Never assert on wall-clock timestamps.** Assert on relative ordering or freshness windows.

### Extending the Baseline

When a test needs rows not present in the baseline reset, insert them after `dummy-data-reset.sh` and document them at the top of the test file:

```python
"""
Integration tests for the validation service against the reviews domain.

Test-specific data extensions (inserted after dummy-data-reset.sh):
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
