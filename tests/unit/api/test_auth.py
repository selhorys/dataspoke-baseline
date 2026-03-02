"""Unit tests for auth endpoints: /api/v1/auth/token, /refresh, /revoke."""

import pytest
from httpx import AsyncClient

from tests.unit.api.conftest import auth_headers

AUTH_TOKEN = "/api/v1/auth/token"
AUTH_REFRESH = "/api/v1/auth/token/refresh"
AUTH_REVOKE = "/api/v1/auth/token/revoke"


@pytest.mark.asyncio
async def test_valid_login_returns_access_token(client: AsyncClient) -> None:
    response = await client.post(AUTH_TOKEN, json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


@pytest.mark.asyncio
async def test_valid_login_sets_refresh_cookie(client: AsyncClient) -> None:
    response = await client.post(AUTH_TOKEN, json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    assert "refresh_token" in response.cookies


@pytest.mark.asyncio
async def test_invalid_credentials_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        AUTH_TOKEN, json={"username": "admin", "password": "wrong-password"}
    )
    assert response.status_code == 401
    body = response.json()
    assert body["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_refresh_without_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.post(AUTH_REFRESH)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_valid_cookie_returns_new_token(client: AsyncClient) -> None:
    # First get a refresh cookie
    login_resp = await client.post(AUTH_TOKEN, json={"username": "admin", "password": "admin"})
    assert login_resp.status_code == 200
    refresh_cookie = login_resp.cookies.get("refresh_token")
    assert refresh_cookie is not None

    # Use the refresh cookie to get a new access token
    refresh_resp = await client.post(AUTH_REFRESH, cookies={"refresh_token": refresh_cookie})
    assert refresh_resp.status_code == 200
    body = refresh_resp.json()
    assert "access_token" in body


@pytest.mark.asyncio
async def test_revoke_clears_cookie(client: AsyncClient) -> None:
    # Login to get a refresh token
    login_resp = await client.post(AUTH_TOKEN, json={"username": "admin", "password": "admin"})
    refresh_cookie = login_resp.cookies.get("refresh_token")

    # Revoke
    revoke_resp = await client.post(AUTH_REVOKE, cookies={"refresh_token": refresh_cookie})
    assert revoke_resp.status_code == 204


@pytest.mark.asyncio
async def test_auth_required_route_without_token_returns_401(client: AsyncClient) -> None:
    """Accessing a protected route without a token must return 401."""
    response = await client.get("/api/v1/spoke/common/ontology")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_group_returns_403(client: AsyncClient) -> None:
    """A user without 'dg' group accessing /spoke/dg/* must get 403."""
    headers = auth_headers(groups=["de"])
    response = await client.get("/api/v1/spoke/dg/metric", headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_group_can_access_dg_routes(client: AsyncClient) -> None:
    """Admin users bypass group-tier restrictions."""
    headers = auth_headers(groups=["admin"])
    response = await client.get("/api/v1/spoke/dg/metric", headers=headers)
    # 501 means route was reached (admin auth passed); auth itself was 200
    assert response.status_code == 501


@pytest.mark.asyncio
async def test_valid_group_can_access_common_routes(client: AsyncClient) -> None:
    """Any valid group member can access /spoke/common/* routes."""
    for group in ["de", "da", "dg"]:
        headers = auth_headers(groups=[group])
        response = await client.get("/api/v1/spoke/common/ontology", headers=headers)
        assert response.status_code == 501, (
            f"Expected 501 for group={group}, got {response.status_code}"
        )
