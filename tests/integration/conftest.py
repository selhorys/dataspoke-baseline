"""Shared fixtures for integration tests against the dev-env infrastructure.

Port-forwards must be active before running:
- PostgreSQL on localhost:9201 (dataspoke-port-forward.sh)
- DataHub GMS on localhost:9004 (datahub-port-forward.sh)
- Redis on localhost:9202 (dataspoke-port-forward.sh)
- Qdrant on localhost:9203/9204 (dataspoke-port-forward.sh)
- Temporal on localhost:9205 (dataspoke-port-forward.sh)
- Lock service on localhost:9221 (lock-port-forward.sh)
- Dummy-data ports on localhost:9102/9104 (dummy-data-port-forward.sh)
"""

import base64
import json
import os
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import requests
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def _load_dotenv() -> None:
    """Load dev_env/.env into os.environ (without overwriting existing vars).

    Searches from the project root (two levels above this file) upward, which
    handles git worktrees where dev_env/.env lives in the main worktree.
    """
    start = Path(__file__).resolve().parents[2]
    for candidate in (start, *start.parents):
        env_path = candidate / "dev_env" / ".env"
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

# ── Shared infrastructure env vars ────────────────────────────────────────────

_datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "http://localhost:9004")
_datahub_frontend_url = os.environ.get("DATASPOKE_DATAHUB_FRONTEND_URL", "http://localhost:9002")
_datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

_redis_host = os.environ.get("DATASPOKE_REDIS_HOST", "localhost")
_redis_port = int(os.environ.get("DATASPOKE_REDIS_PORT", "9202"))
_redis_password = os.environ.get("DATASPOKE_REDIS_PASSWORD", "")

_kafka_brokers = os.environ.get("DATASPOKE_DATAHUB_KAFKA_BROKERS", "localhost:9005")

_qdrant_host = os.environ.get("DATASPOKE_QDRANT_HOST", "localhost")
_qdrant_http_port = int(os.environ.get("DATASPOKE_QDRANT_HTTP_PORT", "9203"))
_qdrant_grpc_port = int(os.environ.get("DATASPOKE_QDRANT_GRPC_PORT", "9204"))
_qdrant_api_key = os.environ.get("DATASPOKE_QDRANT_API_KEY", "")

_temporal_host = os.environ.get("DATASPOKE_TEMPORAL_HOST", "localhost")
_temporal_port = int(os.environ.get("DATASPOKE_TEMPORAL_PORT", "9205"))
_temporal_namespace = os.environ.get("DATASPOKE_TEMPORAL_NAMESPACE", "dataspoke")

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
    """Create JWT auth headers for integration test requests."""
    from src.api.auth.jwt import create_access_token

    token, _ = create_access_token(
        subject="integration-test-user",
        groups=["de", "da", "dg"],
        email="test@example.com",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Shared fixtures ───────────────────────────────────────────────────────────


def _alembic_cmd(*args: str) -> subprocess.CompletedProcess[str]:
    """Run an alembic command against the dev-env PostgreSQL."""
    host = os.environ.get("DATASPOKE_POSTGRES_HOST", "localhost")
    port = os.environ.get("DATASPOKE_POSTGRES_PORT", "9201")
    user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
    password = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke")
    db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")
    alembic_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    env = {**os.environ, "PYTHONPATH": _PROJECT_ROOT, "DATASPOKE_ALEMBIC_URL": alembic_url}
    return subprocess.run(
        ["alembic", *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_PROJECT_ROOT,
        env=env,
    )


@pytest.fixture(scope="session")
def integration_db_url() -> str:
    host = os.environ.get("DATASPOKE_POSTGRES_HOST", "localhost")
    port = os.environ.get("DATASPOKE_POSTGRES_PORT", "9201")
    user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
    password = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke")
    db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")
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
    return _kafka_brokers


@pytest_asyncio.fixture
async def redis_client():
    client = RedisClient(host=_redis_host, port=_redis_port, password=_redis_password)
    yield client
    await client.close()


@pytest.fixture(scope="session", autouse=True)
def alembic_at_head() -> None:
    """Ensure the dataspoke schema is at head for the entire test session."""
    result = _alembic_cmd("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade failed: {result.stderr}"
    yield  # type: ignore[misc]


@pytest.fixture(scope="session", autouse=True)
def acquire_lock() -> None:
    # When run from prauto phases.sh, the lock is already held externally.
    if os.environ.get("DATASPOKE_DEV_ENV_LOCK_PREACQUIRED"):
        yield  # type: ignore[misc]
        return

    lock_url = os.environ.get("DATASPOKE_LOCK_URL", "http://localhost:9221")
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
        pytest.skip("Lock service not reachable at localhost:9221")

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
    import asyncio

    from tests.integration.util import datahub, kafka, postgres

    asyncio.run(postgres.reset_all())
    kafka.reset_all()
    asyncio.run(datahub.reset_and_ingest())


@pytest.fixture(scope="session", autouse=True)
def dummy_data_reset(acquire_lock) -> None:  # noqa: ARG001 — depends on lock
    """Reset dummy data after tests only (Step 6).

    Per-module selective resets (module_dummy_data) handle Step 3 setup for
    modules that declare DUMMY_DATA_SCHEMAS / DUMMY_DATA_TOPICS /
    DUMMY_DATA_DATAHUB_SCHEMAS.  The full teardown reset ensures the next
    tester starts with a clean baseline.
    """
    yield  # type: ignore[misc]
    _reset_all_dummy_data()


@pytest.fixture(scope="module", autouse=True)
def module_dummy_data(request) -> None:
    """Autouse module-scoped fixture for selective dummy-data reset.

    Test modules declare dependencies via module-level constants:
        DUMMY_DATA_SCHEMAS: frozenset[str]         — PostgreSQL schemas to reset
        DUMMY_DATA_TOPICS: frozenset[str]           — Kafka topics to reset
        DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str]  — DataHub datasets to ingest

    DUMMY_DATA_DATAHUB_SCHEMAS implies the corresponding DUMMY_DATA_SCHEMAS
    (DataHub discovery requires the PG tables to exist).

    Modules that declare no constants are no-ops.
    """
    import asyncio

    from tests.integration.util import kafka, postgres

    schemas = getattr(request.module, "DUMMY_DATA_SCHEMAS", None)
    topics = getattr(request.module, "DUMMY_DATA_TOPICS", None)
    datahub_schemas = getattr(request.module, "DUMMY_DATA_DATAHUB_SCHEMAS", None)

    # DataHub ingest requires PG tables for schema discovery.
    if datahub_schemas:
        schemas = (schemas or frozenset()) | datahub_schemas

    has_pg_kafka = bool(schemas or topics)

    def _reset_pg_kafka():
        if schemas:
            asyncio.run(postgres.reset_schemas(schemas))
        if topics:
            kafka.reset_topics(topics)

    def _ingest_datahub():
        if datahub_schemas:
            from tests.integration.util import datahub

            asyncio.run(datahub.ingest_datasets(schemas=datahub_schemas))

    if has_pg_kafka:
        _reset_pg_kafka()
    if datahub_schemas:
        _ingest_datahub()

    yield  # type: ignore[misc]

    if has_pg_kafka:
        _reset_pg_kafka()


# ── Temporal fixture ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module")
async def temporal_client():
    """Connect to the dev-env Temporal server; skip if unreachable."""
    from temporalio.client import Client

    addr = f"{_temporal_host}:{_temporal_port}"
    try:
        client = await Client.connect(addr, namespace=_temporal_namespace)
    except Exception:
        pytest.skip(f"Temporal not reachable at {addr}")
    yield client


# ── Qdrant fixture ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def qdrant_manager():
    """Create a QdrantManager pointing at the dev-env Qdrant instance."""
    from src.shared.vector.client import QdrantManager

    return QdrantManager(
        host=_qdrant_host,
        port=_qdrant_http_port,
        api_key=_qdrant_api_key,
        grpc_port=_qdrant_grpc_port,
    )
