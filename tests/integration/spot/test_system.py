"""Spot tests for system endpoints: /health and /ready.

Concerns covered:
- GET /health returns 200 (liveness check, no auth required)
- GET /ready returns 200 with datahub, postgres, redis subsystem checks per spec
"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_health_liveness(api_client: httpx.AsyncClient) -> None:
    """GET /health returns 200 — liveness check, no auth required.

    spec: API.md §System — GET /health is a liveness check with no auth required.
    Note: /health and /ready are mounted at root (no /api/v1 prefix).
    Note: the exact status string value ("ok") is impl-pinned — spec gap surfaced 2026-05-01.
    """
    resp = await api_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    # status string is impl-pinned; assert it exists but not its exact value
    assert "status" in body


@pytest.mark.asyncio
async def test_ready_returns_200_with_all_checks_true(api_client: httpx.AsyncClient) -> None:
    """GET /ready returns 200 with datahub, postgres, and redis subsystem checks.

    spec: API.md §System — GET /ready verifies DataHub, PostgreSQL, Redis connectivity.
    The response must include a 'checks' dict with exactly these three named subsystems.
    Note: the exact status string value ("ok") is impl-pinned — spec gap surfaced 2026-05-01.
    """
    resp = await api_client.get("/ready")

    assert resp.status_code == 200
    body = resp.json()
    # spec: API.md §System — /ready must verify DataHub, PostgreSQL, Redis
    assert "checks" in body, "/ready response must include 'checks' per spec/API.md §System"
    checks = body["checks"]
    assert "datahub" in checks, "/ready must report datahub subsystem per spec/API.md §System"
    assert "postgres" in checks, "/ready must report postgres subsystem per spec/API.md §System"
    assert "redis" in checks, "/ready must report redis subsystem per spec/API.md §System"
    assert checks.get("datahub") is True, f"datahub check failed: {checks}"
    assert checks.get("postgres") is True, f"postgres check failed: {checks}"
    assert checks.get("redis") is True, f"redis check failed: {checks}"
    # status string ("ok") is impl-pinned; spec gap surfaced 2026-05-01
    assert "status" in body
