"""Unit tests for /ready endpoint when DataHub peripheral is unconfigured.

Concern:
- GET /ready returns 200 with status="degraded" and checks["datahub"]=False
  when the DataHub peripheral is not configured (StorageUnavailableError raised
  by get_datahub).
- GET /ready never returns 503 — it always returns 200 with per-check flags.
- GET /ready returns 200 with checks["datahub"]=True when DataHub is configured
  and reachable.

This is a targeted complement to test_health.py: existing tests use
dependency_overrides to inject a working mock DataHub. These tests verify that
the unconfigured path (no dependency_override and get_datahub raises
StorageUnavailableError) does NOT propagate as 503 but is caught locally and
reported as checks["datahub"]=False.

spec traceability:
- plan/scalable-beaming-hamster.md §/ready degraded when peripheral unconfigured —
  /ready must return 200 with status="degraded"; never 503.
- spec/API.md §System — /ready reports DataHub, PostgreSQL, Redis per-check flags.
- src/api/routers/health.py ready() — StorageUnavailableError caught; checks["datahub"]=False.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_datahub, get_db, get_redis
from src.api.main import app


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _make_ready_client(
    datahub_override=None,
    postgres_ok: bool = True,
    redis_ok: bool = True,
) -> AsyncClient:
    """Build a test client with configurable dependency mocks."""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    if not postgres_ok:
        mock_db.execute.side_effect = ConnectionError("pg down")

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    if not redis_ok:
        mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_redis] = lambda: mock_redis

    if datahub_override is not None:
        app.dependency_overrides[get_datahub] = datahub_override

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_ready_returns_200_not_503_when_datahub_unconfigured() -> None:
    """GET /ready returns 200 even when DataHub peripheral is unconfigured.

    When get_datahub raises StorageUnavailableError (peripheral not configured),
    the /ready handler must catch it locally and report checks["datahub"]=False
    rather than letting the global exception handler convert it to 503.

    spec: plan/scalable-beaming-hamster.md §/ready degraded when peripheral unconfigured.
    spec: src/api/routers/health.py ready() — StorageUnavailableError caught locally.
    """
    from src.shared.exceptions import StorageUnavailableError

    async def _get_datahub_raises(db):
        raise StorageUnavailableError("DataHub peripheral not configured")

    # Patch get_datahub at the source so the health router's manual resolution
    # picks up the raising version (the health router calls get_datahub(db) directly
    # on the production path, not via dependency_overrides).
    with patch("src.api.routers.health.get_datahub", side_effect=_get_datahub_raises):
        async with _make_ready_client() as ac:
            response = await ac.get("/ready")

    assert response.status_code == 200, (
        f"GET /ready must return 200 even when DataHub is unconfigured; "
        f"got {response.status_code}. "
        "spec: plan/scalable-beaming-hamster.md §/ready degraded when peripheral unconfigured."
    )


@pytest.mark.asyncio
async def test_ready_checks_datahub_false_when_unconfigured() -> None:
    """GET /ready reports checks["datahub"]=False when DataHub peripheral is unconfigured.

    spec: plan/scalable-beaming-hamster.md §/ready degraded when peripheral unconfigured.
    spec: API.md §System — /ready must verify DataHub connectivity per subsystem.
    """
    from src.shared.exceptions import StorageUnavailableError

    async def _get_datahub_raises(db):
        raise StorageUnavailableError("DataHub peripheral not configured")

    with patch("src.api.routers.health.get_datahub", side_effect=_get_datahub_raises):
        async with _make_ready_client() as ac:
            response = await ac.get("/ready")

    body = response.json()
    assert "datahub" in body.get("checks", {}), (
        "/ready must include 'datahub' in checks dict. "
        "spec: API.md §System — /ready verifies DataHub, PostgreSQL, Redis."
    )
    assert body["checks"]["datahub"] is False, (
        f"checks['datahub'] must be False when peripheral unconfigured; "
        f"got {body['checks']['datahub']!r}. "
        "spec: plan/scalable-beaming-hamster.md §/ready degraded."
    )


@pytest.mark.asyncio
async def test_ready_status_degraded_when_datahub_unconfigured() -> None:
    """GET /ready reports status="degraded" when DataHub is unconfigured.

    status is "degraded" when any check fails.

    spec: src/api/routers/health.py ready() — all_ok=all(checks.values());
    "degraded" when not all_ok.
    """
    from src.shared.exceptions import StorageUnavailableError

    async def _get_datahub_raises(db):
        raise StorageUnavailableError("DataHub peripheral not configured")

    with patch("src.api.routers.health.get_datahub", side_effect=_get_datahub_raises):
        async with _make_ready_client() as ac:
            response = await ac.get("/ready")

    body = response.json()
    assert body.get("status") == "degraded", (
        f"status must be 'degraded' when datahub check fails; got {body.get('status')!r}."
    )


@pytest.mark.asyncio
async def test_ready_postgres_and_redis_still_checked_when_datahub_unconfigured() -> None:
    """GET /ready still checks PostgreSQL and Redis even when DataHub fails.

    When DataHub is unconfigured, the other checks must still run and report
    their own status — a DataHub failure must not short-circuit other checks.

    spec: API.md §System — /ready verifies DataHub, PostgreSQL, Redis.
    """
    from src.shared.exceptions import StorageUnavailableError

    async def _get_datahub_raises(db):
        raise StorageUnavailableError("DataHub peripheral not configured")

    with patch("src.api.routers.health.get_datahub", side_effect=_get_datahub_raises):
        async with _make_ready_client(postgres_ok=True, redis_ok=True) as ac:
            response = await ac.get("/ready")

    body = response.json()
    checks = body.get("checks", {})
    assert "postgres" in checks, "/ready must report postgres check."
    assert "redis" in checks, "/ready must report redis check."
    assert checks["postgres"] is True, "PostgreSQL check must pass when DB is healthy."
    assert checks["redis"] is True, "Redis check must pass when Redis is healthy."


@pytest.mark.asyncio
async def test_ready_status_ok_when_datahub_configured_and_reachable() -> None:
    """GET /ready returns status="ok" and checks["datahub"]=True when DataHub healthy.

    Baseline case: DataHub configured and reachable → all checks pass.

    spec: API.md §System — /ready returns "ok" when all dependencies reachable.
    """
    mock_datahub = MagicMock()
    mock_datahub.check_connectivity = AsyncMock(return_value=True)

    async with _make_ready_client(datahub_override=lambda: mock_datahub) as ac:
        response = await ac.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["datahub"] is True, (
        "checks['datahub'] must be True when DataHub is healthy and reachable."
    )
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_never_returns_503() -> None:
    """GET /ready returns 200 (not 503) regardless of which dependency fails.

    spec: src/api/routers/health.py ready() docstring —
    'Reports state; never returns 503.'
    """
    from src.shared.exceptions import StorageUnavailableError

    async def _get_datahub_raises(db):
        raise StorageUnavailableError("DataHub peripheral not configured")

    # Worst case: DataHub unconfigured, PostgreSQL and Redis down
    with patch("src.api.routers.health.get_datahub", side_effect=_get_datahub_raises):
        async with _make_ready_client(postgres_ok=False, redis_ok=False) as ac:
            response = await ac.get("/ready")

    assert response.status_code == 200, (
        f"GET /ready must NEVER return 503; "
        f"got {response.status_code}. "
        "spec: src/api/routers/health.py — reports state, never 503."
    )
