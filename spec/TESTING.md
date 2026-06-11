# DataSpoke: Testing Conventions

> This document defines testing conventions, toolchains, and workflows for DataSpoke.
> Priority 3 in the spec hierarchy -- alongside [`ARCHITECTURE.md`](ARCHITECTURE.md).
> For the technology decisions that motivate the toolchain choices here, see
> [`ARCHITECTURE.md §Technology Stack`](ARCHITECTURE.md#technology-stack).
> For the deployment subsystem and dev-lock service used in integration/E2E tests, see
> [`spec/feature/HELM_CHART.md`](feature/HELM_CHART.md).
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
| Frontend | TypeScript | Vitest + React Testing Library | TypeScript compiler, ESLint |
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
    end-to-end. One or more files per UC named `test_uc{n}_<slug>.py`, covering the UC's
    scenarios; steps mirror the user-story narrative.
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

**Running** (from project root): `pnpm -C src/frontend test`

**Mocking rules**: Mock API client calls with Vitest mocks (`vi.mock`). Use `@testing-library/react` for
rendering; assert on accessible roles, not DOM internals.

**Static gates**: `npx tsc --noEmit` and `npx eslint src/` (from `src/frontend/`)

---

## Integration Testing

Integration tests run against the dev environment, exercising real infrastructure: PostgreSQL
(with pgvector), DataHub GMS, Airflow, Redis, and dummy-data sources.

### Testing Modes

The API runs **in-cluster** alongside Airflow so that Airflow DAGs can call back to the API
directly via `http://dataspoke-api:8002`. Developers access the in-cluster API via the
nginx-ingress endpoint (`http://api.<INGRESS_IP>.nip.io/api/v1/`) for running tests and manual
exploration. Code changes require `docker build` + `helm upgrade` (automated by
`./helm-charts/bin/install.sh --profile dev --components api`).

### Workflow

Follow these seven steps in order. `conftest.py` automates Steps 2/7 (lock) at session scope
and Steps 3/6 (dummy-data reset) at module scope. It also loads `helm-charts/.env` and runs
`alembic upgrade head`. The manual commands below are for reference.

1. **Write test scenarios** -- map to [Imazon](USE_CASE_en.md) entities. Place compact
   per-concern tests under `integration/spot/`; reserve `integration/api_wired/` for the five
   UC user-story tests (see [Spot vs Api-Wired Integration Tests](#spot-vs-api-wired-integration-tests)).
2. **Acquire dev-env lock** -- `POST http://<INGRESS_IP>:9221/lock/acquire` with
   `{"owner": "...", "message": "..."}`. Returns `409` if held by another tester. Set
   `DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1` if an outer process already holds it.
3. **Reset dummy data** -- always reset before running, even if data appears clean.
   `conftest.py` resets via `tests/integration/util/`. Manual:
   `uv run python -m tests.integration.util --reset-seed`.
4. **Extend dummy data** if needed -- insert after reset, document in test file's module
   docstring.
5. **Run and iterate** -- `uv run pytest tests/integration/`. Re-run from Step 3 as needed.
6. **Reset on exit** -- module-scoped teardowns restore baseline. Manual fallback:
   `--reset-seed`.
7. **Release lock** -- `POST http://<INGRESS_IP>:9221/lock/release` with `{"owner": "..."}`.
   Force-release: `DELETE http://<INGRESS_IP>:9221/lock`.

#### Per-Module Dummy-Data Reset

Test modules declare dependencies via module-level constants:

```python
DUMMY_DATA_SCHEMAS: frozenset[str] = frozenset(["catalog", "orders"])
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])
```

`DUMMY_DATA_DATAHUB_SCHEMAS` triggers DataHub PG ingestion for those schemas (auto-includes
them in PG reset). `DUMMY_DATA_DATAHUB_TOPICS` triggers DataHub Kafka registration for those
topics (auto-includes them in Kafka reset). Modules with no constants are no-ops.

### Prerequisites

Before running integration tests, ensure the dev environment is installed and the health check
passes:

```bash
./helm-charts/bin/health-check.sh
```

The script probes each service via nginx-ingress at the application layer (PostgreSQL, Redis,
Airflow, DataHub GMS, Kafka, lock service). Do not proceed if any check fails -- reinstall the
failing subsystem with `./helm-charts/bin/install.sh --profile dev --components <name>`:

| Failing service | Component |
|---|---|
| dataspoke-postgresql, redis, airflow, api | `dataspoke-infra` |
| datahub-gms, datahub-kafka | `datahub` |
| example-postgres, example-kafka | `dummy-data` |
| lock-service | `dev-lock` |
| Langfuse | `langfuse` |

The util has two reset modes. `--reset-all` produces an empty baseline (no Imazon entities
anywhere) — useful for testing UC1 ingestion against a blank slate. `--reset-seed` produces
the seeded baseline used by UC2/3/4/5 — full Imazon datasets present in PostgreSQL, Kafka,
and DataHub with descriptions and typed columns.

### Airflow Integration Test Pitfalls

- **Connection**: Airflow is accessed via nginx-ingress at `http://airflow.<INGRESS_IP>.nip.io`
  (`DATASPOKE_TEST_AIRFLOW_URL` in `helm-charts/.env`). `conftest.py` loads this automatically;
  tests in worktrees must source it explicitly.
- **Direct activity testing**: Preferred approach -- call `/internal/activities/{domain}/*` via
  `httpx.AsyncClient` (ASGI transport) without Airflow orchestration.
- **Full DAG testing**: Requires running Airflow + deployed DAG files. Use `AirflowClient` to
  trigger and poll. Keep timeouts short (30s max).
- **Stale DAG runs**: Cancel via Airflow REST API or UI (`http://airflow.<INGRESS_IP>.nip.io`)
  before starting new ones.
- **Airflow utilities** (`tests/integration/util/airflow.py`): kill/cleanup stale DAG runs,
  verify DAGs, poll until terminal state.

### Stub Toggles (RuntimeConfig)

Four boolean fields on the singleton `RuntimeConfig` row gate real-vs-stub client wiring at request time. Each is flippable online via `PATCH /api/v1/admin/conf`; changes propagate in ≤30s via the existing TTL cache on `RuntimeConfigDTO`. The factories in `src/workflows/_common.py` each accept a `stub: bool = False` keyword arg; per-request dependency providers in `src/api/dependencies.py` read the matching RuntimeConfig field and pass it through.

| RuntimeConfig field | Factory | Real client | Stub class | Stub behavior |
|---|---|---|---|---|
| `stub_redis_client` | `make_redis_client(stub=)` | `RedisClient` | `StubRedisClient` | All ops are no-ops |
| `stub_llm_client` | `make_llm_client(stub=, ...)` | `LLMClient` | `StubLLMClient` | Returns minimal dict matching Pydantic schema; `embed()` returns a deterministic unit vector |
| `stub_pgvector_manager` | `make_pgvector_manager(stub=)` | `PgVectorManager` | `StubPgVectorManager` | `search()` returns `[]` |
| `stub_notification_service` | `make_notification_service(stub=)` | `NotificationService` | `StubNotificationService` | `send_sla_alert()` is a no-op |

Defaults are all `false` (real clients — prod-safe). The dev profile's `helm-charts/bin/post-install/seed-runtime-config.sh` PATCHes all four to `true` so the dev API runs fully stubbed by default; integration suites depend on this. `make_datahub()` always returns the real DataHub client (no stub toggle). Stub classes are defined in `src/workflows/_stubs.py`.

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
  may skip the API rebuild and rely on already-deployed dev-env infra alone.
- **Reads like spec**: each test's docstring names the concern; assertions match the spec
  contract, not implementation internals.

### Api-wired integration tests (`tests/integration/api_wired/`)

Each test implements one of the five **`USE_CASE_en.md` user stories** end-to-end through the
public REST API. Files are named `test_uc{1..5}_<slug>.py`; a UC may be split across multiple
files when the user story has independent scenarios (e.g., `test_uc1_active_custom_postgres.py`
and `test_uc1_passive_kafka_external_script.py` both belong to UC1).

- **REST only**: test logic uses `httpx.AsyncClient` against `http://api.<INGRESS_IP>.nip.io/`.
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

Export `helm-charts/.env` into the shell before invoking pytest — `conftest.py` and `util/*.py` consume the `DATASPOKE_TEST_*` block it contains: `set -a && source helm-charts/.env && set +a`.

```bash
# Spot
./helm-charts/bin/install.sh --profile dev --components api --skip-build   # safe to run; idempotent
uv run python -m tests.integration.util --reset-seed
set -a && source helm-charts/.env && set +a && uv run pytest tests/integration/spot/

# Api-wired
./helm-charts/bin/install.sh --profile dev --components api --skip-build
uv run python -m tests.integration.util --reset-seed
set -a && source helm-charts/.env && set +a && uv run pytest tests/integration/api_wired/

# Teardown (optional; leave running if you'll iterate)
kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
```

The session-scoped `runtime_conf` fixture (in `tests/integration/conftest.py`) GETs `/api/v1/admin/conf` once and asserts the three infra stubs (`stub_redis_client`, `stub_pgvector_manager`, `stub_notification_service`) are true. `stub_llm_client` is intentionally unchecked so real-LLM tests (UC3/UC4 `_with_real_llm` variants) can run with it false; per-test skip decorators consult `runtime_conf.stub_llm_client` and skip when stubbed. The `require_server` fixture additionally verifies `/health` returns 200 and Airflow DAGs are registered via `/admin/dags/verify`. Spot tests opt in by depending on the fixture; api-wired tests always depend on it.

### Test Execution Groups

Tests must run in **three separate groups**:

| Group | Command | Requires server? |
|-------|---------|-----------------|
| 1. Unit | `uv run pytest tests/unit/` | No |
| 2. Spot integration | `uv run pytest tests/integration/spot/` | Recommended (some tests opt in) |
| 3. Api-wired integration | `uv run pytest tests/integration/api_wired/` | Yes |

**Why separate groups?** The dev API runs Airflow DAGs. Mixing spot and api-wired runs causes competing Airflow load and flaky timing.

To exercise real-LLM tests against the dev API, first `PATCH /api/v1/admin/conf {"stub_llm_client": false}` (≤30s propagation), then run pytest. Revert afterward with `PATCH ... {"stub_llm_client": true}`.

---

## Manual REST API Testing

Interactive endpoint testing with `curl` against the test-mode server. Useful for exploratory
testing and verifying features before writing automated tests.

> For a guided harness that walks an existing api-wired test scenario step-by-step (extracts
> requests from the test file, pauses for approval before each mutation, probes side effects),
> use the `/test-api-wired-manual` skill.

### Setup

```bash
./helm-charts/bin/health-check.sh                                  # Pre-flight
./helm-charts/bin/install.sh --profile dev --components api        # Build and deploy in-cluster API
uv run python -m tests.integration.util --reset-seed               # Seed Imazon dummy data
```

### Authentication

```bash
# Replace <INGRESS_IP> with DATASPOKE_KUBE_INGRESS_IP from helm-charts/.env
TOKEN=$(curl -s -X POST http://api.<INGRESS_IP>.nip.io/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"email": "admin", "password": "admin"}' | jq -r .access_token)
```

Admin tokens carry `role = "Admin"` and expire in 15 minutes.

### Making Requests

```bash
curl -s "http://api.<INGRESS_IP>.nip.io/api/v1/spoke/common/data/$URN/attr/ingestion/conf" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

URN format: `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.<schema>.<table>,DEV)`
(`example_db` is the dummy-data instance name emitted by `tests/integration/util/datahub.py`;
the Imazon company narrative lives at the dataset / column level, not in the URN segment.)

Route namespaces: `/api/v1/spoke/{governance,ingestion,validation,ontogen,metagen}/…`,
`/api/v1/hub/…`, `/api/v1/auth/…` (public).

### Verifying Side Effects

| Side effect | How to check |
|---|---|
| Event logged | `GET /api/v1/spoke/<feature>/data/{urn}/event` (one of `ingestion`, `validation`, `metagen`) |
| Airflow DAG | `curl http://airflow.<INGRESS_IP>.nip.io/api/v2/dags/{dag_id}` |
| DB row | `psql -h $DATASPOKE_TEST_POSTGRES_HOST -p $DATASPOKE_TEST_POSTGRES_PORT -U $DATASPOKE_TEST_POSTGRES_USER -d $DATASPOKE_TEST_POSTGRES_DB` |
| DataHub aspect | `curl http://datahub.<INGRESS_IP>.nip.io/gms/aspects?urn={urn}&aspect={aspect}` |

### References

- Valid URNs and request payloads: spot tests in `tests/integration/spot/`
- Full route catalogue: `spec/API.md`
- Imazon test data: [Test Data Design](#test-data-design) below

### Teardown

```bash
kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
```

---

## End-to-End (E2E) Testing

E2E tests verify the full stack through a real browser (Playwright, TypeScript, `tests/e2e/`).

**Prerequisites**: All services running -- Frontend (`http://app.<INGRESS_IP>.nip.io/`), API
(`http://api.<INGRESS_IP>.nip.io/api/v1/`), dev environment installed with nginx-ingress.

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

> **Note**: The fixture covers the minimal Imazon profile sketched in
> [`USE_CASE_en.md`](USE_CASE_en.md#imaginary-company-profile-imazon) (catalog, orders,
> customers, reviews, and shipping domains with 2 Kafka topics). The narrative scenarios in
> `USE_CASE_en.md` remain authoritative for feature behavior; this fixture is the test data
> that backs them and may be revised when integration tests are rewritten.

| Schema.Table | Rows | Primary UC | Key Characteristic |
|---|---|---|---|
| `catalog.title_master` | 30 | UC1, UC4 | 17 cols, `isbn` sole PK; every column has a description |
| `catalog.editions` | 40 | UC1 | Per-format edition rows; `isbn` joins to `title_master` |
| `customers.eu_profiles` | 20 | UC3, UC5 | EU customer accounts; `user_id` VARCHAR PK; GDPR PII surface |
| `reviews.user_ratings` | 50 | UC2, UC3 | Ratings; `user_id` → `eu_profiles`, `edition_id` → `editions` |
| `orders.daily_fulfillment_summary` | 30 | UC2 | 1 anomalous day (Jan 15, warehouse outage) — detectable via historical-baseline GET |
| `shipping.carrier_status` | 30 | UC3 | Carrier scan events; `order_id` joins to both Kafka topics |

Kafka topics: `imazon.orders.events` (20 msgs), `imazon.shipping.updates` (15 msgs).

DataHub datasets: All 6 tables registered as DataHub entities (platform `postgres`, env `DEV`)
via `tests/integration/util/datahub.py`, with `DatasetProperties` and `SchemaMetadata` aspects
(53 columns total). Both Kafka topics registered as Kafka platform entities with field
descriptions.

### Data Design Choices

- **UC2**: `daily_fulfillment_summary` has 1 anomalous day (Jan 15) — pipeline POSTs
  daily `row_cnt`; tomorrow's task GETs the prior 30-day series via the historical-baseline
  endpoint and detects the outlier without re-aggregating
- **UC3 cross-dataset join paths**: five signal paths available for ontology inference:
  `catalog.editions.isbn` → `catalog.title_master.isbn` (edition↔title);
  `reviews.user_ratings.edition_id` → `catalog.editions.edition_id` (rating↔edition);
  `reviews.user_ratings.user_id` → `customers.eu_profiles.user_id` (rating↔customer);
  `shipping.carrier_status.order_id` → `imazon.orders.events.order_id` (PG↔Kafka);
  `shipping.carrier_status.order_id` → `imazon.shipping.updates.order_id` (PG↔Kafka)
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
