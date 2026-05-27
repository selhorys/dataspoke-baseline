"""Spot integration test: refresh-token round-trip flow.

Concerns covered:
- POST /auth/token sets refresh_token HttpOnly cookie on the /api/v1/auth/token path.
- POST /auth/token/refresh with the refresh cookie returns 200 + new access_token.
- New access token is accepted by GET /auth/me.

This test pins the cookie path to /api/v1/auth/token (the Wave B2 fix). If a
future change breaks the refresh-cookie path, this test fails immediately.

Note: requires dev environment running.

spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke — POST /auth/token/refresh
      validates the refresh cookie and issues a fresh access token.
spec: spec/API.md §Auth POST /auth/token — sets refresh_token cookie; path = /api/v1/auth/token.
spec: spec/API.md §Auth POST /auth/token/refresh — requires HttpOnly refresh cookie.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _unique_email(prefix: str = "refresh") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


@pytest.mark.asyncio
async def test_refresh_token_round_trip(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """POST /auth/token → refresh cookie → POST /auth/token/refresh → new access_token → GET /auth/me.

    Flow:
    1. Seed a user directly in DB via create_user (no inline hashing).
    2. POST /auth/token with email + password → capture refresh_token cookie.
    3. POST /auth/token/refresh with the cookie → assert 200 + new access_token in response.
    4. Use the new access token on GET /auth/me → assert 200 (valid authenticated session).

    spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke — refresh token is a JWT
          cookie; POST /auth/token/refresh validates it and issues a fresh access token.
    spec: spec/API.md §Auth POST /auth/token/refresh — cookie path must be /api/v1/auth/token.
    """
    from src.backend.auth import users as user_service

    email = _unique_email()

    # Seed user via create_user to bypass /auth/register rate limit.
    # The password hash protocol stays inside the impl — test only holds the plaintext.
    user = await user_service.create_user(
        async_session, email, "Refresh Round Trip User", password="refreshpassword1"
    )
    await async_session.commit()
    user_id = user.id

    try:
        # Step 1: POST /auth/token — capture the refresh cookie
        login_resp = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "refreshpassword1"},
        )
        assert login_resp.status_code == 200, (
            f"POST /auth/token must return 200, got {login_resp.status_code}: {login_resp.text}"
        )
        assert "access_token" in login_resp.json(), "Login response must contain access_token"

        # The refresh cookie must be set on /api/v1/auth/token (cookie path check)
        assert "refresh_token" in login_resp.cookies, (
            "POST /auth/token must set the refresh_token cookie per spec/API.md §Auth "
            "— cookie path must include /api/v1/auth/token for the browser to send it back"
        )

        # Step 2: POST /auth/token/refresh — httpx carries the cookie automatically
        refresh_resp = await api_client.post("/api/v1/auth/token/refresh")
        assert refresh_resp.status_code == 200, (
            f"POST /auth/token/refresh must return 200, got {refresh_resp.status_code}: {refresh_resp.text}"
        )
        refresh_body = refresh_resp.json()
        assert "access_token" in refresh_body, (
            "POST /auth/token/refresh must return a new access_token "
            "per spec/feature/AUTH.md §Lifecycle §Refresh & revoke"
        )
        new_access_token = refresh_body["access_token"]

        # Step 3: Use new access token on GET /auth/me → 200
        me_resp = await api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        assert me_resp.status_code == 200, (
            f"GET /auth/me with refreshed access_token must return 200 "
            f"per spec/feature/AUTH.md §Lifecycle §Refresh & revoke, "
            f"got {me_resp.status_code}: {me_resp.text}"
        )
        me = me_resp.json()
        assert me["email"] == email, (
            "GET /auth/me must return the correct user after refresh"
        )

    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        await async_session.commit()
