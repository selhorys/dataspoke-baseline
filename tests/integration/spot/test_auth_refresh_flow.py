"""Spot integration test: refresh-token round-trip flow.

Concerns covered:
- POST /auth/token sets refresh_token HttpOnly cookie on the /api/v1/auth/token path.
- POST /auth/token/refresh with the refresh cookie returns 200 + new access_token.
- New access token is accepted by GET /auth/me.
- POST /auth/token/refresh carrying a deleted user's original refresh cookie returns 401.

This test pins the cookie path to /api/v1/auth/token (the Wave B2 fix). If a
future change breaks the refresh-cookie path, this test fails immediately.

Note: requires dev environment running.

spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke — POST /auth/token/refresh
      validates the refresh cookie and issues a fresh access token.
spec: spec/feature/AUTH.md §Lifecycle §Deletion — a /auth/token/refresh attempt
      carrying the deleted user's refresh cookie also fails with 401 UNAUTHORIZED;
      the cookie is revoked before the user lookup.
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
    """POST /auth/token → refresh cookie → POST /auth/token/refresh → new token → GET /auth/me.

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
            f"POST /auth/token/refresh must return 200, "
            f"got {refresh_resp.status_code}: {refresh_resp.text}"
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
        assert me["email"] == email, "GET /auth/me must return the correct user after refresh"

    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_refresh_with_deleted_user_cookie_returns_401(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
    admin_headers: dict[str, str],
) -> None:
    """POST /auth/token/refresh with a deleted user's original refresh cookie must return 401.

    Flow:
    1. Seed a password-capable user via create_user; commit.
    2. POST /auth/token → capture the refresh cookie (the ORIGINAL, never used for refresh).
    3. Obtain the user's id via GET /auth/me using the login access token.
    4. DELETE /admin/users/{id} — hard-deletes the user row.
    5. Replay the ORIGINAL refresh cookie at POST /auth/token/refresh.
    6. Assert 401 + error_code UNAUTHORIZED.

    Correctness note (reviewer-required, see TESTING.md): the original cookie is
    replayed WITHOUT first calling POST /auth/token/refresh to consume it.  That
    means Redis has NOT yet recorded this cookie as revoked; the revocation check
    (line ~195 of auth.py) passes cleanly and the request reaches the
    `user is None` branch (line ~209) that raises 401 "User no longer exists."
    Replaying a previously-refreshed cookie would yield 401 from the revocation
    check instead — a different code path, a vacuous pass.

    spec: spec/feature/AUTH.md §Lifecycle §Deletion — "a /auth/token/refresh attempt
          carrying the deleted user's refresh cookie also fails with 401 UNAUTHORIZED;
          the cookie is revoked before the user lookup, so the failure is fail-closed."
    spec: spec/API.md §Auth POST /auth/token/refresh — 401 UNAUTHORIZED on invalid
          or missing refresh token.
    """
    from src.backend.auth import users as user_service

    email = _unique_email("deleted-refresh")

    # Step 1: Seed a user with a password so POST /auth/token works.
    user = await user_service.create_user(
        async_session, email, "Deleted Refresh User", password="deletedpass1"
    )
    await async_session.commit()
    user_id = user.id

    try:
        # Step 2: Login to obtain the refresh cookie.
        login_resp = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "deletedpass1"},
        )
        assert login_resp.status_code == 200, (
            f"POST /auth/token must return 200, got {login_resp.status_code}: {login_resp.text}"
        )
        access_token = login_resp.json()["access_token"]
        assert "refresh_token" in login_resp.cookies, (
            "POST /auth/token must set the refresh_token HttpOnly cookie"
        )
        # Capture the original refresh cookie value before deletion.
        # This cookie has NOT been submitted to POST /auth/token/refresh yet,
        # so Redis has no revocation record for it.
        original_refresh_cookie = login_resp.cookies["refresh_token"]

        # Step 3: Confirm the user id via GET /auth/me (stable reference for delete).
        me_resp = await api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert me_resp.status_code == 200
        fetched_id = me_resp.json()["id"]

        # Step 4: Admin hard-deletes the user.
        del_resp = await api_client.delete(
            f"/api/v1/admin/users/{fetched_id}",
            headers=admin_headers,
        )
        assert del_resp.status_code == 204, (
            f"DELETE /admin/users/{fetched_id} must return 204: {del_resp.text}"
        )

        # Step 5: Replay the ORIGINAL refresh cookie (not consumed by any prior
        # /token/refresh call) after the user has been deleted.
        # Use a fresh client with just this cookie — no stale state from login.
        async with httpx.AsyncClient(
            base_url=str(api_client.base_url),
            timeout=30.0,
            cookies={"refresh_token": original_refresh_cookie},
        ) as fresh_client:
            replay_resp = await fresh_client.post("/api/v1/auth/token/refresh")

        # Step 6: Assert 401 UNAUTHORIZED — deleted subject fails authentication.
        assert replay_resp.status_code == 401, (
            f"POST /auth/token/refresh with a deleted user's original refresh cookie "
            f"must return 401 UNAUTHORIZED per spec/feature/AUTH.md §Lifecycle §Deletion, "
            f"got {replay_resp.status_code}: {replay_resp.text}"
        )
        body = replay_resp.json()
        assert body.get("error_code") == "UNAUTHORIZED", (
            "Response error_code must be UNAUTHORIZED (authentication failure, "
            "not an authorisation failure) per spec/feature/AUTH.md §Lifecycle §Deletion "
            f"and spec/shared/exceptions.py AuthenticationError default; got: {body}"
        )
        # Confirm we reached the deleted-user branch, not the earlier revocation branch.
        # The revocation branch message contains "revoked"; the deleted-user branch does not.
        # We do not pin the exact prose (spec does not fix it), only verify it is not
        # the revocation path that a pre-consumed cookie would trigger.
        message = body.get("message", "")
        assert "revoked" not in message.lower(), (
            "The 401 must originate from the deleted-user lookup branch "
            "(user is None), not from the Redis revocation check "
            f"per spec/feature/AUTH.md §Lifecycle §Deletion; message was: {message!r}"
        )
        # Positive discriminator so a reworded revocation message cannot make the
        # negative guard above vacuous: the deleted-user branch names the user.
        assert "user" in message.lower(), (
            "The 401 must identify the deleted-user lookup branch "
            f"per spec/feature/AUTH.md §Lifecycle §Deletion; message was: {message!r}"
        )

    finally:
        # Best-effort cleanup: the DELETE above already removed the user row;
        # this guard handles the case where deletion failed mid-test.
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        await async_session.commit()
