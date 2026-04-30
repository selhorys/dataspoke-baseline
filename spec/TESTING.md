# DataSpoke: Testing Conventions

> This document defines testing conventions, toolchains, and workflows for DataSpoke.
> Priority 3 in the spec hierarchy -- alongside [`ARCHITECTURE.md`](ARCHITECTURE.md).
> For the technology decisions that motivate the toolchain choices here, see
> [`ARCHITECTURE.md §Technology Stack`](ARCHITECTURE.md#technology-stack).
> For the dev environment and lock service used in integration/E2E tests, see
> [`spec/feature/DEV_ENV.md`](feature/DEV_ENV.md).
> For the Imazon use-case scenarios that define test data context, see
> [`spec/USE_CASE_en.md`](USE_CASE_en.md).

---

## Table of Contents

1. [Toolchain Summary](#toolchain-summary)
2. [Repository Layout](#repository-layout)
3. [Python Environment Setup](#python-environment-setup)
4. [Unit Testing](#unit-testing)
5. [Integration Testing](#integration-testing)
6. [Spot vs Api-Wired Integration Tests](#spot-vs-api-wired-integration-tests)
7. [Manual REST API Testing](#manual-rest-api-testing)
8. [End-to-End (E2E) Testing](#end-to-end-e2e-testing)
9. [Test Data Design](#test-data-design)
10. [CI Behavior](#ci-behavior)

---

## Toolchain Summary

| Layer | Language | Framework | Static Gates |
|-------|----------|-----------|-------------|
| Backend (API + services) | Python 3.13 | pytest + httpx | mypy, ruff |
| Frontend | TypeScript | Jest + React Testing Library | TypeScript compiler, ESLint |
| E2E | TypeScript | Playwright | -- |

> **Do not use the `datahub` CLI** -- it requires Python <= 3.11 and is incompatible with the
> project's Python 3.13 runtime. Use Python scripts with the `acryl-datahub` SDK instead
> (e.g., `tests/integration/util/datahub.py`).

---

## Repository Layout

Tests live under `tests/` at the repo root, mirroring `src/`:

- `tests/unit/` — `api/`, `backend/`, `shared/`, `workflows/`, `frontend/` (mirrors `src/`
  modules)
- `tests/integration/` — Dev-env-backed tests:
  - `spot/` — compact, independent tests of Python classes/functions or REST endpoints. The set
    must cover all integration scope on its own (api-wired removable without losing coverage).
  - `api_wired/` — REST-only tests that implement the five `USE_CASE_en.md` user stories
    end-to-end. One file per UC, steps mirror the user-story narrative.
  - `util/` — dummy-data reset/ingest utilities with `fixtures/sql/` and `fixtures/kafka/`.
  - `conftest.py` — root fixtures: infra, lock, dummy-data lifecycle.
- `tests/e2e/` — Playwright end-to-end tests

---

## Python Environment Setup

All Python test commands use `uv run`. Before running tests, ensure `uv sync` has been run.
Re-run it whenever `pyproject.toml` or `uv.lock` changes.

When adding dependencies: edit `pyproject.toml` -> `uv sync` -> commit both `pyproject.toml`
and `uv.lock`.

---

## Unit Testing

### Scope

Unit tests verify business logic in isolation. They **must never** require a running dev
environment.

### Python (Backend / API)

**Naming**: `test_<module>.py` (e.g., `tests/unit/backend/test_quality_score.py`)

**Running**: `uv run pytest tests/unit/`

**Mocking rules**:
- Patch external clients at the module boundary where they are imported (not where defined)
- Mock DataHub SDK calls -- never reach a real GMS
- Mock all LLM calls -- inject deterministic fixture responses
- Use in-memory or SQLite-backed fixtures for PostgreSQL-dependent logic when possible

**Static gates** (must pass before committing): `uv run mypy src/` and
`uv run ruff check src/ tests/`

### TypeScript (Frontend)

**Running** (from `src/frontend/`): `npm test`

**Mocking rules**: Mock API client calls with Jest mocks. Use `@testing-library/react` for
rendering; assert on accessible roles, not DOM internals.

**Static gates**: `npx tsc --noEmit` and `npx eslint src/` (from `src/frontend/`)

---

## Integration Testing

Integration tests run against the dev environment, exercising real infrastructure: PostgreSQL
(with pgvector), DataHub GMS, Airflow, Redis, and dummy-data sources.

### Testing Modes

The API runs **in-cluster** alongside Airflow so that Airflow DAGs can call back to the API
directly via `http://dataspoke-api:8002`. Developers access the in-cluster API via the
nginx-ingress endpoint (`http://app.<INGRESS_IP>.nip.io/api/v1/`) for running tests and manual
exploration. Code changes require `docker build` + `helm upgrade` (automated by
`dataspoke-test-mode.sh`).

### Workflow

Follow these seven steps in order. `conftest.py` automates Steps 2/7 (lock) at session scope
and Steps 3/6 (dummy-data reset) at module scope. It also loads `dev_env/.env` and runs
`alembic upgrade head`. The manual commands below are for reference.

1. **Write test scenarios** -- map to [Imazon](USE_CASE_en.md) entities. Place compact
   per-concern tests under `integration/spot/`; reserve `integration/api_wired/` for the five
   UC user-story tests (see [Spot vs Api-Wired Integration Tests](#spot-vs-api-wired-integration-tests)).
2. **Acquire dev-env lock** -- `POST http://<INGRESS_IP>:9221/lock/acquire` with
   `{"owner": "...", "message": "..."}`. Returns `409` if held by another tester. Set
   `DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1` if an outer process already holds it.
3. **Reset dummy data** -- always reset before running, even if data appears clean.
   `conftest.py` resets via `tests/integration/util/`. Manual:
   `uv run python -m tests.integration.util --reset-all`.
4. **Extend dummy data** if needed -- insert after reset, document in test file's module
   docstring.
5. **Run and iterate** -- `uv run pytest tests/integration/`. Re-run from Step 3 as needed.
6. **Reset on exit** -- module-scoped teardowns restore baseline. Manual fallback:
   `--reset-all`.
7. **Release lock** -- `POST http://<INGRESS_IP>:9221/lock/release` with `{"owner": "..."}`.
   Force-release: `DELETE http://<INGRESS_IP>:9221/lock`.

#### Per-Module Dummy-Data Reset

Test modules declare dependencies via module-level constants:

```python
DUMMY_DATA_SCHEMAS: frozenset[str] = frozenset(["catalog", "orders"])
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])
```

`DUMMY_DATA_DATAHUB_SCHEMAS` triggers DataHub ingestion for those schemas (auto-includes them
in PG reset). Modules with no constants are no-ops.

### Prerequisites

Before running integration tests, ensure the dev environment is installed and the health check
passes:

```bash
./dev_env/health-check.sh
```

The script probes each service via nginx-ingress at the application layer (PostgreSQL, Redis,
Airflow, DataHub GMS, Kafka, lock service). Do not proceed if any check fails -- reinstall the
failing subsystem:

| Failing service | Subsystem directory |
|---|---|
| dataspoke-postgresql, redis, airflow | `dev_env/dataspoke-infra/` |
| datahub-gms, datahub-kafka | `dev_env/datahub/` |
| example-postgres, example-kafka | `dev_env/dataspoke-example/` |
| lock-service | `dev_env/dataspoke-lock/` |

### Airflow Integration Test Pitfalls

- **Connection**: Airflow is accessed via nginx-ingress at `http://airflow.<INGRESS_IP>.nip.io`
  (`DATASPOKE_AIRFLOW_URL` in `dev_env/.env`). `conftest.py` loads this automatically; tests
  in worktrees must source it explicitly.
- **Direct activity testing**: Preferred approach -- call `/internal/activities/{domain}/*` via
  `httpx.AsyncClient` (ASGI transport) without Airflow orchestration.
- **Full DAG testing**: Requires running Airflow + deployed DAG files. Use `AirflowClient` to
  trigger and poll. Keep timeouts short (30s max).
- **Stale DAG runs**: Cancel via Airflow REST API or UI (`http://airflow.<INGRESS_IP>.nip.io`)
  before starting new ones.
- **Airflow utilities** (`tests/integration/util/airflow.py`): kill/cleanup stale DAG runs,
  verify DAGs, poll until terminal state.

### Test-Mode Stubs (`DATASPOKE_TEST_MODE`)

When the in-cluster API runs with `DATASPOKE_TEST_MODE=true` (set via `values-dev.yaml`
`api.testMode: true`), the `make_*` factories in `src/workflows/_common.py` return stubs:

| Factory | Stub | Behavior |
|---------|------|----------|
| `make_llm()` | `StubLLMClient` | Returns minimal dict matching Pydantic schema; `embed()` returns zero vector |
| `make_vector()` | `StubVectorManager` | `search()` returns `[]` |
| `make_cache()` | `StubRedisClient` | All ops are no-ops |
| `make_notification()` | `StubNotificationService` | `send_sla_alert()` is a no-op |

`make_datahub()` and `make_db_session()` always return real clients. Stubs are defined in
`src/workflows/_stubs.py`.

---

## Spot vs Api-Wired Integration Tests

Integration tests split into two complementary sets. Together they describe what "covered" means
for the integration layer; each set is run as its own pytest group (see
[Test Execution Groups](#test-execution-groups)).

### Spot integration tests (`tests/integration/spot/`)

Each test exercises **one concern** -- a single Python class/function or a single REST endpoint
behavior -- with the **minimum span** of setup needed to prove it works against real
infrastructure.

- **Independence**: tests do not chain -- one test's pass/fail must not depend on another's
  state. Reset fixtures handle data lifecycle.
- **Coverage rule**: the set as a whole must cover the full integration scope. If api-wired
  tests were skipped entirely, the spot set alone should still catch backend regressions.
- **Boundary**: a spot test may call dataspoke Python directly (e.g., a backend service or a
  workflow stub) **or** call the API over HTTP. Either is valid -- pick whichever proves the
  concern most directly.
- **Test-mode API server**: required only when the test calls REST. Pure-Python spot tests
  may skip `dataspoke-test-mode.sh` and rely on dev-env infra alone.
- **Reads like spec**: each test's docstring names the concern; assertions match the spec
  contract, not implementation internals.

### Api-wired integration tests (`tests/integration/api_wired/`)

Each test implements one of the five **`USE_CASE_en.md` user stories** end-to-end through the
public REST API. There is exactly one file per UC -- `test_uc{1..5}_<slug>.py`.

- **REST only**: test logic uses `httpx.AsyncClient` against `http://app.<INGRESS_IP>.nip.io/`.
  No direct imports of `src/backend`, `src/workflows`, or peripheral SDK clients in the test
  body. Setup/teardown fixtures may use `tests.integration.util` to reset/ingest data; the
  test itself stays REST-only.
- **Mirrors the narrative**: steps follow the user-story prose verbatim where possible.
  Annotate each step with the matching paragraph from `USE_CASE_en.md` so the test reads as
  the executable form of the story.
- **Test-mode API server**: required.
- **Readability over DRY**: show full request payloads inline; do not abstract API calls into
  helpers. Shared fixtures (auth, urn lookup) live in `conftest.py`.
- **Why this split**: the spot set proves the parts; api-wired proves the parts compose into
  the user-visible journey. Both must pass for an integration release.

### Running

```bash
# Spot -- some tests need the test-mode server, others do not. Run together for simplicity:
./dev_env/dataspoke-test-mode.sh --skip-build      # safe to run; idempotent
uv run python -m tests.integration.util --reset-all
DATASPOKE_TEST_MODE=true uv run pytest tests/integration/spot/

# Api-wired -- always run with the test-mode server up
./dev_env/dataspoke-test-mode.sh --skip-build
uv run python -m tests.integration.util --reset-all
DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/

# Teardown (optional; leave running if you'll iterate)
./dev_env/dataspoke-test-mode.sh --stop
```

The `require_server` fixture (in `conftest.py`) verifies (1) `DATASPOKE_TEST_MODE` is set,
(2) `/health` returns 200, (3) Airflow DAGs are registered via `/admin/dags/verify`. Spot
tests opt in by depending on the fixture; api-wired tests always depend on it.

### Test Execution Groups

Tests must run in **three separate groups**:

| Group | Command | Requires server? |
|-------|---------|-----------------|
| 1. Unit | `uv run pytest tests/unit/` | No |
| 2. Spot integration | `DATASPOKE_TEST_MODE=true uv run pytest tests/integration/spot/` | Recommended (some tests opt in) |
| 3. Api-wired integration | `DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/` | Yes |

**Why separate groups?** The test-mode server runs Airflow DAGs. Mixing spot and api-wired
runs causes competing Airflow load and flaky timing.

---

## Manual REST API Testing

Interactive endpoint testing with `curl` against the test-mode server. Useful for exploratory
testing and verifying features before writing automated tests.

### Setup

```bash
./dev_env/health-check.sh                                        # Pre-flight
./dev_env/dataspoke-test-mode.sh                                 # Build and deploy in-cluster API
uv run python -m tests.integration.util --reset-all              # Seed Imazon dummy data
```

### Authentication

```bash
# Replace <INGRESS_IP> with DATASPOKE_DEV_INGRESS_IP from dev_env/.env
TOKEN=$(curl -s -X POST http://app.<INGRESS_IP>.nip.io/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"email": "admin", "password": "admin"}' | jq -r .access_token)
```

Admin has groups `["admin", "de", "da", "dg"]` (all tiers). Tokens expire in 15 minutes.

### Making Requests

```bash
curl -s http://app.<INGRESS_IP>.nip.io/api/v1/spoke/common/data/$URN \
  -H "Authorization: Bearer $TOKEN" | jq .
```

URN format: `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.<schema>.<table>,DEV)`
(`example_db` is the dummy-data instance name emitted by `tests/integration/util/datahub.py`;
the Imazon company narrative lives at the dataset / column level, not in the URN segment.)

Route tiers: `/api/v1/spoke/common/…` (any group), `/api/v1/spoke/[de|da|dg]/…` (matching
group), `/api/v1/hub/…` (any group), `/api/v1/auth/…` (public).

### Verifying Side Effects

| Side effect | How to check |
|---|---|
| Event logged | `GET /api/v1/spoke/common/data/{urn}/event` |
| Airflow DAG | `curl http://airflow.<INGRESS_IP>.nip.io/api/v2/dags/{dag_id}` |
| DB row | `psql -h <INGRESS_IP> -p 9201 -U dataspoke -d dataspoke` |
| DataHub aspect | `curl http://datahub.<INGRESS_IP>.nip.io/gms/aspects?urn={urn}&aspect={aspect}` |

### References

- Valid URNs and request payloads: spot tests in `tests/integration/spot/`
- Full route catalogue: `spec/API.md`
- Imazon test data: [Test Data Design](#test-data-design) below

### Teardown

```bash
./dev_env/dataspoke-test-mode.sh --stop
```

---

## End-to-End (E2E) Testing

E2E tests verify the full stack through a real browser (Playwright, TypeScript, `tests/e2e/`).

**Prerequisites**: All services running -- Frontend (`http://app.<INGRESS_IP>.nip.io/`), API
(`http://app.<INGRESS_IP>.nip.io/api/v1/`), dev environment installed with nginx-ingress.

**Lock protocol**: Same seven-step workflow as integration tests (acquire lock -> reset data ->
run -> reset -> release lock).

**Running** (from `tests/e2e/`): `npx playwright test` (or `--headed` for debugging).

---

## Test Data Design

All integration and E2E scenarios use **Imazon** as the canonical company context. Do not
invent alternative test companies.

### Imazon Dummy-Data Reference

UC numbers below follow `USE_CASE_en.md` §Feature Mapping: UC1 Ingestion Control, UC2
Validation, UC3 Ontology Generation, UC4 Metadata Generation, UC5 Governance.

> **Note**: The fixture below extends the minimal Imazon profile sketched in
> [`USE_CASE_en.md`](USE_CASE_en.md#imaginary-company-profile-imazon) (5 OLTP tables and 2
> Kafka topics) with additional schemas needed to exercise PII auto-classification, multi-hop
> lineage, ontology variants, and time-series anomaly detection. The narrative scenarios in
> `USE_CASE_en.md` remain authoritative for feature behavior; this fixture is the test data
> that backs them and may be revised when integration tests are rewritten.

| Schema.Table | Rows | Primary UC | Key Characteristic |
|---|---|---|---|
| `catalog.genre_hierarchy` | 15 | UC3 | Self-referencing hierarchy — feeds ontology |
| `catalog.title_master` | 30 | UC1, UC3 | ~18 cols, composite PK |
| `catalog.editions` | 40 | UC1, UC3 | Edition/format variants |
| `orders.order_items` | 80 | UC1, UC3 | Multi-hop join path (PL/SQL lineage + ontology edges) |
| `orders.daily_fulfillment_summary` | 30 | UC2 | 1 anomalous low-volume day (Jan 15) — time-series validation |
| `orders.raw_events` | 100 | UC2 | Lifecycle event stream |
| `orders.eu_purchase_history` | 30 | UC1 | PII: shipping_address, payment_last4 (auto-classified on ingestion) |
| `customers.eu_profiles` | 20 | UC1 | PII: email, full_name, DOB |
| `reviews.user_ratings` | 50 | UC2 | Healthy: rating_score NOT NULL |
| `reviews.user_ratings_legacy` | 50 | UC2 | Degraded: ~30% NULL rating_score |
| `publishers.feed_raw` | 20 | UC1 | JSONB raw payload |
| `shipping.carrier_status` | 40 | UC2 | Delayed and exception statuses |
| `inventory.book_stock` | 25 | UC3, UC4 | Multi-warehouse stock — BOOK/PRINT concept variant |
| `marketing.eu_email_campaigns` | 15 | UC1, UC5 | Downstream of eu_profiles (ingestion + governance PII metrics) |
| `products.digital_catalog` | 20 | UC3, UC4 | ~30% NULL isbn — BOOK/DIGITAL concept variant |
| `content.ebook_assets` | 20 | UC3, UC4 | EPUB/PDF/MOBI assets |
| `storefront.listing_items` | 15 | UC3, UC4 | Marketplace listings |

Kafka topics: `imazon.orders.events` (20 msgs), `imazon.shipping.updates` (15 msgs),
`imazon.reviews.new` (10 msgs).

DataHub datasets: All 17 tables registered as DataHub entities (platform `postgres`, env `DEV`)
via `tests/integration/util/datahub.py`, with `DatasetProperties` and `SchemaMetadata` aspects
(137 columns total).

### Data Design Choices

- **UC1**: EU PII tables (`orders.eu_purchase_history`, `customers.eu_profiles`,
  `marketing.eu_email_campaigns`) carry structurally realistic EU PII across DE/FR/ES/IT/NL —
  tests PII auto-classification on ingestion
- **UC1**: `order_items -> editions -> title_master -> genre_hierarchy` full referential
  integrity — tests multi-hop PL/SQL lineage extraction
- **UC2**: `user_ratings_legacy` has 30% NULL `rating_score` — tests data quality detection
- **UC2**: `daily_fulfillment_summary` has 1 anomalous day (Jan 15) — tests time-series
  anomaly detection / predictive SLA
- **UC3**: BOOK concept has PRINT variant (`title_master`, `editions`, `book_stock`) and
  DIGITAL variant (`digital_catalog`, `ebook_assets`, `listing_items`) — tests cross-variant
  ontology construction
- **UC4**: ~70% of `digital_catalog` titles match `title_master` by ISBN — tests
  ontology-grounded doc generation and cross-source lineage matching
- **ISBNs**: 978-prefix, obviously fake (e.g., `9780000000001`)

### Assertion Principles

- **Never hardcode row counts** -- query actual counts within the test
- **Never hardcode surrogate IDs** -- look up by stable natural key (ISBN, URN, email)
- **Never assert on wall-clock timestamps** -- assert on relative ordering or freshness windows

---

## CI Behavior

| Test Type | Runs in CI | Requires Dev Env |
|-----------|-----------|-----------------|
| Unit tests | Yes -- on every push | No |
| Integration tests | No (unless CI-specific dev-env provisioned) | Yes |
| E2E tests | No (unless CI-specific dev-env provisioned) | Yes (full stack) |

CI pipeline (GitHub Actions) runs unit tests and static gates on every push/PR.
