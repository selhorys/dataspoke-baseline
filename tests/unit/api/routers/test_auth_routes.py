"""Unit tests for auth ROUTE handlers in src/api/routers/auth.py.

Backend auth logic (token issue/decode, user repo, reset, oauth resolution) is
covered by tests/unit/api/auth/test_{tokens,users,reset,oauth_google,...}.py and
schema validation by test_auth_spec.py. This file fills the remaining gap: the
HTTP-level wiring of the router handlers themselves — the refresh-token flow's
fail-closed ordering and error mapping, and GET /auth/me.

Routes under test:
  POST /auth/token/refresh   — refresh access token via HttpOnly refresh cookie
  POST /auth/token/revoke    — revoke the refresh token (logout)
  GET  /auth/me              — return caller's profile

Spec traceability:
- spec/feature/AUTH.md §Refresh & revoke — POST /auth/token/refresh validates the
  refresh-cookie JWT, checks the Redis revocation list, and fails closed on Redis
  unreachability (503 STORAGE_UNAVAILABLE).
- spec/feature/AUTH.md §Refresh & revoke — POST /auth/token/revoke records the
  refresh token's hash in Redis; both refresh and revoke fail closed on Redis
  unreachability, and the 503 path retains the refresh cookie.
- spec/feature/AUTH.md §Refresh & revoke — revoke is credential-optional and
  idempotent: a missing/undecodable/wrong-signature/expired/non-type=refresh
  cookie is a no-op on the revocation store; the cookie is cleared and the call
  returns 204 (RFC 7009 §2.2).
- spec/API.md §Authorization — /auth/token/refresh and /auth/token/revoke take the
  HttpOnly refresh cookie rather than a bearer token.
- spec/feature/AUTH.md §Failure Modes (~L516) — "Redis unreachable during refresh
  or revoke" → 503 STORAGE_UNAVAILABLE.
- spec/feature/AUTH.md §Deletion (~L205) — a refresh attempt carrying a deleted
  user's cookie fails 401; the cookie is revoked BEFORE the user lookup (fail-closed).
- spec/feature/AUTH.md (~L564) — the refresh endpoint accepts only type="refresh"
  JWTs; an access token presented there fails with 401.
- spec/feature/AUTH.md §Profile read & update — GET /auth/me returns the caller's
  users row (without password_hash), including users.role.
- spec/API.md §Authentication — authenticated routes require a valid JWT (else 401).
"""

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import jwt
import pytest

from src.api.main import app
from src.backend.auth.tokens import issue_refresh_token
from src.shared.exceptions import StorageUnavailableError
from src.shared.settings import settings
from tests.unit.api.conftest import _TEST_USER_ID, _make_mock_user, auth_headers

_REFRESH = "/api/v1/auth/token/refresh"
_REVOKE = "/api/v1/auth/token/revoke"
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


# ── POST /auth/token/revoke ───────────────────────────────────────────────────


@contextmanager
def _redis_override(fake_redis):
    """Swap the client fixture's get_redis override for *fake_redis*, then restore.

    The conftest ``client`` fixture installs its own permissive AsyncMock; these
    tests need a handle on the Redis double to assert whether the revocation key
    was written. The previous override is restored (and the restore asserted) so
    the swap cannot leak into other tests.
    """
    from src.api.dependencies import get_redis

    previous = app.dependency_overrides[get_redis]
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        yield
    finally:
        app.dependency_overrides[get_redis] = previous
        assert app.dependency_overrides[get_redis] is previous


@pytest.mark.asyncio
async def test_revoke_redis_unreachable_fails_closed_503_storage_unavailable(client) -> None:
    """POST /auth/token/revoke fails closed with 503 STORAGE_UNAVAILABLE when Redis is down.

    This pins the router's error mapping in isolation: mark_refresh_revoked is
    replaced wholesale, so the decode path is NOT exercised here — see
    test_revoke_real_redis_error_propagates_503_end_to_end for the unmocked
    route↔tokens wiring.

    spec: spec/feature/AUTH.md §Refresh & revoke — both refresh and revoke fail
    closed on Redis unreachability (503 STORAGE_UNAVAILABLE).
    spec: spec/feature/AUTH.md §Failure Modes — "Redis unreachable during refresh
    or revoke" → 503 STORAGE_UNAVAILABLE.
    """
    # A realistic refresh cookie. The AsyncMock below ignores its arguments, so the
    # token's validity does not affect this test's outcome — it is here to keep the
    # request representative, not to reach a particular branch.
    client.cookies.set("refresh_token", issue_refresh_token(_TEST_USER_ID))
    with patch(
        "src.backend.auth.tokens.mark_refresh_revoked",
        AsyncMock(side_effect=StorageUnavailableError("redis down")),
    ):
        resp = await client.post(_REVOKE)

    assert resp.status_code == 503, (
        f"Redis-unreachable revoke must fail closed with 503; got {resp.status_code}: "
        f"{resp.text}"
    )
    assert resp.json()["error_code"] == "STORAGE_UNAVAILABLE", (
        "the 503 must carry error_code STORAGE_UNAVAILABLE per spec/feature/AUTH.md "
        f"§Failure Modes; got {resp.json()!r}"
    )


@pytest.mark.asyncio
async def test_revoke_redis_unreachable_retains_refresh_cookie(client) -> None:
    """The failed-revoke 503 response carries no cookie-clearing Set-Cookie header.

    Sensitivity is limited, and deliberately so. While the current handler raises,
    FastAPI discards mutations to the injected ``Response`` — so this assertion
    holds for free and would not catch a handler that called ``delete_cookie``
    before re-raising. It does guard a variant that *returns* a
    ``JSONResponse(503)`` with the cookie cleared, which is a live refactor risk.
    Treat it as a boundary guard, not as proof that the handler actively retains
    the cookie.

    spec: spec/feature/AUTH.md §Refresh & revoke — "On that 503 path revoke
    **retains the refresh cookie**: the token is still live server-side, and
    clearing the cookie would signal a revocation that did not occur — a fail-open
    dressed as an error."
    """
    client.cookies.set("refresh_token", issue_refresh_token(_TEST_USER_ID))
    with patch(
        "src.backend.auth.tokens.mark_refresh_revoked",
        AsyncMock(side_effect=StorageUnavailableError("redis down")),
    ):
        resp = await client.post(_REVOKE)

    # Backstop: without this the assertion below could pass on a route that never
    # reached the Redis write (e.g. a 400/422 short-circuit).
    assert resp.status_code == 503
    set_cookie = resp.headers.get("set-cookie")
    assert set_cookie is None, (
        "the 503 must not clear the refresh cookie — the token is still live "
        f"server-side; got Set-Cookie={set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_revoke_real_redis_error_propagates_503_end_to_end(client) -> None:
    """A genuine RedisError from set_nx surfaces as 503 STORAGE_UNAVAILABLE.

    The other 503 tests replace mark_refresh_revoked with a mock that raises, so
    they pin the router's error mapping but assume the route↔tokens wiring. This
    test mocks nothing but the Redis client itself: the real mark_refresh_revoked
    decodes the cookie, reaches set_nx, and the resulting RedisError must travel
    through StorageUnavailableError to the HTTP boundary.

    spec: spec/feature/AUTH.md §Refresh & revoke — both flows fail-closed on Redis
    unreachability (503 STORAGE_UNAVAILABLE).
    spec: spec/feature/AUTH.md §Failure Modes — "Redis unreachable during refresh
    or revoke" → 503 STORAGE_UNAVAILABLE.
    """
    import redis.exceptions

    # A real, correctly-signed REFRESH token is load-bearing here: mark_refresh_revoked
    # is NOT mocked, so anything else would return early at a no-op branch and never
    # reach set_nx — the test would pass as a 204 and prove nothing.
    client.cookies.set("refresh_token", issue_refresh_token(_TEST_USER_ID))
    failing_redis = AsyncMock()
    failing_redis.set_nx = AsyncMock(
        side_effect=redis.exceptions.RedisError("connection refused")
    )

    with _redis_override(failing_redis):
        resp = await client.post(_REVOKE)

    # Backstop: proves the decode path ran to completion and the write was attempted.
    # Without this, a no-op branch swallowing the token would leave a green 503-less test.
    failing_redis.set_nx.assert_awaited_once()
    assert resp.status_code == 503, (
        "a real RedisError during the revocation write must surface as 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "STORAGE_UNAVAILABLE", (
        f"the 503 must carry error_code STORAGE_UNAVAILABLE; got {resp.json()!r}"
    )
    # The cookie is retained on the 503 path (spec/feature/AUTH.md §Refresh & revoke).
    assert resp.headers.get("set-cookie") is None


@pytest.mark.asyncio
async def test_revoke_success_returns_204_and_clears_refresh_cookie(client) -> None:
    """A successful revoke returns 204 and clears the HttpOnly refresh cookie.

    spec: spec/feature/AUTH.md §Refresh & revoke — POST /auth/token/revoke records
    the refresh token's hash in Redis under revoked_refresh:{sha256[:16]}; the
    session cookie is cleared once the token is revoked.
    """
    token = issue_refresh_token(_TEST_USER_ID)
    client.cookies.set("refresh_token", token)
    mock_mark_revoked = AsyncMock()
    with patch("src.backend.auth.tokens.mark_refresh_revoked", mock_mark_revoked):
        resp = await client.post(_REVOKE)

    assert resp.status_code == 204, resp.text
    # Backstop proving the Redis-write path ran (not a missing-cookie no-op), and
    # that the token from the cookie — not some other value — was the one revoked.
    mock_mark_revoked.assert_awaited_once_with(ANY, token)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie, (
        f"the refresh cookie must be cleared on a successful revoke; got {set_cookie!r}"
    )
    assert "Max-Age=0" in set_cookie, (
        f"cookie clearing is expressed as Max-Age=0; got {set_cookie!r}"
    )
    assert "Path=/api/v1/auth/token" in set_cookie, (
        "the clearing cookie must carry the refresh cookie's path or the browser "
        f"keeps the original; got {set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_revoke_without_cookie_returns_204_and_writes_nothing(client) -> None:
    """POST /auth/token/revoke without a refresh cookie is a 204 no-op.

    spec: spec/feature/AUTH.md §Refresh & revoke — "Revoke is credential-optional
    and idempotent: a missing, undecodable, wrong-signature, expired, or
    non-`type=refresh` cookie is a no-op on the revocation store: the cookie is
    cleared and the call returns `204`."
    spec: spec/API.md §Authorization — /auth/token/revoke takes the HttpOnly
    refresh cookie rather than a bearer token.
    """
    fake_redis = AsyncMock()
    with _redis_override(fake_redis):
        resp = await client.post(_REVOKE)

    assert resp.status_code == 204, resp.text
    fake_redis.set_nx.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "cookie_value"),
    [
        ("undecodable garbage", "not-a-jwt"),
        # The exact value delete_cookie emits, so any client can self-trigger this.
        ("empty string", ""),
        (
            "wrong signature",
            jwt.encode(
                {
                    "sub": str(_TEST_USER_ID),
                    "type": "refresh",
                    "exp": int(time.time()) + 3600,
                },
                "an-attacker-chosen-key",
                algorithm="HS256",
            ),
        ),
        (
            "expired refresh token",
            jwt.encode(
                {
                    "sub": str(_TEST_USER_ID),
                    "type": "refresh",
                    "exp": int(time.time()) - 60,
                    "iat": int(time.time()) - 3660,
                },
                settings.jwt_secret_key,
                algorithm=settings.jwt_algorithm,
            ),
        ),
    ],
)
async def test_revoke_with_unrevocable_cookie_returns_204_and_writes_nothing(
    client, case: str, cookie_value: str
) -> None:
    """A cookie naming no live refresh token yields 204, no Redis write, cookie cleared.

    The cookie is untrusted input on a credential-optional route, so a garbage,
    forged, or expired value must not surface as a server error.

    spec: spec/feature/AUTH.md §Refresh & revoke — "Revoke is credential-optional
    and idempotent: a missing, undecodable, wrong-signature, expired, or
    non-`type=refresh` cookie is a no-op on the revocation store: the cookie is
    cleared and the call returns `204`. There is no live token to revoke, and per
    RFC 7009 §2.2 revocation reports success whether the token was revoked or was
    already invalid."
    """
    client.cookies.set("refresh_token", cookie_value)
    fake_redis = AsyncMock()
    with _redis_override(fake_redis):
        resp = await client.post(_REVOKE)

    assert resp.status_code == 204, (
        f"revoke with a {case} cookie must be a 204 no-op, not a server error; "
        f"got {resp.status_code}: {resp.text}"
    )
    # "a no-op on the revocation store" — nothing may be recorded for a token that
    # was never live.
    fake_redis.set_nx.assert_not_awaited()
    # "the cookie is cleared" — unlike the 503 path, the 204 path does clear it.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Max-Age=0" in set_cookie, (
        f"the {case} 204 path must still clear the useless cookie; got {set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_revoke_with_access_token_as_cookie_returns_204_and_writes_nothing(
    client,
) -> None:
    """An access token presented as the refresh cookie is a 204 no-op with no Redis write.

    Writing a revocation key for a non-refresh token would let a caller poison the
    revocation set with hashes of tokens that were never refresh tokens.

    spec: spec/feature/AUTH.md §Refresh & revoke — a "non-`type=refresh` cookie is
    a no-op on the revocation store: the cookie is cleared and the call returns
    `204`"; the revocation record is keyed on the *refresh* token's hash
    (revoked_refresh:{sha256[:16]}).
    """
    # A real, correctly-signed ACCESS token — decodes cleanly, but type != "refresh".
    # Load-bearing: mark_refresh_revoked is unmocked here, so only a genuinely
    # signed token reaches (and must be rejected by) the type check.
    access_token = auth_headers()["Authorization"].removeprefix("Bearer ")
    client.cookies.set("refresh_token", access_token)
    fake_redis = AsyncMock()
    with _redis_override(fake_redis):
        resp = await client.post(_REVOKE)

    assert resp.status_code == 204, resp.text
    fake_redis.set_nx.assert_not_awaited()
    assert "Max-Age=0" in resp.headers.get("set-cookie", "")


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
