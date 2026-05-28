"""Shared fixtures for integration tests against the dev-env infrastructure.

Services are accessed via nginx-ingress (HTTP) or TCP passthrough ports.
All endpoint values are read from helm-charts/.env, which is populated by the
install scripts.  Tier B TCP defaults:
- PostgreSQL (dataspoke)  : <INGRESS_IP>:9201
- Redis                   : <INGRESS_IP>:9202
- DataHub Kafka           : <INGRESS_IP>:9005
- Example PostgreSQL      : <INGRESS_IP>:9102
- Example Kafka           : <INGRESS_IP>:9104
- Lock service            : <INGRESS_IP>:9221
"""

import asyncio
import base64
import json
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import requests
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import httpx

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def _load_dotenv() -> None:
    """Load helm-charts/.env into os.environ (without overwriting existing vars).

    Searches from the project root (two levels above this file) upward, which
    handles git worktrees where helm-charts/.env lives in the main worktree.
    """
    start = Path(__file__).resolve().parents[2]
    for candidate in (start, *start.parents):
        env_path = candidate / "helm-charts" / ".env"
        if env_path.is_file():
            break
    else:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _promote_test_runtime_overrides() -> None:
    """Promote DATASPOKE_TEST_* values into the runtime DATASPOKE_* names that
    src/ Pydantic Settings reads. Required when test code imports src/ helpers
    (e.g. src.backend.auth.tokens.issue_access_token) and must sign with the same
    secret the API pod uses — the chart-generated secret is mirrored into .env
    as DATASPOKE_TEST_JWT_SECRET_KEY by install.sh's _sync_env_from_secret.
    """
    if "DATASPOKE_TEST_JWT_SECRET_KEY" in os.environ:
        os.environ["DATASPOKE_JWT_SECRET_KEY"] = os.environ["DATASPOKE_TEST_JWT_SECRET_KEY"]


_promote_test_runtime_overrides()

# ── Ingress URL helper ────────────────────────────────────────────────────────


def _shared_ingress_url() -> str:
    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    return f"http://api.{domain}"


# ── Shared infrastructure env vars ────────────────────────────────────────────

_datahub_gms_url = os.environ["DATASPOKE_TEST_DATAHUB_GMS_URL"]
_datahub_frontend_url = os.environ.get("DATASPOKE_DEV_DATAHUB_FRONTEND_URL", "")
_datahub_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

_redis_host = os.environ["DATASPOKE_TEST_REDIS_HOST"]
_redis_port = int(os.environ["DATASPOKE_TEST_REDIS_PORT"])
_redis_password = os.environ.get("DATASPOKE_TEST_REDIS_PASSWORD", "")

_kafka_brokers = os.environ["DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS"]
_datahub_kafka_brokers = os.environ["DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS"]

_airflow_url = os.environ.get("DATASPOKE_TEST_AIRFLOW_URL", "http://localhost:8080")
_airflow_user = os.environ.get("DATASPOKE_TEST_AIRFLOW_USER", "")
_airflow_password = os.environ.get("DATASPOKE_TEST_AIRFLOW_PASSWORD", "")

_lock_owner = os.environ.get(
    "DATASPOKE_LOCK_OWNER",
    f"integration-test-{os.environ.get('USER', 'unknown')}",
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _get_datahub_session_token() -> str:
    """Get a DataHub session token via frontend login for dev-env testing."""
    resp = requests.post(
        f"{_datahub_frontend_url}/logIn",
        json={"username": "datahub", "password": "datahub"},
        timeout=5,
    )
    resp.raise_for_status()
    cookie = resp.headers.get("Set-Cookie", "")
    if "PLAY_SESSION=" not in cookie:
        return ""
    play_session = cookie.split("PLAY_SESSION=")[1].split(";")[0]
    payload = play_session.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    data = json.loads(base64.b64decode(payload))
    return data.get("data", {}).get("token", "")


def _auth_headers() -> dict[str, str]:
    """Create JWT auth headers for integration test requests.

    Signs a token using the backend token helper so the secret matches the
    one the in-cluster API pod uses (synced via DATASPOKE_TEST_JWT_SECRET_KEY).
    """
    import uuid

    from src.backend.auth.tokens import issue_access_token

    fake_user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    token, _ = issue_access_token(fake_user_id, "integration-test@example.com")
    return {"Authorization": f"Bearer {token}"}


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def integration_db_url() -> str:
    host = os.environ["DATASPOKE_TEST_POSTGRES_HOST"]
    port = os.environ["DATASPOKE_TEST_POSTGRES_PORT"]
    user = os.environ["DATASPOKE_TEST_POSTGRES_USER"]
    password = os.environ["DATASPOKE_TEST_POSTGRES_PASSWORD"]
    db = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


@pytest_asyncio.fixture(scope="session")
async def async_engine(integration_db_url: str) -> AsyncGenerator[AsyncEngine]:
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
    token = _datahub_token
    if not token:
        try:
            token = _get_datahub_session_token()
        except Exception:
            pytest.skip("Cannot obtain DataHub token (frontend unreachable)")
    return DataHubClient(gms_url=_datahub_gms_url, token=token)


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


@pytest.fixture(autouse=True)
def _flush_rate_limit_keys() -> None:
    """Drop slowapi `LIMITER/*` keys before each test so the per-IP 5/min limit
    on `/auth/register` and `/auth/token` does not bleed across tests in the
    same minute window."""
    import redis as _redis_sync

    client = _redis_sync.Redis(host=_redis_host, port=_redis_port, password=_redis_password or None)
    try:
        for key in client.scan_iter("LIMITS:LIMITER/*"):
            client.delete(key)
    except Exception:
        pass
    finally:
        client.close()


async def _bootstrap_schema(db_url: str) -> None:
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
def schema_bootstrap(integration_db_url: str) -> None:
    """Idempotent schema setup: schema, extensions, AGE graph, ORM tables, HNSW indexes."""
    asyncio.run(_bootstrap_schema(integration_db_url))
    yield  # type: ignore[misc]


@pytest.fixture(scope="session", autouse=True)
def acquire_lock() -> None:
    # When run from prauto phases.sh, the lock is already held externally.
    if os.environ.get("DATASPOKE_DEV_ENV_LOCK_PREACQUIRED"):
        yield  # type: ignore[misc]
        return

    _ingress_ip = os.environ["DATASPOKE_KUBE_INGRESS_IP"]
    lock_url = os.environ.get("DATASPOKE_LOCK_URL", f"http://{_ingress_ip}:9221")
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
        httpx.post(
            f"{lock_url}/lock/release",
            json={"owner": _lock_owner},
            timeout=5.0,
        )
    except httpx.ConnectError:
        pass


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
    """

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

    has_pg_kafka = bool(schemas or topics)

    def _reset_pg_kafka():
        if schemas:
            asyncio.run(postgres.reset_schemas(schemas))
        if topics:
            kafka.reset_topics(topics)

    def _ingest_datahub():
        from tests.integration.util import datahub

        if datahub_schemas:
            asyncio.run(datahub.ingest_pg_datasets(schemas=datahub_schemas))
        if datahub_topics:
            asyncio.run(datahub.ingest_kafka_datasets(topics=datahub_topics))

    if has_pg_kafka:
        _reset_pg_kafka()
    if datahub_schemas or datahub_topics:
        _ingest_datahub()

    yield  # type: ignore[misc]

    if has_pg_kafka:
        _reset_pg_kafka()


# ── DataHub actions pod guard ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
def datahub_actions_pod_required() -> None:
    """Skip (or fail) when the DataHub actions pod is absent.

    Use as a parameter on tests that rely on DataHub Managed Ingestion execution.
    Skip behaviour:
      - kubectl not on PATH or cluster unreachable → skip (environment not set up for this test)
      - kubectl reachable but no matching pod → skip (actions pod not deployed)
      - kubectl exits with non-zero status (real error) → raise, not skip

    spec: plan §test_uc1_passive_postgres_via_datahub_managed_ingestion
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
    The per-test ``pytest.skip`` decorators that read
    ``runtime_conf["stub_llm_client"]`` are the correct gate.

    spec: src/workflows/_common.py — factory stub= contract; infra stubs must be on.
    spec: TESTING.md §Integration Testing — integration tests run with stubs for infra.
    """
    base_url = _shared_ingress_url()

    # Ensure the bootstrap admin user exists — a prior `--reset-all` may have wiped it.
    internal_token = os.environ.get("DATASPOKE_TEST_INTERNAL_TOKEN", "")
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
    # but real-LLM tests (test_uc3/4_*_with_real_llm) need it false.  The per-test
    # pytest.skip decorators that read runtime_conf["stub_llm_client"] are the right gate.
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
