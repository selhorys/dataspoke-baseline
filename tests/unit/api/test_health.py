"""Unit tests for /health and /ready endpoints."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_datahub, get_db, get_redis
from src.api.main import app


def _override_ready_deps(
    datahub_ok: bool = True,
    postgres_ok: bool = True,
    redis_ok: bool = True,
) -> AsyncClient:
    """Create a test client with mocked infrastructure dependencies."""
    mock_datahub = MagicMock()
    mock_datahub.check_connectivity = AsyncMock(return_value=datahub_ok)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    if not postgres_ok:
        mock_db.execute.side_effect = ConnectionError("pg down")

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    if not redis_ok:
        mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

    app.dependency_overrides[get_datahub] = lambda: mock_datahub
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_no_auth_required(client: AsyncClient) -> None:
    """Health endpoint must be accessible without any Authorization header."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ready_all_ok() -> None:
    test_app = _override_ready_deps(datahub_ok=True, postgres_ok=True, redis_ok=True)
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as ac:
        response = await ac.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["datahub"] is True
    assert body["checks"]["postgres"] is True
    assert body["checks"]["redis"] is True


@pytest.mark.asyncio
async def test_ready_degraded_when_one_fails() -> None:
    test_app = _override_ready_deps(datahub_ok=True, postgres_ok=False, redis_ok=True)
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as ac:
        response = await ac.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] is False


@pytest.mark.asyncio
async def test_ready_includes_checks_dict() -> None:
    test_app = _override_ready_deps()
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as ac:
        response = await ac.get("/ready")
    body = response.json()
    assert "checks" in body
    assert set(body["checks"].keys()) == {"datahub", "postgres", "redis"}


@pytest.mark.asyncio
async def test_ready_no_auth_required() -> None:
    test_app = _override_ready_deps()
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as ac:
        response = await ac.get("/ready")
    assert response.status_code == 200
