"""Unit tests for auth ROUTE handlers in src/api/routers/auth.py.

Backend auth logic (token issue/decode, user repo, reset, oauth resolution) is
covered by tests/unit/api/auth/test_{tokens,users,reset,oauth_google,...}.py and
schema validation by test_auth_spec.py. This file fills the remaining gap: the
HTTP-level wiring of the router handlers themselves — the refresh-token flow's
fail-closed ordering and error mapping, and GET /auth/me.

Routes under test:
  POST /auth/token/refresh   — refresh access token via HttpOnly refresh cookie
  GET  /auth/me              — return caller's profile

Spec traceability:
- spec/feature/AUTH.md §Refresh & revoke — POST /auth/token/refresh validates the
  refresh-cookie JWT, checks the Redis revocation list, and fails closed on Redis
  unreachability (503 STORAGE_UNAVAILABLE).
- spec/feature/AUTH.md §Deletion (~L205) — a refresh attempt carrying a deleted
  user's cookie fails 401; the cookie is revoked BEFORE the user lookup (fail-closed).
- spec/feature/AUTH.md (~L564) — the refresh endpoint accepts only type="refresh"
  JWTs; an access token presented there fails with 401.
- spec/feature/AUTH.md §Profile read & update — GET /auth/me returns the caller's
  users row (without password_hash), including users.role.
- spec/API.md §Authentication — authenticated routes require a valid JWT (else 401).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.main import app
from src.backend.auth.tokens import issue_refresh_token
from src.shared.exceptions import StorageUnavailableError
from tests.unit.api.conftest import _TEST_USER_ID, _make_mock_user, auth_headers

_REFRESH = "/api/v1/auth/token/refresh"
_ME = "/api/v1/auth/me"


# ── POST /auth/token/refresh ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_missing_cookie_returns_401(client) -> None:
    """POST /auth/token/refresh without the refresh cookie returns 401.

    spec: spec/feature/AUTH.md §Refresh & revoke — the flow validates the
    refresh-cookie JWT; absent cookie means no valid session → 401.
    """
    resp = await client.post(_REFRESH)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_revoked_cookie_returns_401(client) -> None:
    """POST /auth/token/refresh with a revoked refresh cookie returns 401.

    spec: spec/feature/AUTH.md §Refresh & revoke — the flow checks the Redis
    revocation list; a revoked token is rejected.
    """
    client.cookies.set("refresh_token", "revoked-token")
    with patch(
        "src.backend.auth.tokens.is_refresh_revoked",
        AsyncMock(return_value=True),
    ):
        resp = await client.post(_REFRESH)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_redis_unreachable_fails_closed_503(client) -> None:
    """POST /auth/token/refresh fails closed (503) when the revocation check hits Redis errors.

    spec: spec/feature/AUTH.md §Refresh & revoke — the refresh flow fails closed on
    Redis unreachability (503 STORAGE_UNAVAILABLE).
    """
    client.cookies.set("refresh_token", "some-token")
    with patch(
        "src.backend.auth.tokens.is_refresh_revoked",
        AsyncMock(side_effect=StorageUnavailableError("redis down")),
    ):
        resp = await client.post(_REFRESH)
    assert resp.status_code == 503, (
        f"Redis-unreachable refresh must fail closed with 503; got {resp.status_code}: "
        f"{resp.text}"
    )


@pytest.mark.asyncio
async def test_refresh_deleted_user_returns_401_and_revokes_before_lookup(client) -> None:
    """POST /auth/token/refresh for a deleted user returns 401; the cookie is revoked first.

    The old refresh token must be marked revoked BEFORE the user lookup (fail-closed
    rotation), so even a refresh that ultimately fails leaves the presented token
    unusable.

    spec: spec/feature/AUTH.md §Deletion — a refresh attempt carrying a deleted
    user's cookie fails 401; the cookie is revoked before the user lookup.
    """
    client.cookies.set("refresh_token", "deleted-user-token")
    mock_mark_revoked = AsyncMock()
    with (
        patch("src.backend.auth.tokens.is_refresh_revoked", AsyncMock(return_value=False)),
        patch(
            "src.backend.auth.tokens.decode_refresh_token",
            MagicMock(return_value={"sub": str(_TEST_USER_ID)}),
        ),
        patch("src.backend.auth.tokens.mark_refresh_revoked", mock_mark_revoked),
        # Deleted user → lookup returns None.
        patch("src.backend.auth.users.get_by_id", AsyncMock(return_value=None)),
    ):
        resp = await client.post(_REFRESH)

    assert resp.status_code == 401
    # Backstop proving the revoke-before-lookup path executed: the presented token
    # was revoked even though the flow then 401'd on the missing user.
    mock_mark_revoked.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_rejects_access_token_presented_as_cookie_401(client) -> None:
    """An access-type JWT presented at /auth/token/refresh is rejected with 401.

    The refresh decoder accepts only type="refresh" JWTs, preventing an access
    token from being replayed to mint a new session.

    spec: spec/feature/AUTH.md — the refresh endpoint accepts only type="refresh"
    JWTs; an access token presented there fails with 401.
    """
    # A real, correctly-signed ACCESS token (auth_headers mints one via make_token).
    access_token = auth_headers()["Authorization"].removeprefix("Bearer ")
    client.cookies.set("refresh_token", access_token)
    # Get past the revocation check so the ONLY possible 401 source is the decoder
    # rejecting the wrong token type (backstop isolating the type check).
    with patch(
        "src.backend.auth.tokens.is_refresh_revoked",
        AsyncMock(return_value=False),
    ):
        resp = await client.post(_REFRESH)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_happy_path_rotates_cookie_and_issues_access_token(client) -> None:
    """A valid refresh cookie yields a new access token, a rotated cookie, and old-token revoke.

    spec: spec/feature/AUTH.md §Refresh & revoke — refresh issues a new access token
    and rotates the refresh cookie; the old refresh token is revoked before minting.
    """
    # A real, correctly-signed REFRESH token whose sub is the resolvable test user.
    refresh_token = issue_refresh_token(_TEST_USER_ID)
    client.cookies.set("refresh_token", refresh_token)
    mock_mark_revoked = AsyncMock()
    resolved_user = _make_mock_user(role="Reader")
    with (
        patch("src.backend.auth.tokens.is_refresh_revoked", AsyncMock(return_value=False)),
        patch("src.backend.auth.tokens.mark_refresh_revoked", mock_mark_revoked),
        patch("src.backend.auth.users.get_by_id", AsyncMock(return_value=resolved_user)),
    ):
        resp = await client.post(_REFRESH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"], "response must carry a new access token"
    assert body["expires_in"] > 0
    # The old refresh token was revoked before the new one was minted (rotation).
    mock_mark_revoked.assert_awaited_once()
    # A fresh refresh cookie is set on the response (rotation).
    set_cookie = resp.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie, (
        f"a rotated refresh cookie must be set; got Set-Cookie={set_cookie!r}"
    )


# ── GET /auth/me ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_me_without_token_returns_401(client) -> None:
    """GET /auth/me without a JWT returns 401.

    spec: spec/API.md §Authentication — authenticated routes require a valid JWT.
    """
    resp = await client.get(_ME)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_me_returns_caller_profile(client) -> None:
    """GET /auth/me returns the caller's profile fields (id, email, name, role, has_google).

    spec: spec/feature/AUTH.md §Profile read & update — GET /auth/me returns the caller's users row
    (without password_hash), including users.role.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext
    from src.shared.db.models import User

    # _user_to_me asserts isinstance(user, User); spec=User makes the mock pass it.
    me_user = MagicMock(spec=User)
    me_user.id = _TEST_USER_ID
    me_user.email = "unit-test@example.com"
    me_user.name = "Unit Test User"
    me_user.role = "Editor"
    me_user.google_sub = None
    me_user.created_at = datetime.now(tz=UTC)
    me_user.updated_at = datetime.now(tz=UTC)
    ctx = AuthContext(user=me_user, effective_role="Editor")
    app.dependency_overrides[require_authenticated] = lambda: ctx
    try:
        resp = await client.get(_ME, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(require_authenticated, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "unit-test@example.com"
    assert body["name"] == "Unit Test User"
    assert body["role"] == "Editor"
    assert body["has_google"] is False, (
        "has_google must be False when the user has no linked google_sub"
    )
    # The profile must never expose a password hash.
    assert "password_hash" not in body
    assert "password" not in body
