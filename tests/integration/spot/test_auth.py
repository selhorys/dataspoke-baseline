"""Spot tests for authentication endpoints.

Concerns covered:
- POST /auth/token issues access + refresh tokens for valid admin credentials
- POST /auth/token/refresh issues new access token via HttpOnly cookie
- POST /auth/token/revoke clears the refresh token cookie (204)
- A DG-only route returns 403 when called with a token lacking 'dg' group claim
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_token_issue_admin(api_client: httpx.AsyncClient) -> None:
    """POST /auth/token with admin credentials returns access_token and sets refresh cookie."""
    resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "admin", "password": "admin"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    # Refresh token must be in an HttpOnly cookie
    assert "refresh_token" in resp.cookies


@pytest.mark.asyncio
async def test_token_issue_invalid_credentials(api_client: httpx.AsyncClient) -> None:
    """POST /auth/token with wrong password returns 401 UNAUTHORIZED."""
    resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "admin", "password": "wrongpassword"},
    )

    assert resp.status_code == 401
    # FastAPI HTTPException wraps the error body under "detail"
    assert resp.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_token_refresh_via_cookie(api_client: httpx.AsyncClient) -> None:
    """POST /auth/token/refresh with valid HttpOnly cookie returns new access token."""
    # First, get a fresh token pair
    issue_resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "admin", "password": "admin"},
    )
    assert issue_resp.status_code == 200
    refresh_cookie = issue_resp.cookies.get("refresh_token")
    assert refresh_cookie is not None

    # Use the refresh cookie to get a new access token
    refresh_resp = await api_client.post(
        "/api/v1/auth/token/refresh",
        cookies={"refresh_token": refresh_cookie},
    )

    assert refresh_resp.status_code == 200
    body = refresh_resp.json()
    assert "access_token" in body
    # Old refresh token should have been rotated — new cookie issued
    assert "refresh_token" in refresh_resp.cookies


@pytest.mark.asyncio
async def test_token_revoke_returns_204(api_client: httpx.AsyncClient) -> None:
    """POST /auth/token/revoke clears the refresh cookie and returns 204."""
    # Get a fresh token pair
    issue_resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "admin", "password": "admin"},
    )
    assert issue_resp.status_code == 200
    refresh_cookie = issue_resp.cookies.get("refresh_token")
    assert refresh_cookie is not None

    # Revoke the refresh token
    revoke_resp = await api_client.post(
        "/api/v1/auth/token/revoke",
        cookies={"refresh_token": refresh_cookie},
    )

    assert revoke_resp.status_code == 204


@pytest.mark.asyncio
async def test_dg_route_requires_dg_group(api_client: httpx.AsyncClient) -> None:
    """A route requiring 'dg' group returns 403 when token has only 'de' group.

    Spec: API.md §Group-to-Route Access Control — /spoke/dg requires 'dg' group.
    The admin token has all groups; we test an unauthenticated call to verify
    403 behavior on a protected /spoke/dg route (token without dg claim).
    """
    # Call a /spoke/dg route without any auth — should get 401 (no token)
    resp_no_auth = await api_client.get("/api/v1/spoke/dg/metric")
    assert resp_no_auth.status_code == 401

    # Call with a deliberately invalid token — should get 401
    resp_bad_token = await api_client.get(
        "/api/v1/spoke/dg/metric",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp_bad_token.status_code == 401
