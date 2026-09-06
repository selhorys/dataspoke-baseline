"""Shared fixtures for integration tests against the dev-env infrastructure.

Services are accessed via nginx-ingress (HTTP) or TCP passthrough ports.
All endpoint values are read from helm-charts/.env.dev, which is populated by the
install scripts.  Tier B TCP defaults:
- PostgreSQL (dataspoke)  : <INGRESS_IP>:9201
- Redis                   : <INGRESS_IP>:9202
- DataHub Kafka           : <INGRESS_IP>:9005
- Example PostgreSQL      : <INGRESS_IP>:9102
- Example Kafka           : <INGRESS_IP>:9104
- Lock service            : <INGRESS_IP>:9221
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from tests.integration.util.auth import login_headers
from tests.integration.util.db_url import build_postgres_url

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def _resolve_env_file() -> Path | None:
    """Locate helm-charts/.env.dev, searching the project root upward.

    Walking upward is what makes a git worktree work: the env file is untracked and
    lives only in the main worktree, so a checkout under `.prauto/worktrees/<branch>/`
    has none of its own. Every consumer must resolve it through here — a caller that
    instead assumes `<project root>/helm-charts/.env.dev` silently reads a
    nonexistent path under a worktree and misreports that as an environment fault.
    """
    start = Path(__file__).resolve().parents[2]
    for candidate in (start, *start.parents):
        env_path = candidate / "helm-charts" / ".env.dev"
        if env_path.is_file():
            return env_path
    return None


def _load_dotenv() -> None:
    """Load helm-charts/.env.dev into os.environ (without overwriting existing vars)."""
    env_path = _resolve_env_file()
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _unquote_env_value(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def _unquote_env_value(value: str) -> str:
    """Reverse the quoting ``env_file_set_var`` applies when it writes the file.

    ``helm-charts/bin/lib/helpers.sh`` wraps a value in single quotes whenever it
    carries anything the shell would act on -- whitespace, ``$``, a backtick,
    ``#`` -- escaping embedded apostrophes as ``'\\''``.  The real consumer of
    the file is ``source``, which undoes that; this parser reads the text
    directly and has to undo it too, or such a value arrives here still wearing
    its quotes and every comparison against it fails on characters no test ever
    put there.

    Deliberately a local copy of ``tests.integration.util.env_file`` rather than
    an import of it: ``tests/integration/util/__init__.py`` eagerly imports the
    DataHub helpers, which require a populated environment at import time — so
    importing it from conftest would make collection depend on the very env this
    function exists to load.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        return inner.replace("'\\''", "'") if value[0] == "'" else inner
    return value


_load_dotenv()


def _promote_test_runtime_overrides() -> None:
    """Promote named DATASPOKE_DEV_* values into the runtime DATASPOKE_* names
    that src/ Pydantic Settings reads. Required when test code imports src/
    helpers (e.g. src.backend.auth.tokens.issue_access_token) and must sign with
    the same secret the API pod uses — the chart-generated secret is mirrored
    into .env as DATASPOKE_DEV_JWT_SECRET_KEY by install.sh's
    _sync_env_from_secret.

    Promotion is per name, never by prefix: the dev tiers of dev-only install
    inputs and auto-populated dev access share the DATASPOKE_DEV_* prefix (spec:
    feature/HELM_CHART.md §Configuration — Five-Tier Env Vars), so promoting the
    whole prefix would push peripheral install inputs into app-runtime settings.
    The JWT secret is the only promoted name; add one here only with a reason.
    """
    if "DATASPOKE_DEV_JWT_SECRET_KEY" in os.environ:
        os.environ["DATASPOKE_JWT_SECRET_KEY"] = os.environ["DATASPOKE_DEV_JWT_SECRET_KEY"]


_promote_test_runtime_overrides()

# ── Ingress URL helper ────────────────────────────────────────────────────────


def _shared_ingress_url() -> str:
    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    return f"http://api.{domain}"


# ── Shared infrastructure env vars ────────────────────────────────────────────

_datahub_gms_url = os.environ["DATASPOKE_DEV_DATAHUB_GMS_URL"]
_datahub_token = os.environ.get("DATASPOKE_DEV_DATAHUB_TOKEN", "")

_redis_host = os.environ["DATASPOKE_DEV_REDIS_HOST"]
_redis_port = int(os.environ["DATASPOKE_DEV_REDIS_PORT"])
_redis_password = os.environ.get("DATASPOKE_DEV_REDIS_PASSWORD", "")

_kafka_brokers = os.environ["DATASPOKE_DEV_DUMMY_DATA_KAFKA_BROKERS"]
_datahub_kafka_brokers = os.environ["DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS"]

_airflow_url = os.environ.get("DATASPOKE_DEV_AIRFLOW_URL", "http://localhost:8080")
_airflow_user = os.environ.get("DATASPOKE_DEV_AIRFLOW_USER", "")
_airflow_password = os.environ.get("DATASPOKE_DEV_AIRFLOW_PASSWORD", "")

_lock_owner = os.environ.get(
    "DATASPOKE_DEV_LOCK_OWNER",
    f"integration-test-{os.environ.get('USER', 'unknown')}",
)


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def integration_db_url() -> URL:
    """The dev-env DataSpoke Postgres URL, credentials carried as ``URL`` fields.

    Host, port, user and password are required env with no fallback: a missing
    ``DATASPOKE_DEV_POSTGRES_*`` block means `helm-charts/.env.dev` was not exported,
    and raising ``KeyError`` here names the cause instead of letting every DB-touching
    test fail against a fallback host. ``DATASPOKE_DEV_POSTGRES_DB`` is the one
    defaulted key (``dataspoke``) — it names the cluster's fixed database rather than a
    coordinate that varies per developer.

    Covered by ``tests/unit/integration_conftest/test_integration_db_url.py``.
    """
    return build_postgres_url(
        host=os.environ["DATASPOKE_DEV_POSTGRES_HOST"],
        port=os.environ["DATASPOKE_DEV_POSTGRES_PORT"],
        user=os.environ["DATASPOKE_DEV_POSTGRES_USER"],
        password=os.environ["DATASPOKE_DEV_POSTGRES_PASSWORD"],
        db=os.environ.get("DATASPOKE_DEV_POSTGRES_DB", "dataspoke"),
    )


@pytest_asyncio.fixture(scope="session")
async def async_engine(integration_db_url: URL) -> AsyncGenerator[AsyncEngine]:
    from sqlalchemy import pool as sa_pool

    eng = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def async_session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def datahub_client():
    if not _datahub_token:
        pytest.skip("DATASPOKE_DEV_DATAHUB_TOKEN not set")
    return DataHubClient(gms_url=_datahub_gms_url, token=_datahub_token)


# ── API server + auth fixtures (shared by spot + api_wired layers) ────────────


@pytest.fixture(scope="session", autouse=True)
def require_server(runtime_conf) -> None:  # noqa: ARG001 — runtime_conf performs stub preflight
    """Assert the dev cluster is healthy, the API server is reachable, infra stubs
    are on, and DAGs are registered.

    Shared by both the spot and api-wired layers (inherited from this parent
    conftest). Checks:
    0. `helm-charts/bin/health-check.sh` passes. Enforcing the cluster-wide gate in
       pytest means it holds for every caller (human, coding-agent CLI, or CI).
       Skipped when the explicit health-check script is missing or not executable.
    1. runtime_conf preflight confirms stub_redis_client, stub_pgvector_manager,
       stub_notification_service are true (stub_llm_client intentionally excluded
       so real-LLM tests can run).
    2. GET /health returns 200.
    3. Best-effort POST /internal/admin/bootstrap so the admin account exists even
       after a prior `--reset-all` wiped it (real login failures still surface in
       step 4). spot previously skipped this and silently relied on the admin being
       seeded elsewhere.
    4. POST /api/v1/admin/dags/verify returns 200 (admin JWT auth).

    If any check fails, the test session is aborted with a clear message.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    health_check = repo_root / "helm-charts" / "bin" / "health-check.sh"
    if os.access(health_check, os.X_OK):
        # The dev-env lock is already held by this point (runtime_conf -> acquire_lock),
        # so --keep-lock tells health-check.sh not to treat it as foreign or stale.
        # --env-file pins the same file _load_dotenv read, which under a git worktree
        # is the main worktree's; without it health-check.sh resolves the env file
        # against its own tree and exits 2 on a perfectly healthy cluster.
        cmd = [str(health_check), "--keep-lock"]
        env_file = _resolve_env_file()
        if env_file is not None:
            cmd += ["--env-file", str(env_file)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "helm-charts/bin/health-check.sh did not finish within 60s — treating "
                "that as unhealthy (an unreachable API server or a stalled endpoint can "
                "outlast a bounded probe). Run it directly to see where it stalls."
            )
        if result.returncode != 0:
            pytest.fail(
                f"helm-charts/bin/health-check.sh failed (exit {result.returncode}). "
                "Integration tests would fail misleadingly against a broken cluster.\n\n"
                f"health-check output:\n{result.stdout}{result.stderr}\n\n"
                "Reinstall the failing subsystem (per AGENTS.md §Integration Test Protocol) "
                "with ./helm-charts/bin/install.sh --profile dev --components <name>:\n"
                "  airflow / postgres / redis → dataspoke-infra\n"
                "  datahub-gms / kafka        → datahub\n"
                "  example-postgres/kafka     → dummy-data\n"
                "  lock-service               → dev-lock"
            )

    base_url = _shared_ingress_url()

    # Check liveness — /health has no /api/v1 prefix (mounted at root)
    try:
        resp = httpx.get(f"{base_url}/health", timeout=10.0)
        if resp.status_code != 200:
            pytest.fail(
                f"GET /health returned {resp.status_code}. Server not running? Try: "
                "./helm-charts/bin/install.sh --profile dev --components api --skip-build"
            )
    except httpx.ConnectError as exc:
        pytest.fail(
            f"Cannot connect to API at {base_url}: {exc}. Try: "
            "./helm-charts/bin/install.sh --profile dev --components api --skip-build"
        )

    # Ensure the bootstrap admin user exists before minting a token.
    internal_token = os.environ.get("DATASPOKE_DEV_INTERNAL_TOKEN", "")
    if internal_token:
        try:
            httpx.post(
                f"{base_url}/internal/admin/bootstrap",
                headers={"X-Internal-Token": internal_token, "Content-Type": "application/json"},
                content="{}",
                timeout=30.0,
            )
        except Exception:
            pass  # Best-effort — may already exist; token login below will surface real failures.

    # Obtain admin token and verify DAGs
    try:
        headers = login_headers(base_url, "dataspoke@dataspoke.local", "dataspoke")
    except Exception as exc:
        pytest.fail(f"Cannot obtain admin token: {exc}")

    try:
        verify_resp = httpx.post(
            f"{base_url}/api/v1/admin/dags/verify",
            headers=headers,
            timeout=30.0,
        )
        if verify_resp.status_code != 200:
            pytest.fail(
                f"POST /admin/dags/verify returned {verify_resp.status_code}: {verify_resp.text}. "
                "DAGs may not be registered. Try: "
                "./helm-charts/bin/install.sh --profile dev --components api --skip-build"
            )
    except Exception as exc:
        pytest.fail(f"POST /admin/dags/verify failed: {exc}")

    yield  # type: ignore[misc]


# ── Expiry-aware admin token ──────────────────────────────────────────────────
#
# The admin JWT expires in jwt_access_token_expire_minutes (15 min, see
# src/shared/settings.py). A session-scoped token minted once would 401 mid-run on
# any suite that runs longer than 15 minutes. Instead we cache the token and re-mint
# when it is within a safety margin of its own `exp` claim, so every request carries
# a non-expired token without hitting /auth/token on every test.

_ADMIN_TOKEN_CACHE: dict[str, float | str | None] = {"token": None, "exp": 0.0}
_ADMIN_TOKEN_REFRESH_MARGIN_S = 120.0  # re-mint this many seconds before exp


def _jwt_exp_epoch(token: str) -> float:
    """Return the ``exp`` claim (epoch seconds) from a JWT, without verifying it.

    Reads the real expiry from the token rather than hardcoding the configured
    lifetime, so the refresh margin stays correct if settings change.
    """
    import base64

    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    return float(payload["exp"])


def _current_admin_token() -> str:
    """Return a non-expired admin JWT, re-minting when within the refresh margin.

    Shared by the ``admin_headers`` and ``admin_token`` fixtures so both always
    surface the same cached, valid token.
    """
    import time

    now = time.time()
    cached = _ADMIN_TOKEN_CACHE["token"]
    if cached is None or now >= float(_ADMIN_TOKEN_CACHE["exp"]) - _ADMIN_TOKEN_REFRESH_MARGIN_S:
        headers = login_headers(_shared_ingress_url(), "dataspoke@dataspoke.local", "dataspoke")
        token = headers["Authorization"].removeprefix("Bearer ")
        _ADMIN_TOKEN_CACHE["token"] = token
        _ADMIN_TOKEN_CACHE["exp"] = _jwt_exp_epoch(token)
        return token
    return str(cached)


@pytest.fixture
def admin_headers(require_server) -> dict[str, str]:  # noqa: ARG001 — gates on server readiness
    """Authorization header dict for the bootstrap admin user, minted per test.

    Function-scoped and backed by ``_current_admin_token`` so every test starts
    with a non-expired token even in suites that run past the 15-min JWT lifetime.
    """
    return {"Authorization": f"Bearer {_current_admin_token()}"}


@pytest.fixture
def admin_token(require_server) -> str:  # noqa: ARG001 — gates on server readiness
    """Admin JWT access token (bare token, no ``Bearer `` prefix), minted per test."""
    return _current_admin_token()


@pytest.fixture(scope="session")
def internal_headers() -> dict[str, str]:
    """Session-scoped X-Internal-Token header dict for internal routes."""
    return {"X-Internal-Token": os.environ["DATASPOKE_DEV_INTERNAL_TOKEN"]}


@pytest.fixture(scope="session")
def kafka_brokers() -> str:
    """Example-kafka (dummy-data namespace) for general integration tests."""
    return _kafka_brokers


@pytest.fixture(scope="session")
def datahub_kafka_brokers() -> str:
    """DataHub Kafka — only for tests verifying DataHub↔DataSpoke connection."""
    return _datahub_kafka_brokers


@pytest_asyncio.fixture
async def redis_client():
    client = RedisClient(host=_redis_host, port=_redis_port, password=_redis_password)
    yield client
    await client.close()


@pytest.fixture(scope="session")
def _rate_limit_redis():
    """Session-scoped sync Redis connection backing the per-test rate-limit flush.

    The flush stays per-test (auth register/token tests need a fresh 5/min window
    each), but the TCP connection is opened once per session rather than once per
    test — removing a connect/close round-trip from every integration test.
    """
    import redis as _redis_sync

    from src.api.middleware.rate_limit import RATE_LIMIT_REDIS_DB

    # Import the index rather than repeating it: the limiter keeps its counters in
    # their own logical DB, away from the cache, the SET NX locks and the refresh
    # revocation set. A hardcoded 0 here would flush the wrong keyspace and the
    # only symptom would be auth tests bleeding their 5/min window into each other.
    client = _redis_sync.Redis(
        host=_redis_host,
        port=_redis_port,
        password=_redis_password or None,
        db=RATE_LIMIT_REDIS_DB,
    )
    yield client
    client.close()


@pytest.fixture(autouse=True)
def _flush_rate_limit_keys(_rate_limit_redis) -> None:
    """Drop slowapi `LIMITER/*` keys before each test so the per-IP 5/min limit
    on `/auth/register` and `/auth/token` does not bleed across tests in the
    same minute window. Per-test (isolation required); reuses the session client."""
    try:
        for key in _rate_limit_redis.scan_iter("LIMITS:LIMITER/*"):
            _rate_limit_redis.delete(key)
    except Exception:
        pass


async def _bootstrap_schema(db_url: URL) -> None:
    from sqlalchemy import pool as sa_pool

    from src.shared.config import EMBEDDING_DIMENSION
    from src.shared.db.models import Base

    engine = create_async_engine(db_url, poolclass=sa_pool.NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS dataspoke"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS age"))
            await conn.execute(text('SET search_path = ag_catalog, "$user", public, pg_catalog'))
            await conn.execute(
                text(
                    """
                    DO $$ BEGIN
                        PERFORM ag_catalog.create_graph('dataspoke_ontogen');
                    EXCEPTION WHEN others THEN
                        IF SQLERRM LIKE '%already exists%' OR SQLSTATE = '42710' THEN
                            NULL;
                        ELSE
                            RAISE;
                        END IF;
                    END $$
                    """
                )
            )
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS dataspoke.dataset_embeddings (
                        dataset_urn TEXT PRIMARY KEY,
                        platform TEXT,
                        tags JSONB,
                        owners JSONB,
                        quality_score FLOAT,
                        has_pii BOOLEAN,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        embedding vector({EMBEDDING_DIMENSION}) NOT NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS dataset_embeddings_embedding_hnsw_idx "
                    "ON dataspoke.dataset_embeddings USING hnsw (embedding vector_cosine_ops)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS node_embeddings_embedding_hnsw_idx "
                    "ON dataspoke.node_embeddings USING hnsw (embedding vector_cosine_ops)"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def schema_bootstrap(integration_db_url: URL) -> None:
    """Idempotent schema setup: schema, extensions, AGE graph, ORM tables, HNSW indexes."""
    asyncio.run(_bootstrap_schema(integration_db_url))
    yield  # type: ignore[misc]


@pytest.fixture(scope="session", autouse=True)
def acquire_lock() -> None:
    # When run from prauto phases.sh, the lock is already held externally.
    if os.environ.get("DATASPOKE_DEV_LOCK_PREACQUIRED"):
        yield  # type: ignore[misc]
        return

    # Two ingress modes: managed (LoadBalancer IP populated in DATASPOKE_KUBE_INGRESS_IP)
    # and shared (no IP — lock reached on 127.0.0.1 via port-forward). install.sh writes
    # DATASPOKE_DEV_LOCK_URL for both modes, so prefer it; the IP is only a legacy fallback
    # and is read defensively (empty in shared mode) to avoid a KeyError.
    _ingress_ip = os.environ.get("DATASPOKE_KUBE_INGRESS_IP", "")
    lock_url = os.environ.get("DATASPOKE_DEV_LOCK_URL", f"http://{_ingress_ip}:9221")
    try:
        resp = httpx.post(
            f"{lock_url}/lock/acquire",
            json={"owner": _lock_owner, "message": "integration test suite"},
            timeout=5.0,
        )
        if resp.status_code == 409:
            pytest.skip("Dev-env lock held by another tester")
        resp.raise_for_status()
    except httpx.ConnectError:
        pytest.skip(f"Lock service not reachable at {lock_url}")

    yield  # type: ignore[misc]

    try:
        release_resp = httpx.post(
            f"{lock_url}/lock/release",
            json={"owner": _lock_owner},
            timeout=5.0,
        )
    except httpx.ConnectError:
        pass
    else:
        # A failed release strands the lock for the next tester — surface it loudly
        # rather than silently ignoring the response.
        assert release_resp.status_code == 200, (
            f"Dev-env lock release failed: POST {lock_url}/lock/release returned "
            f"{release_resp.status_code}: {release_resp.text}"
        )


def _reset_all_dummy_data() -> None:
    """Reset all dummy data via Python utilities.

    Per spec/TESTING.md Steps 3 & 6: always reset dummy data before and after
    integration test runs so the baseline state is clean.
    """

    from tests.integration.util import datahub, kafka, postgres

    asyncio.run(postgres.reset_all())
    kafka.reset_all()
    asyncio.run(datahub.seed())


@pytest.fixture(scope="session", autouse=True)
def dummy_data_reset(acquire_lock) -> None:  # noqa: ARG001 — depends on lock
    """Placeholder for session-level dummy-data lifecycle.

    Per-module selective resets (module_dummy_data) handle both setup and
    teardown for modules that declare DUMMY_DATA_SCHEMAS / DUMMY_DATA_TOPICS /
    DUMMY_DATA_DATAHUB_SCHEMAS.  Individual tests clean up their own transient
    data.  No session-level full reset is needed — it was too slow and
    redundant with module-level teardowns.
    """
    yield  # type: ignore[misc]


# Record of the *source-store* baseline the most recently-provisioned module left
# standing (its resolved (schemas, topics, datahub_schemas, datahub_topics)
# 4-tuple). When the next module declares an identical requirement, the standing
# baseline already satisfies the source legs and re-running them is skipped: no
# test body mutates the source example-postgres/Kafka, hard-deletes the example
# DataHub datasets, or perturbs their core aspects, so the seeded sources are
# stable across a run once provisioned.
#
# That invariant covers the sources only. dataset_registry is NOT stable the same
# way — test bodies run sweeps of their own against stub DataHub clients, and
# reconcile_registry soft-flags every registry row absent from what the stub
# enumerates (src/shared/db/registry.py::reconcile_registry step B). So this record
# gates the reset/ingest legs alone; the registry reconcile runs for every module
# declaring a DUMMY_DATA_DATAHUB_* requirement regardless of it.
#
# Process-global, so the spot and api-wired groups (separate pytest invocations,
# per TESTING.md §Execution Groups) each start cold.
_PROVISIONED_BASELINE: tuple | None = None


@pytest.fixture(scope="module", autouse=True)
def module_dummy_data(request) -> None:
    """Autouse module-scoped fixture for selective dummy-data reset.

    Test modules declare dependencies via module-level constants:
        DUMMY_DATA_SCHEMAS: frozenset[str]          — PostgreSQL schemas to reset
        DUMMY_DATA_TOPICS: frozenset[str]            — Kafka topics to reset
        DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str]   — DataHub PG datasets to ingest
        DUMMY_DATA_DATAHUB_TOPICS: frozenset[str]    — DataHub Kafka topics to ingest

    DUMMY_DATA_DATAHUB_SCHEMAS implies the corresponding DUMMY_DATA_SCHEMAS
    (DataHub discovery requires the PG tables to exist).
    DUMMY_DATA_DATAHUB_TOPICS implies the corresponding DUMMY_DATA_TOPICS
    (DataHub Kafka registration requires the topics to exist in Kafka).

    Modules that declare no constants are no-ops.

    A module that declares either DUMMY_DATA_DATAHUB_* constant is provisioned
    through to dataset_registry, not only into DataHub: the ingestion datahub-sync
    sweep (run on schedule by the datahub-sync-hourly DAG) follows the ingest legs.
    Only the sweep does what this fixture needs: it is the one that INSERTs a
    registry row for any URN DataHub holds and the only writer of the row's
    filter-attribute columns (origin / platform_urn / tag_urns /
    glossary_term_urns / is_primary / attrs_synced_at). Every other writer does
    strictly less — ValidationService.upsert_config's ensure_dataset_registered
    inserts a bare row for the one URN being configured, POST
    /internal/admin/datahub/sync flips the flag bidirectionally but inserts
    nothing, and the ingest legs' own _mark_registry_registered is an UPDATE over
    rows that already exist, never an INSERT. Both gaps bite here — after --reset-all (which
    TRUNCATEs the registry and, unlike --reset-seed, runs no sweep) a freshly
    ingested URN has no row at all until this reconcile inserts one, and a
    dataset_filter carrying a tag or origin predicate resolves against attribute
    columns nothing else writes. Everything resolving a dataset scope reads that
    registry — src/backend/_dataset_filter.py selects rows with
    datahub_registered=true — so without the reconcile the module would inherit its
    precondition from whatever a previous run happened to leave behind (spec:
    TESTING.md §Spot integration tests — "tests do not chain -- one test's
    pass/fail must not depend on another's state").

    The reconcile is also what un-poisons the registry between modules, which is
    why it is exempt from the skip guard below: test bodies run sweeps of their own
    against stub DataHub clients that enumerate a handful of URNs (e.g.
    spot/test_internal_activities.py, spot/test_ingestion_cli_pipeline_inheritance.py),
    and reconcile_registry soft-flags every registry row absent from that
    enumeration. A later module reusing the standing source baseline would otherwise
    start against a registry the previous module left deregistered. The guarantee is
    per-declaration, not global: a module that declares nothing stays a no-op and may
    still run against a poisoned registry, which is sound only because declaring
    nothing is the module's own statement that it reads no provisioned data — the
    repair lands at the next module that does declare one.

    Setup only (no teardown reset): the next module that needs a clean baseline
    resets at its own setup, and modules with no dummy-data needs are unaffected —
    so a symmetric teardown reset would only re-do work the next setup redoes.
    The reset/ingest legs are themselves skipped when the standing baseline already
    equals this module's requirement (see _PROVISIONED_BASELINE); the registry
    reconcile is not. Independent legs run concurrently: the PG-source reset and
    Kafka-source reset are independent of each other; the DataHub ingest reads the
    freshly-reset PG rows, so it follows the reset, and the registry reconcile
    enumerates the estate the ingest just added to, so it follows the ingest.
    """

    global _PROVISIONED_BASELINE

    from tests.integration.util import kafka, postgres

    schemas = getattr(request.module, "DUMMY_DATA_SCHEMAS", None)
    topics = getattr(request.module, "DUMMY_DATA_TOPICS", None)
    datahub_schemas = getattr(request.module, "DUMMY_DATA_DATAHUB_SCHEMAS", None)
    datahub_topics = getattr(request.module, "DUMMY_DATA_DATAHUB_TOPICS", None)

    # DataHub ingest requires PG tables for schema discovery.
    if datahub_schemas:
        schemas = (schemas or frozenset()) | datahub_schemas

    # DataHub Kafka registration requires the topics to exist in Kafka.
    if datahub_topics:
        topics = (topics or frozenset()) | datahub_topics

    schemas = frozenset(schemas or ())
    topics = frozenset(topics or ())
    datahub_schemas = frozenset(datahub_schemas or ())
    datahub_topics = frozenset(datahub_topics or ())

    requirement = (schemas, topics, datahub_schemas, datahub_topics)
    needs_provision = bool(schemas or topics or datahub_schemas or datahub_topics)
    needs_registry = bool(datahub_schemas or datahub_topics)

    # Skip guard, scoped to the source legs: the previously-provisioned module left
    # this exact source baseline standing and no test body dirties the sources.
    reuse_sources = requirement == _PROVISIONED_BASELINE

    # Nothing left to do only when there is no requirement at all, or the sources
    # are standing AND the module needs no registry state.
    if not needs_provision or (reuse_sources and not needs_registry):
        yield  # type: ignore[misc]
        return

    async def _provision() -> None:
        from tests.integration.util import datahub

        if not reuse_sources:
            # Reset legs: PG source (async) + Kafka source (sync, off-thread) are
            # mutually independent → run concurrently.
            reset_coros = []
            if schemas:
                reset_coros.append(postgres.reset_schemas(schemas))
            if topics:
                reset_coros.append(asyncio.to_thread(kafka.reset_topics, topics))
            if reset_coros:
                await asyncio.gather(*reset_coros)

            # DataHub ingest legs: PG-dataset ingest reads the freshly-reset PG rows,
            # so it MUST follow the reset above; the two ingests are independent of
            # each other → run concurrently.
            ingest_coros = []
            if datahub_schemas:
                ingest_coros.append(datahub.ingest_pg_datasets(schemas=datahub_schemas))
            if datahub_topics:
                ingest_coros.append(datahub.ingest_kafka_datasets(topics=datahub_topics))
            if ingest_coros:
                await asyncio.gather(*ingest_coros)

        if needs_registry:
            # Registry reconcile — a full estate sweep (IngestionService.sync()), not a
            # read of the URNs just emitted: it enumerates all of DataHub, reconciles
            # dataset_registry against that whole set (inserting URNs it holds,
            # soft-flagging rows it does not), refreshes the filter-attribute columns,
            # mirrors DATAHUB_MANAGED ingestion sources, books INGESTION events and
            # stamps the shared datahub-api peripheral_health row. Estate-wide and
            # destructive rather than narrow and additive, so it runs strictly after
            # the ingest legs that add to that estate and never alongside them.
            #
            # Run whenever the module declares a DataHub requirement, including when
            # the source legs above were skipped — the previous module's test bodies
            # may have deregistered registry rows (see the fixture docstring). This
            # branch is not reached by a module declaring PG/Kafka sources alone,
            # which touches no DataHub URN; no module currently has that shape.
            await datahub.sync_dataset_registry()

    asyncio.run(_provision())
    _PROVISIONED_BASELINE = requirement

    yield  # type: ignore[misc]


# ── DataHub actions pod guard ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
def datahub_actions_pod_required() -> None:
    """Skip (or fail) when the DataHub actions pod is absent.

    Use as a parameter on tests that rely on DataHub Managed Ingestion execution.
    Skip behaviour:
      - kubectl not on PATH or cluster unreachable → skip (environment not set up for this test)
      - kubectl reachable but no matching pod → skip (actions pod not deployed)
      - kubectl exits with non-zero status (real error) → raise, not skip

    spec: USE_CASE_en.md §UC1 — DataHub Managed Ingestion execution
    spec: TESTING.md §Spot vs Api-Wired Integration Tests — environment-missing guard
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pods",
                "-A",
                "-l",
                "app.kubernetes.io/name=acryl-datahub-actions",
                "--no-headers",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        pytest.skip(
            "kubectl not found; cannot verify DataHub actions pod. Skipping Managed Ingestion test."
        )
        return
    except subprocess.TimeoutExpired:
        pytest.skip(
            "kubectl timed out; cluster may be unreachable. Skipping Managed Ingestion test."
        )
        return

    if result.returncode != 0:
        raise RuntimeError(
            f"kubectl exited with code {result.returncode}: {result.stderr.strip()}. "
            "Fix the environment before running the Managed Ingestion test."
        )

    if not result.stdout.strip():
        pytest.skip(
            "No DataHub actions pod found "
            "(kubectl get pods -A -l app.kubernetes.io/name=acryl-datahub-actions "
            "returned no rows). Managed Ingestion executor is not running in this dev-env."
        )


# ── Airflow fixture ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module")
async def airflow_client():
    """Create an AirflowClient pointing at the dev-env Airflow instance; skip if unreachable.

    DAG registration is handled by Airflow loading DAG files from the dags/
    directory. This fixture only performs a health check on setup.
    Execution cleanup is each test module's responsibility (scoped to
    the specific DAG it uses).
    """
    from src.workflows.airflow.client import AirflowClient

    client = AirflowClient(
        base_url=_airflow_url,
        username=_airflow_user,
        password=_airflow_password,
    )
    try:
        await client.list_dags()
    except Exception:
        pytest.skip(f"Airflow not reachable at {_airflow_url}")

    yield client

    await client.close()


# ── pgvector fixture ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def vector_manager(async_engine):
    """Create a PgVectorManager bound to the dev-env PostgreSQL."""
    from src.shared.vector.client import PgVectorManager

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return PgVectorManager(session_factory=factory)


# ── Shared mock fixtures ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def mock_cache():
    """AsyncMock Redis cache with standard methods (get/set/publish/delete)."""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    cache.publish = AsyncMock()
    cache.delete = AsyncMock()
    return cache


# ── Shared test helpers ──────────────────────────────────────────────────────


@asynccontextmanager
async def override_app(
    *,
    datahub=None,
    db=None,
    redis=None,
    vector=None,
    airflow=None,
    notification=None,
):
    """Create an AsyncClient with FastAPI DI overrides for integration tests.

    Usage in a test fixture::

        @pytest_asyncio.fixture
        async def http_client(datahub_client, async_session):
            async with override_app(datahub=datahub_client, db=async_session) as client:
                yield client
    """
    from src.api.main import app

    if datahub is not None:
        from src.api.dependencies import get_datahub

        app.dependency_overrides[get_datahub] = lambda: datahub

    if redis is not None:
        from src.api.dependencies import get_redis

        app.dependency_overrides[get_redis] = lambda: redis

    if vector is not None:
        from src.api.dependencies import get_vector

        app.dependency_overrides[get_vector] = lambda: vector

    if db is not None:
        from src.api.dependencies import get_db

        async def _override_db():
            yield db

        app.dependency_overrides[get_db] = _override_db

    if airflow is not None:
        from src.api.dependencies import get_airflow_client

        app.dependency_overrides[get_airflow_client] = lambda: airflow

    if notification is not None:
        from src.api.dependencies import get_notification

        app.dependency_overrides[get_notification] = lambda: notification

    # This transport runs the app inside the test process, where `settings.redis_host`
    # points at a Redis the host does not run. The credential-accepting auth routes are
    # governed by a fail-closed limiter, so leaving it armed makes every request to
    # /auth/* answer 503 STORAGE_UNAVAILABLE before reaching the handler under test —
    # masking whatever the test actually asserts. The limits themselves are exercised
    # against the in-cluster API, not here.
    from src.api.middleware import rate_limit as _rate_limit

    with (
        patch.object(_rate_limit.auth_limiter, "enabled", False),
        patch.object(_rate_limit.limiter, "enabled", False),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client

    app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def runtime_conf(acquire_lock) -> dict:  # noqa: ARG001 — depends on lock
    """Session-scoped fixture: fetch GET /api/v1/admin/conf and return the conf dict.

    Asserts that the three infra stub fields are true (the required dev-env baseline):
      - stub_redis_client
      - stub_pgvector_manager
      - stub_notification_service

    stub_llm_client is intentionally NOT checked here — it is the per-test branch
    knob.  Stub-mode tests require it true; real-LLM tests require it false.
    The per-test ``pytest.skip`` guards — the first statement of each test body —
    that read ``runtime_conf["stub_llm_client"]`` are the correct gate.

    spec: src/workflows/_common.py — factory stub= contract; infra stubs must be on.
    spec: TESTING.md §Integration Testing — integration tests run with stubs for infra.
    """
    base_url = _shared_ingress_url()

    # Ensure the bootstrap admin user exists — a prior `--reset-all` may have wiped it.
    internal_token = os.environ.get("DATASPOKE_DEV_INTERNAL_TOKEN", "")
    if internal_token:
        try:
            httpx.post(
                f"{base_url}/internal/admin/bootstrap",
                headers={"X-Internal-Token": internal_token, "Content-Type": "application/json"},
                content="{}",
                timeout=30.0,
            )
        except Exception:
            pass  # Best-effort — login below will surface real failures.

    # Obtain admin token (bootstrap account: dataspoke@dataspoke.local / dataspoke)
    try:
        token_resp = httpx.post(
            f"{base_url}/api/v1/auth/token",
            json={"email": "dataspoke@dataspoke.local", "password": "dataspoke"},
            timeout=10.0,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
    except Exception as exc:
        pytest.fail(f"Cannot obtain admin token for runtime_conf preflight: {exc}")

    # Fetch the runtime conf
    try:
        conf_resp = httpx.get(
            f"{base_url}/api/v1/admin/conf",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        conf_resp.raise_for_status()
    except Exception as exc:
        pytest.fail(f"GET /admin/conf failed during runtime_conf preflight: {exc}")

    conf = conf_resp.json()

    # These three infra stubs must always be true — every integration test relies on them.
    # stub_llm_client is intentionally excluded: stub-mode tests (the default) need it true,
    # but real-LLM tests (test_uc3_ontology_generation[real], test_uc4_*_with_real_llm)
    # need it false.  The per-test pytest.skip guards — the first statement of each test
    # body — that read runtime_conf["stub_llm_client"] are the right gate.
    infra_stub_fields = ("stub_redis_client", "stub_pgvector_manager", "stub_notification_service")
    not_stubbed = [f for f in infra_stub_fields if not conf.get(f)]
    if not_stubbed:
        patch_url = f"{base_url}/api/v1/admin/conf"
        pytest.fail(
            f"Required infra stub fields {not_stubbed} are not true. "
            "Enable the three infra stubs before running integration tests:\n"
            f"  curl -X PATCH {patch_url} "
            "-H 'Authorization: Bearer <admin_token>' "
            "-H 'Content-Type: application/json' "
            "-d '{\"stub_redis_client\": true, "
            "\"stub_pgvector_manager\": true, \"stub_notification_service\": true}'"
        )

    return conf


def make_test_urn(service: str, suffix: str) -> str:
    """Build a test dataset URN: ``imazon.test.<service>.<suffix>``."""
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.test.{service}.{suffix},DEV)"


async def seed_events(
    session,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str | None = None,
    count: int = 3,
) -> list[str]:
    """Insert test events into dataspoke.events and return their IDs."""
    event_ids: list[str] = []
    for i in range(count):
        eid = str(uuid.uuid4())
        event_ids.append(eid)
        await session.execute(
            text(
                "INSERT INTO dataspoke.events"
                " (id, entity_type, entity_id, event_type, status, detail, occurred_at)"
                " VALUES (:id, :entity_type, :entity_id, :event_type,"
                " :status, :detail, :occurred_at)"
            ),
            {
                "id": eid,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "event_type": event_type or f"{entity_type.upper()}.COMPLETE",
                "status": "success",
                "detail": json.dumps({"run_id": str(uuid.uuid4()), "index": i}),
                "occurred_at": datetime.now(tz=UTC),
            },
        )
    await session.commit()
    return event_ids


async def cleanup_events(session, event_ids: list[str]) -> None:
    """Delete events by their IDs."""
    for eid in event_ids:
        await session.execute(
            text("DELETE FROM dataspoke.events WHERE id = :id"),
            {"id": eid},
        )
    await session.commit()


async def emit_test_dataset(
    client,
    *,
    urn: str,
    name: str,
    description: str = "Integration test dataset",
    fields: list[tuple[str, str, bool]] | None = None,
    with_ownership: bool = False,
    with_tags: bool = False,
    wait_seconds: float = 3.0,
) -> None:
    """Emit standard DataHub aspects for a test dataset.

    Args:
        fields: list of (fieldPath, nativeDataType, nullable) tuples.
            Defaults to [("id", "integer", False), ("name", "text", True)].
    """
    from datahub.metadata.schema_classes import (
        DatasetPropertiesClass,
        OperationClass,
        OperationTypeClass,
        OtherSchemaClass,
        SchemaFieldClass,
        SchemaMetadataClass,
        StatusClass,
    )

    if fields is None:
        fields = [("id", "integer", False), ("name", "text", True)]

    await client.emit_aspect(urn, StatusClass(removed=False))
    await client.emit_aspect(
        urn,
        DatasetPropertiesClass(
            name=name,
            description=description,
            customProperties={"source": "integration-test"},
        ),
    )

    _type_map = {"integer": "NUMBER", "bigint": "NUMBER", "real": "NUMBER"}
    schema_fields = [
        SchemaFieldClass(
            fieldPath=fp,
            nativeDataType=nt,
            type={"type": {"type": _type_map.get(nt, "STRING")}},
            nullable=nl,
        )
        for fp, nt, nl in fields
    ]

    await client.emit_aspect(
        urn,
        SchemaMetadataClass(
            schemaName=name,
            platform="urn:li:dataPlatform:postgres",
            version=0,
            hash="",
            platformSchema=OtherSchemaClass(rawSchema=""),
            fields=schema_fields,
        ),
    )

    import time

    now_ms = int(time.time() * 1000)
    await client.emit_aspect(
        urn,
        OperationClass(
            timestampMillis=now_ms,
            lastUpdatedTimestamp=now_ms,
            operationType=OperationTypeClass.INSERT,
        ),
    )

    if with_ownership:
        from datahub.metadata.schema_classes import (
            OwnerClass,
            OwnershipClass,
            OwnershipTypeClass,
        )

        await client.emit_aspect(
            urn,
            OwnershipClass(
                owners=[
                    OwnerClass(
                        owner="urn:li:corpuser:testuser@example.com",
                        type=OwnershipTypeClass.DATAOWNER,
                    ),
                ]
            ),
        )

    if with_tags:
        from datahub.metadata.schema_classes import GlobalTagsClass, TagAssociationClass

        await client.emit_aspect(
            urn,
            GlobalTagsClass(tags=[TagAssociationClass(tag="urn:li:tag:integration-test")]),
        )

    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


async def soft_delete_test_dataset(client, urn: str) -> None:
    """Soft-delete a test dataset in DataHub."""
    from datahub.metadata.schema_classes import StatusClass

    await client.emit_aspect(urn, StatusClass(removed=True))
