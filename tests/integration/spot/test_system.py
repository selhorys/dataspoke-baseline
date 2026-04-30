"""Spot tests for system endpoints: /health and /ready.

Concerns covered:
- GET /health returns 200 and status='ok'
- GET /ready returns 200 with datahub, postgres, redis all True
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_health_liveness(api_client: httpx.AsyncClient) -> None:
    """GET /health returns 200 with status='ok' — liveness check.

    Note: /health and /ready are mounted at root (no /api/v1 prefix).
    """
    resp = await api_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_returns_200_with_all_checks_true(api_client: httpx.AsyncClient) -> None:
    """GET /ready returns 200 with datahub, postgres, and redis all True."""
    resp = await api_client.get("/ready")

    assert resp.status_code == 200
    body = resp.json()
    checks = body["checks"]
    assert checks.get("datahub") is True, f"datahub check failed: {checks}"
    assert checks.get("postgres") is True, f"postgres check failed: {checks}"
    assert checks.get("redis") is True, f"redis check failed: {checks}"
    assert body["status"] == "ok"
