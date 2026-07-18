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
5. [Assertion Discipline](#assertion-discipline)
6. [Integration Testing](#integration-testing)
7. [Spot vs Api-Wired Integration Tests](#spot-vs-api-wired-integration-tests)
8. [Manual REST API Testing](#manual-rest-api-testing)
9. [End-to-End (E2E) Testing](#end-to-end-e2e-testing)
10. [Test Data Design](#test-data-design)
11. [CI Behavior](#ci-behavior)

---

## Toolchain Summary

| Layer | Language | Framework | Static Gates |
|-------|----------|-----------|-------------|
| Backend (API + services) | Python 3.13 | pytest + httpx | mypy, ruff |
| Frontend | TypeScript | Vitest + React Testing Library | TypeScript compiler, ESLint |
| E2E | TypeScript | Playwright | TypeScript compiler |

### Full test surface

"Run all tests" spans five run commands across these layers — not just the Python groups:

| # | Layer | Command | Cluster? |
|---|-------|---------|----------|
| 1 | Python unit | `uv run pytest tests/unit/` | No |
| 2 | Frontend unit/component (Vitest) | `pnpm -C src/frontend test` | No |
| 3 | Spot integration | `uv run pytest tests/integration/spot/` | Rec. |
| 4 | Api-wired integration | `uv run pytest tests/integration/api_wired/` | Yes |
| 5 | E2E (Playwright) | `pnpm -C tests/e2e test` (gate: `pnpm -C tests/e2e typecheck`) | Yes |

Groups 1–2 need no cluster. Groups 3–4 are the pytest integration groups and run **separately**
(see [Python (pytest) Execution Groups](#python-pytest-execution-groups)). Group 5 owns the
dev-env lock and reset for its run. Integration/api-wired need `helm-charts/.env.dev` exported; api-wired
and E2E reset dummy data per their own protocols.

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
    end-to-end. One or more files per UC named `test_uc{n}_{nn}_<slug>.py`, where `{nn}` is a
    two-digit scenario index so files sort in user-story order; steps mirror the narrative.
  - `util/` — dummy-data reset/ingest utilities with `fixtures/sql/` and `fixtures/kafka/`.
  - `conftest.py` — root fixtures: infra, lock, dummy-data lifecycle.
- `tests/e2e/` — Playwright end-to-end tests (TypeScript), split into `use-case/` (one browser
  flow per `USE_CASE_en.md` story, mirroring api-wired) and `ground/<feature>/` (narrow per-page
  flows, mirroring spot); plus `fixtures/`, `global-setup.ts`/`global-teardown.ts`, and
  `COVERAGE.md` (the route-coverage map). Self-contained pnpm/TypeScript project.

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

**Naming**: `test_<module>.py` (e.g., `tests/unit/backend/metrics/test_service.py`)

**Running**: `uv run pytest tests/unit/`

**Mocking rules**:
- Patch external clients at the module boundary where they are imported (not where defined)
- Mock DataHub SDK calls -- never reach a real GMS
- Mock all LLM calls -- inject deterministic fixture responses
- Use in-memory or SQLite-backed fixtures for PostgreSQL-dependent logic when possible
- **Do not** drive a mocked `db.execute(...)` with a positional `side_effect=[...]` list ordered by
  call sequence. Sequence-ordered result lists are brittle: any reorder, added query, or short-circuit
  in the code under test silently shifts every downstream result and the test asserts against the wrong
  row. For logic that issues more than one query, use a **query-routing fake session** that returns
  results by inspecting the SQL/statement it receives, or a **SQLite-backed session**. See
  `tests/unit/backend/ingestion/test_service.py` for the copyable query-routing fake session.
- Give every shared mock fixture a `spec=` (e.g. `MagicMock(spec=AsyncSession)`) so attribute typos and
  renamed methods fail loud instead of silently returning a new auto-mock.

**Static gates** (must pass before committing): `uv run mypy src/` and
`uv run ruff check src/ tests/`

The asymmetry is deliberate: `tests/` is ruff-gated but not type-checked, and mypy stays
`src/`-only. Mock-heavy test code fights strict typing for no gain in defect detection, so the
ruff gate is the enforced quality bar for tests.

### TypeScript (Frontend)

**Running** (from project root): `pnpm -C src/frontend test`

**Mocking rules**: Mock API client calls with Vitest mocks (`vi.mock`). Use `@testing-library/react` for
rendering; assert on accessible roles, not DOM internals.

**Static gates**: `npx tsc --noEmit` and `npx eslint src/` (from `src/frontend/`)

---

## Assertion Discipline

These rules apply to **every** test layer — Python unit, frontend Vitest, spot integration, api-wired
integration, and Playwright E2E. A test that passes without proving anything is worse than no test: it
certifies broken behavior and blocks the next author from noticing. Author assertions so that a passing
result is only reachable when the spec'd behavior actually occurred. The four core anti-vacuity rules
apply to all layers; the dead-assertion-tuple rule is Python/mock-specific (noted inline). The related
`db.execute` mock rule lives in [Unit Testing](#unit-testing) → Mocking rules.

- **Guarded asserts need a backstop.** A conditional assertion (`if x is not None: assert x == ...` and
  similar) passes vacuously whenever the guard is false — the interesting branch never runs. Any guarded
  assert must be paired with a backstop that proves the guarded path executed (e.g. assert the value is
  present first, or assert a counter of executed branches), so the test fails when the value is absent
  instead of skipping silently.
- **Absence assertions require injection.** A negative or absence assertion (`assert x not in result`,
  `assert field is None`) is meaningful only when the test injected the thing whose absence it checks. If
  nothing was injected, the assertion is trivially true and proves nothing. Seed the value, then assert it
  was filtered/removed/absent.
- **Filter/query/matching tests seed both sides.** A test of a filter, query predicate, or matching rule
  must seed **both** rows that match and rows that do not, then assert the matching rows appear and the
  non-matching rows are excluded. Seeding only matching rows cannot catch an over-broad predicate.
- **Mutation tests verify a concrete side effect.** A test of a mutating operation must read back and
  assert the concrete side effect (the DB row, the emitted event, the DataHub aspect, the changed field),
  not merely a 2xx status. A handler that returns 200 and does nothing must fail the test.
- **No dead assertion-message tuples** *(Python/mock)*. The `mock.assert_called_*` / `assert_*` family takes no message
  argument. Writing `mock.assert_called_once(), ("msg")` evaluates the assertion, discards its result, and
  builds a dead tuple — the message silences nothing and hides that no real check ran. Never attach a
  trailing tuple to a mock `assert_*` call; if a failure message is wanted, use a plain `assert`
  expression.
- **Citations must exist at the cited location.** A `spec:` citation attached to a test must reference
  text that actually exists at the cited document and section. A citation to a rule that is not present in
  the cited section is forbidden: it launders the very violation it purports to excuse. Reviewers verify
  each citation against the cited lines.

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
and Steps 3/6 (dummy-data reset) at module scope. It also loads `helm-charts/.env.dev` and runs
`alembic upgrade head`. The manual commands below are for reference.

1. **Write test scenarios** -- map to [Imazon](USE_CASE_en.md) entities. Place compact
   per-concern tests under `integration/spot/`; reserve `integration/api_wired/` for the five
   UC user-story tests (see [Spot vs Api-Wired Integration Tests](#spot-vs-api-wired-integration-tests)).
2. **Acquire dev-env lock** -- `POST $DATASPOKE_TEST_LOCK_URL/lock/acquire` with
   `{"owner": "...", "message": "..."}`. Returns `409` if held by another tester. Set
   `DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1` if an outer process already holds it.
   `DATASPOKE_TEST_LOCK_URL` is auto-populated in `helm-charts/.env.dev` by `install.sh`
   (`http://<INGRESS_IP>:9221` in managed ingress mode; `http://127.0.0.1:9221`
   via `bin/port-forward.sh` in shared ingress mode).
3. **Reset dummy data** -- always reset before running, even if data appears clean.
   `conftest.py` resets via `tests/integration/util/`. Manual:
   `uv run python -m tests.integration.util --reset-seed`.
4. **Extend dummy data** if needed -- insert after reset, document in test file's module
   docstring.
5. **Run and iterate** -- `uv run pytest tests/integration/`. Re-run from Step 3 as needed.
6. **Reset on exit** -- module-scoped teardowns restore baseline. Manual fallback:
   `--reset-seed`.
7. **Release lock** -- `POST $DATASPOKE_TEST_LOCK_URL/lock/release` with `{"owner": "..."}`.
   Force-release: `DELETE $DATASPOKE_TEST_LOCK_URL/lock`.

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

#### Event Log Purge Policy

Events are purged at two scopes, and only these two:

- **Wholesale, per run.** `reset_all()` (`tests/integration/util/dataspoke_db.py`) TRUNCATEs every
  table in the `dataspoke` schema, `dataspoke.events` included, under **both** `--reset-seed` and
  `--reset-all`.
- **URN-scoped, per test.** Api-wired's autouse `purge_urns` fixture (`api_wired/conftest.py`) reads
  the module-level `URNS_TO_PURGE` list and hard-deletes each URN's operational rows — including its
  `dataspoke.events` rows — before and after every test.

No **wholesale per-module** events purge exists, and none is wanted: a TRUNCATE sweep per module
reintroduces reset overhead the per-run purge already covers, while the URN-scoped fixture gives
api-wired its per-test slot at negligible cost. Beyond these purges, event isolation rests on
identity-bound assertions (see [Integration Lifecycle & Isolation](#integration-lifecycle--isolation)).

### Airflow Integration Test Pitfalls

- **Connection**: Airflow is accessed via nginx-ingress at `http://airflow.<INGRESS_IP>.nip.io`
  (`DATASPOKE_TEST_AIRFLOW_URL` in `helm-charts/.env.dev`). `conftest.py` loads this automatically;
  tests in worktrees must source it explicitly.
- **Direct activity testing**: Preferred approach -- call `/internal/activities/{domain}/*` via
  `httpx.AsyncClient` (ASGI transport) without Airflow orchestration.
- **Full DAG testing**: Requires running Airflow + deployed DAG files. Use `AirflowClient` to
  trigger and poll. Keep timeouts short — 30s is the default cap. The exemption is a whole-run
  `trigger_and_wait` budget on a scheduler-triggered DAG: that budget spans trigger→terminal as one
  window, and the scheduler's pickup latency inside it is unbounded and not something the test can
  shorten. Exempt call sites are enumerated here, currently exactly one —
  `tests/integration/spot/test_auth_role_sync_dag.py:135` (`auth-role-sync-daily`, 180s). Extending
  the exemption to another call site requires editing this list.
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

### Integration Lifecycle & Isolation

Integration tests share a single dev cluster with singleton state (peripheral config, SMTP config,
RuntimeConfig stub toggles, ontogen conf) and concurrent runs. Tests that mutate shared state or assert
on shared logs must isolate themselves so they neither leak into nor flake against other runs.

- **Snapshot → mutate → verified restore.** A test that mutates any singleton or global (peripheral
  config, SMTP config, RuntimeConfig stub toggles, ontogen conf, etc.) reads the current value first,
  mutates, and restores the snapshot in a `finally`. The restore is **asserted**, not assumed — read the
  value back after restoring and assert it matches the snapshot, so a failed restore fails the test that
  caused it rather than silently corrupting later tests.
- **Bind event assertions by identity, never by count.** Assert on events by `run_id` or an `after=`
  timestamp captured before the action. Never assert a count-delta over a `limit=` window (e.g. "event
  count went from 3 to 4"): concurrent runs on the shared cluster invalidate the window and the assertion
  flakes.
- **All cleanup runs in `try/finally`.** Data seeded, locks taken, or state mutated by a test must be
  torn down in a `finally` block so a mid-test failure still restores the baseline.
- **Reset helpers fail loud and carry no baked-in credentials.** Utility reset helpers raise on any reset
  failure — never swallow the error and continue against a dirty baseline. They read all credentials from
  the environment (the `DATASPOKE_TEST_*` block in `helm-charts/.env.dev`); no credential is hardcoded in
  a helper.

---

## Spot vs Api-Wired Integration Tests

Integration tests split into two complementary sets. Together they describe what "covered" means
for the integration layer; each set is run as its own pytest group (see
[Python (pytest) Execution Groups](#python-pytest-execution-groups)).

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
public REST API. Files are named `test_uc{1..5}_{nn}_<slug>.py`, where `{nn}` is a two-digit
scenario index so files sort in user-story order; a UC may be split across multiple files when
the user story has independent scenarios (e.g., `test_uc1_01_datahub_managed.py`,
`test_uc1_02_active_custom_postgres.py`, and `test_uc1_03_passive_kafka.py` all belong to UC1).

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

### Tier placement rationales

Two concerns sit at spot for reasons that are not visible from the test file alone. They belong at
spot; lifting them into api-wired would violate the rules above.

- **Validation `assertionRunEvent` DataHub read-back**
  (`spot/test_validation_passive_store.py::test_post_result_emits_assertion_run_event`). Proving the
  aspect landed requires the DataHubGraph SDK plus `build_assertion_urn` from `src/backend` — both
  barred from api-wired test bodies by the REST-only rule above.
- **Ontogen candidate review** (`spot/test_ontogen_review.py`, covering
  `POST /spoke/ontogen/result/{node,edge,triple}/{id}/method/review`). Under stub mode the Producer
  returns an empty payload, so no candidate rows persist and an api-wired arc has nothing to review.
  Reaching a reviewable candidate means seeding `llm_pending` rows through the ORM, which the
  REST-only rule bars from api-wired test bodies. The UC3 api-wired arc does cover seed
  create-then-enable (`PATCH .../attr/seed/{id}/attr/enabled`); only candidate review is delegated
  to spot.

### Running

Export `helm-charts/.env.dev` into the shell before invoking pytest — `conftest.py` and `util/*.py` consume the `DATASPOKE_TEST_*` block it contains: `set -a && source helm-charts/.env.dev && set +a`.

```bash
# Spot
./helm-charts/bin/install.sh --profile dev --components api --skip-build   # safe to run; idempotent
uv run python -m tests.integration.util --reset-seed
set -a && source helm-charts/.env.dev && set +a && uv run pytest tests/integration/spot/

# Api-wired
./helm-charts/bin/install.sh --profile dev --components api --skip-build
uv run python -m tests.integration.util --reset-seed
set -a && source helm-charts/.env.dev && set +a && uv run pytest tests/integration/api_wired/

# Teardown (optional; leave running if you'll iterate)
kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
```

The session-scoped `runtime_conf` fixture (in `tests/integration/conftest.py`) GETs `/api/v1/admin/conf` once and asserts the three infra stubs (`stub_redis_client`, `stub_pgvector_manager`, `stub_notification_service`) are true. `stub_llm_client` is intentionally unchecked so real-LLM tests can run with it false; each such test guards inline as the first statement of its body (`if runtime_conf.get("stub_llm_client"): pytest.skip(...)`), not via a decorator. The two UCs carry different shapes: UC3 is a single test parametrized over `llm_mode` in `["stub", "real"]` (node ids `test_uc3_ontology_generation[stub]` / `[real]`), while UC4 keeps two distinct tests (`_under_stub` and `_with_real_llm`) because they assert genuinely different contracts rather than duplicating one arc. UC3's guard is **symmetric** — `real` skips when `stub_llm_client` is true, `stub` skips when it is false — so exactly one case runs per dev-env configuration and each runs against the LLM its node id names. The `require_server` fixture additionally verifies `/health` returns 200 and Airflow DAGs are registered via `/admin/dags/verify`. Spot tests opt in by depending on the fixture; api-wired tests always depend on it.

### Python (pytest) Execution Groups

The Python (pytest) tests must run in **three separate groups** — this split is about Airflow
contention, not the full test surface (frontend Vitest and E2E are separate layers; see
[Full test surface](#full-test-surface)):

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
> use the `/test-manual-api-wired` skill.

### Setup

```bash
./helm-charts/bin/health-check.sh                                  # Pre-flight
./helm-charts/bin/install.sh --profile dev --components api        # Build and deploy in-cluster API
uv run python -m tests.integration.util --reset-seed               # Seed Imazon dummy data
```

### Authentication

```bash
# Replace <INGRESS_IP> with DATASPOKE_KUBE_INGRESS_IP from helm-charts/.env.dev
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
`/api/v1/auth/…` (public).

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

E2E tests verify the full stack through a real browser: real frontend -> real API -> real
DataHub / PostgreSQL / Kafka, nothing mocked (Playwright, TypeScript, `tests/e2e/`). They are the
frontend counterpart of the integration layer — the **use-case group** mirrors api-wired and the
**ground group** mirrors spot. The colocated Vitest tests (`src/frontend/`) remain the mocked
unit/component tier; E2E never mocks the API.

### Prerequisites

All services running, including the **cluster-deployed frontend**:
`./helm-charts/bin/install.sh --profile dev --frontend cluster` deploys the UI at
`http://app.<INGRESS_IP>.nip.io/`; the API is at `http://api.<INGRESS_IP>.nip.io/api/v1/`. Run
`./helm-charts/bin/health-check.sh` first. Playwright's `baseURL` resolves from
`PLAYWRIGHT_BASE_URL`, defaulting to the cluster URL derived from `DATASPOKE_KUBE_INGRESS_DOMAIN`
in `helm-charts/.env.dev`.

### Two groups

E2E splits into two complementary groups, the same way integration splits into spot and api-wired.
Each is a Playwright project group under `tests/e2e/`.

#### Use-case group (`tests/e2e/use-case/`)

One browser flow per `USE_CASE_en.md` user story — the executable UI form of the matching
`tests/integration/api_wired/test_uc{1..5}_*.py` file and the `/test-manual-ui` walkthrough. Files
mirror the api-wired split: `uc{1..5}-{nn}-<slug>.spec.ts`, where `{nn}` is a two-digit scenario
index so files sort in user-story order (UC1 has three —
`uc1-01-datahub-managed`, `uc1-02-active-custom-postgres`, `uc1-03-passive-kafka`).

- **Mirrors the narrative**: steps follow the user-story prose and the api-wired step sequence
  verbatim; annotate each step with the matching `USE_CASE_en.md` paragraph.
- **Dual confirmation**: each step asserts the **UI** state (toast, row, badge, redirect) **and**
  independently verifies the **backend** state via Playwright's `APIRequestContext` — the same
  REST read-back the api-wired step asserts. A UI that renders stale or cached state must not pass.
- **Gestures from FRONTEND specs**: map each REST mutation to its page + gesture per
  `spec/feature/FRONTEND_*.md` (create form + Submit, run panel `dry_run` toggle, Approve/Reject
  card, conf editor Save, delete behind ConfirmDialog).
- **Readability over DRY**: inline the gesture sequence and expected values per step; do not hide
  flows behind helpers. Shared setup (auth, env, URN constants) lives in `fixtures/`.
- **LLM mode**: UC3/UC4 each carry a stub-mode variant and a gated real-LLM variant — the real-LLM
  variant `test.skip`s unless `stub_llm_client` is false in `/admin/conf`. The gating concept is
  shared with api-wired; the test shape is not — api-wired expresses the same split via
  parametrization (UC3) or separate tests (UC4), while E2E keeps two variants throughout.

#### Ground group (`tests/e2e/ground/<feature>/`)

Many narrow, single-concern UI-flow tests, each proving one page behavior against the real stack —
the spot-tier analogue.

- **One concern per test**, independent (reset fixtures handle data lifecycle), minimum setup;
  the test reads like its concern.
- **Coverage rule**: the use-case and ground groups **together** cover the entire frontend route
  surface — every route under `src/frontend/app/`. Unlike backend spot, the ground group need not
  be self-sufficient: the Vitest unit/component tests and the use-case group already cover much,
  and ground fills only what they leave. `tests/e2e/COVERAGE.md` maps every route to its covering
  test(s) and is the acceptance artifact for "fully covered".
- **Boundary vs Vitest**: ground tests cover real-stack UI flows (a real role-gated nav, a form
  submit that lands in the DB and re-renders, polling that reflects a real event). Presentational
  and pure-logic assertions stay in the colocated Vitest tests — do not duplicate them here.

### Selectors

Prefer Playwright's user-facing locators (`getByRole`, `getByLabel`, `getByText`). Add a
`data-testid` to a component only where a semantic locator is insufficient (recharts widgets,
dynamic table rows, status badges), per `spec/feature/FRONTEND_BASIC.md §Testability`.

### Authentication

`global-setup` logs in once per role through the real `/login` page and persists each session as a
Playwright `storageState` (the refresh token is an HttpOnly cookie; the app refreshes the in-memory
access token on load). Playwright projects are keyed on role (admin / editor / reader); role-gated
tests select the matching project. Non-admin users are provisioned via the admin API during setup.

### Lock + reset

Same dev-env lock and data-reset protocol as integration tests, driven from Playwright's
`globalSetup`/`globalTeardown` by **reusing the existing Python utilities** — acquire the lock
(`POST $DATASPOKE_TEST_LOCK_URL/lock/acquire`, honouring `DATASPOKE_DEV_ENV_LOCK_PREACQUIRED`),
`uv run python -m tests.integration.util --reset-seed`, run, reset, release. UC4 uses the same
`--uc4-seed` / `--uc4-restore` staging as the manual skill.

### Running

```bash
./helm-charts/bin/install.sh --profile dev --frontend cluster   # deploy the cluster UI
./helm-charts/bin/health-check.sh
set -a && source helm-charts/.env.dev && set +a                 # export DATASPOKE_* for fixtures
pnpm -C tests/e2e install
pnpm -C tests/e2e test                                          # --headed / --ui to debug
```

Static gate: `pnpm -C tests/e2e typecheck` (`tsc --noEmit`). Real-LLM use-case variants run only
after `PATCH /api/v1/admin/conf {"stub_llm_client": false}` (≤30s propagation); revert afterward.

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

These test-data-specific rules complement the layer-wide anti-vacuity rules in
[Assertion Discipline](#assertion-discipline).

- **Never hardcode row counts** -- query actual counts within the test
- **Never hardcode surrogate IDs** -- look up by stable natural key (ISBN, URN, email)
- **Never assert on wall-clock timestamps** -- assert on relative ordering or freshness windows

---

## CI Behavior

A hosted CI pipeline is **future work, not yet built** by decision — there is no `.github/workflows/`
and no service runs the gates automatically. Until a pipeline exists, the static gates below are a
**manual pre-commit obligation on the author**: run them locally and keep them green before committing.

Author-run pre-commit gates:

- `uv run ruff check src/ tests/`
- `uv run mypy src/`
- Frontend (from `src/frontend/`): `npx tsc --noEmit` and `npx eslint src/`
- E2E: `pnpm -C tests/e2e typecheck` (`tsc --noEmit`)

The table below is the **intended target state** for a future pipeline — which layers a CI would run
automatically once built, not a description of anything running today:

| Test Type | Target: runs in CI | Requires Dev Env |
|-----------|--------------------|-----------------|
| Unit tests + static gates | Yes -- on every push/PR | No |
| Integration tests | No (unless CI-specific dev-env provisioned) | Yes |
| E2E tests | No (unless CI-specific dev-env provisioned) | Yes (full stack) |
