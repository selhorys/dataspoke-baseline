"""Shared fixtures for integration tests against the dev-env PostgreSQL.

Port-forwards must be active before running:
- PostgreSQL on localhost:9201 (dataspoke-port-forward.sh)
- DataHub GMS on localhost:9004 (datahub-port-forward.sh)
- Redis on localhost:9202 (dataspoke-port-forward.sh)
- Lock service on localhost:9221 (lock-port-forward.sh)
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
