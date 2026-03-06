"""Shared fixtures for integration tests against the dev-env PostgreSQL.

Port-forwards must be active before running:
- PostgreSQL on localhost:9201 (dataspoke-port-forward.sh)
- Lock service on localhost:9221 (lock-port-forward.sh)
"""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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


@pytest.fixture(scope="session", autouse=True)
def acquire_lock() -> None:
    # When run from prauto phases.sh, the lock is already held externally.
    if os.environ.get("DATASPOKE_LOCK_PREACQUIRED"):
        yield  # type: ignore[misc]
        return

    lock_url = os.environ.get("DATASPOKE_LOCK_URL", "http://localhost:9221")
    try:
        resp = httpx.post(
            f"{lock_url}/lock/acquire",
            json={"owner": "prauto-01", "message": "integration test: alembic migrations"},
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
            json={"owner": "prauto-01"},
            timeout=5.0,
        )
    except httpx.ConnectError:
        pass
