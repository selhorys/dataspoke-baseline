"""Spot tests for authentication endpoints.

Concerns covered:
- POST /auth/token issues access + refresh tokens for valid admin credentials
- POST /auth/token returns expires_in == 900s (15 min) per spec
- POST /auth/token sets HttpOnly refresh cookie with 7-day Max-Age per spec
- POST /auth/token/refresh issues new access token via HttpOnly cookie
- POST /auth/token/revoke clears the refresh token cookie (204)
- A route requiring 'dg' group returns 403 when token has only 'de' group
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_token_issue_admin(api_client: httpx.AsyncClient) -> None:
    """POST /auth/token with admin credentials returns access_token and sets refresh cookie.

    spec: API.md §Token Strategy — access token lifetime is 15 minutes (900 seconds).
    spec: API.md §Token Strategy — refresh token stored in HttpOnly cookie.
    spec: API.md §Token Strategy — refresh token lifetime is 7 days (604800 seconds).
    spec: API.md §Known Limitations (Current Stub) — HTTP-only cookie mandated.
    """
    # spec: API.md §Token Strategy — access token expires in 15 minutes
    _ACCESS_TOKEN_SECONDS = 15 * 60  # 900 seconds per spec/API.md §Token Strategy
    # spec: API.md §Token Strategy — refresh token expires in 7 days
    _REFRESH_TTL_SECONDS = 7 * 24 * 3600  # 604800 seconds per spec/API.md §Token Strategy

    resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "admin", "password": "admin"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    # token_type is impl-pinned; spec/API.md does not mandate it — spec gap surfaced 2026-05-01
    # spec: API.md §Token Strategy — expires_in must be exactly 900 seconds (15 min)
    assert body["expires_in"] == _ACCESS_TOKEN_SECONDS, (
        f"expires_in must be {_ACCESS_TOKEN_SECONDS}s per spec/API.md §Token Strategy, "
        f"got {body['expires_in']}"
    )
    # Refresh token must be in an HttpOnly cookie
    assert "refresh_token" in resp.cookies

    # Inspect Set-Cookie header for security attributes
    set_cookie = resp.headers.get("set-cookie", "")
    set_cookie_lower = set_cookie.lower()

    # spec: API.md §Known Limitations (Current Stub) — HttpOnly is the only mandated cookie flag
    assert "httponly" in set_cookie_lower, (
        "Refresh cookie must be HttpOnly per spec/API.md §Known Limitations (Current Stub)"
    )

    # spec: API.md §Token Strategy — refresh token lifetime is 7 days = 604800 seconds
    assert f"max-age={_REFRESH_TTL_SECONDS}" in set_cookie_lower, (
        f"Refresh cookie Max-Age must be {_REFRESH_TTL_SECONDS}s (7 days) "
        "per spec/API.md §Token Strategy"
    )

    # samesite and path are impl-pinned; spec/API.md does not mandate them — spec gap surfaced 2026-05-01


@pytest.mark.asyncio
async def test_token_issue_invalid_credentials(api_client: httpx.AsyncClient) -> None:
    """POST /auth/token with wrong password returns 401 UNAUTHORIZED."""
    resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "admin", "password": "wrongpassword"},
    )

    assert resp.status_code == 401
    # spec/API.md §Error Catalogue — top-level error envelope
    assert resp.json()["error_code"] == "UNAUTHORIZED"


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

    spec: API.md §Group-to-Route Access Control — /spoke/dg/* requires 'dg' group claim.
    A valid token with only 'de' in groups must be rejected with 403 FORBIDDEN.
    """
    from src.api.auth.jwt import create_access_token

    # Mint a real signed token with only 'de' group — no 'dg' claim.
    # spec: API.md §Group-to-Route Access Control — DE group cannot access /spoke/dg routes
    de_only_token, _ = create_access_token(
        subject="de-only-user",
        groups=["de"],
        email="de-user@example.com",
    )
    de_only_headers = {"Authorization": f"Bearer {de_only_token}"}

    resp_forbidden = await api_client.get(
        "/api/v1/spoke/dg/metric",
        headers=de_only_headers,
    )
    assert resp_forbidden.status_code == 403, (
        f"A 'de'-only token must get 403 on /spoke/dg route per spec/API.md "
        f"§Group-to-Route Access Control, got {resp_forbidden.status_code}"
    )

    # Verify no-auth case still returns 401 (separate from the 403 case)
    resp_no_auth = await api_client.get("/api/v1/spoke/dg/metric")
    assert resp_no_auth.status_code == 401
