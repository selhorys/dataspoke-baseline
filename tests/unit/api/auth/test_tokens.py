"""Unit tests for src/backend/auth/tokens.py.

Concerns covered:
- issue_access_token payload shape: sub, email, exp, iat (no groups claim)
- decode_refresh_token rejects access tokens (type != "refresh") with
  AuthenticationError("INVALID_REFRESH_TOKEN")
- decode_access_token rejects refresh tokens (security: refresh token cannot be used for auth)
- is_refresh_revoked raises StorageUnavailableError when Redis is unreachable (fail-closed)

spec: spec/feature/AUTH.md §Lifecycle §Login
spec: spec/API.md §JWT Claims
spec: spec/API.md §Authentication §Token Strategy
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── issue_access_token — payload shape ────────────────────────────────────────


def test_issue_access_token_payload_shape() -> None:
    """issue_access_token returns a token with sub, email, exp, iat and no groups claim.

    spec: spec/API.md §JWT Claims — access-token payload: sub (user uuid), email, exp, iat.
    The groups claim is absent; authorization is URI-prefix × HTTP method × users.role.
    """
    import jwt

    from src.backend.auth.tokens import issue_access_token
    from src.shared.settings import settings

    user_id = uuid.uuid4()
    email = "test@example.com"
    token, expires_in = issue_access_token(user_id, email)

    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    # spec: spec/API.md §JWT Claims — sub must be the user UUID as string
    assert "sub" in payload, "Access token must carry 'sub' claim per spec/API.md §JWT Claims"
    assert payload["sub"] == str(user_id), (
        "sub must equal str(user_id) per spec/API.md §JWT Claims"
    )

    # spec: spec/API.md §JWT Claims — email claim must match the input
    assert "email" in payload, "Access token must carry 'email' claim per spec/API.md §JWT Claims"
    assert payload["email"] == email

    # spec: spec/API.md §JWT Claims — groups claim must be absent
    assert "groups" not in payload, (
        "Access token must NOT carry a 'groups' claim — authorization is role-based "
        "per spec/API.md §JWT Claims"
    )

    # spec: spec/API.md §JWT Claims — exp and iat are present
    assert "exp" in payload, "Access token must carry 'exp' claim per spec/API.md §JWT Claims"
    assert "iat" in payload, "Access token must carry 'iat' claim per spec/API.md §JWT Claims"

    # spec: spec/API.md §Token Strategy — access token lifetime is 15 minutes
    assert expires_in == 15 * 60, (
        "expires_in must be 900 seconds (15 min) per spec/API.md §Token Strategy"
    )


def test_access_token_does_not_carry_role() -> None:
    """The access token JWT must NOT contain a role claim.

    spec: spec/API.md §JWT Claims — role is read per-request from users.role;
    the JWT does not carry role to enable instant demotion.
    spec: spec/feature/AUTH.md §Privilege Model — role changes take effect on the next request.
    """
    import jwt

    from src.backend.auth.tokens import issue_access_token
    from src.shared.settings import settings

    token, _ = issue_access_token(uuid.uuid4(), "norole@example.com")
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])

    assert "role" not in payload, (
        "Access token must NOT carry a 'role' claim — role is DB-backed per-request "
        "per spec/API.md §JWT Claims and spec/feature/AUTH.md §Privilege Model"
    )


# ── decode_refresh_token — rejects access tokens ─────────────────────────────


def test_decode_refresh_token_rejects_access_token() -> None:
    """decode_refresh_token raises AuthenticationError('INVALID_REFRESH_TOKEN') for an access token.

    spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke — refresh token has type='refresh';
    presenting an access token to the refresh endpoint must be rejected.
    """
    from src.backend.auth.tokens import decode_refresh_token, issue_access_token
    from src.shared.exceptions import AuthenticationError

    # Issue a real access token (no 'type' field or type != 'refresh')
    access_token, _ = issue_access_token(uuid.uuid4(), "swap@example.com")

    with pytest.raises(AuthenticationError) as exc_info:
        decode_refresh_token(access_token)

    assert exc_info.value.error_code == "INVALID_REFRESH_TOKEN", (
        "Passing an access token to decode_refresh_token must raise "
        "AuthenticationError('INVALID_REFRESH_TOKEN') "
        "per spec/feature/AUTH.md §Lifecycle §Refresh & revoke"
    )


# ── decode_access_token — rejects refresh tokens ──────────────────────────────


def test_decode_access_token_rejects_refresh_token() -> None:
    """decode_access_token raises AuthenticationError('UNAUTHORIZED') for a refresh token.

    Security fix from B2: refresh tokens must not be usable for authentication.
    spec: spec/feature/AUTH.md §Security Considerations — token-type confusion rejected.
    spec: spec/API.md §Authentication — access tokens are for auth; refresh for rotation only.
    """
    from src.backend.auth.tokens import decode_access_token, issue_refresh_token
    from src.shared.exceptions import AuthenticationError

    refresh_token = issue_refresh_token(uuid.uuid4())

    with pytest.raises(AuthenticationError) as exc_info:
        decode_access_token(refresh_token)

    assert exc_info.value.error_code == "UNAUTHORIZED", (
        "Refresh token presented to decode_access_token must raise "
        "AuthenticationError('UNAUTHORIZED') — security: token-type confusion prevention"
    )


# ── is_refresh_revoked — Redis fail-closed ────────────────────────────────────


@pytest.mark.asyncio
async def test_is_refresh_revoked_raises_storage_unavailable_on_redis_error() -> None:
    """is_refresh_revoked raises StorageUnavailableError when Redis raises RedisError.

    spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke — both flows fail-closed
    on Redis unreachability (503 STORAGE_UNAVAILABLE).
    spec: spec/feature/AUTH.md §Failure Modes — Redis unreachable → fail-closed.
    """
    import redis.exceptions

    from src.backend.auth.tokens import is_refresh_revoked
    from src.shared.exceptions import StorageUnavailableError

    failing_redis = AsyncMock()
    failing_redis.get = AsyncMock(
        side_effect=redis.exceptions.RedisError("connection refused")
    )

    # spec: spec/feature/AUTH.md §Failure Modes — Redis unreachable → fail-closed
    with pytest.raises(StorageUnavailableError):
        await is_refresh_revoked(failing_redis, "some_refresh_token")


@pytest.mark.asyncio
async def test_is_refresh_revoked_returns_true_for_revoked_token() -> None:
    """is_refresh_revoked returns True when Redis has the revocation key.

    spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke — Redis revocation list.
    """
    from src.backend.auth.tokens import is_refresh_revoked, issue_refresh_token

    token = issue_refresh_token(uuid.uuid4())

    # Compute what key would be stored
    import hashlib

    key_prefix = "revoked_refresh:"
    key = f"{key_prefix}{hashlib.sha256(token.encode()).hexdigest()[:16]}"

    present_redis = AsyncMock()
    present_redis.get = AsyncMock(return_value="1")

    result = await is_refresh_revoked(present_redis, token)
    assert result is True, (
        "is_refresh_revoked must return True when Redis has the revocation key "
        "per spec/feature/AUTH.md §Lifecycle §Refresh & revoke"
    )


@pytest.mark.asyncio
async def test_is_refresh_revoked_returns_false_for_non_revoked_token() -> None:
    """is_refresh_revoked returns False when Redis has no revocation key for the token.

    spec: spec/feature/AUTH.md §Lifecycle §Refresh & revoke
    """
    from src.backend.auth.tokens import is_refresh_revoked

    not_revoked_redis = AsyncMock()
    not_revoked_redis.get = AsyncMock(return_value=None)

    result = await is_refresh_revoked(not_revoked_redis, "a_valid_token")
    assert result is False
