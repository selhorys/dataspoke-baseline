"""Unit tests for /health and /ready endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_returns_200(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_no_auth_required(client: AsyncClient) -> None:
    """Health endpoint must be accessible without any Authorization header."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ready_no_auth_required(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
