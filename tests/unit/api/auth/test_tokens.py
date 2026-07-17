"""Unit tests for src/backend/auth/tokens.py.

Concerns covered:
- issue_access_token payload shape: sub, email, exp, iat (no groups claim)
- decode_refresh_token rejects access tokens (type != "refresh") with
  AuthenticationError("INVALID_REFRESH_TOKEN")
- decode_access_token rejects refresh tokens (security: refresh token cannot be used for auth)
- is_refresh_revoked raises StorageUnavailableError when Redis is unreachable (fail-closed)
- mark_refresh_revoked writes the revocation key only for a live refresh token, and
  fails closed (StorageUnavailableError) when the Redis write errors

spec: spec/feature/AUTH.md §Lifecycle §Login
spec: spec/feature/AUTH.md §Refresh & revoke
spec: spec/API.md §JWT Claims
spec: spec/API.md §Authentication & Authorization §Token Strategy
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

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
    spec: spec/feature/AUTH.md §Security Considerations §Token-type confusion rejected.
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


# ── mark_refresh_revoked — revocation write ───────────────────────────────────


@pytest.mark.asyncio
async def test_mark_refresh_revoked_writes_key_for_live_refresh_token() -> None:
    """A live refresh token is recorded in Redis with TTL = its remaining lifetime.

    Regression guard for the fail-closed rotation performed by /auth/token/refresh:
    the no-op branches for unrevocable tokens must not stop a genuine refresh token
    from being revoked.

    spec: spec/feature/AUTH.md §Refresh & revoke — the refresh token's hash is
    recorded under revoked_refresh:{sha256[:16]} with TTL equal to the token's
    remaining lifetime.
    spec: spec/feature/AUTH.md §Login — the refresh JWT has a 7-day lifetime.
    """
    from src.backend.auth.tokens import issue_refresh_token, mark_refresh_revoked

    token = issue_refresh_token(uuid.uuid4())
    redis = AsyncMock()

    await mark_refresh_revoked(redis, token)

    redis.set_nx.assert_awaited_once()
    key, value, ttl = redis.set_nx.await_args.args
    assert key.startswith("revoked_refresh:"), (
        "the revocation key must use the revoked_refresh: prefix per "
        f"spec/feature/AUTH.md §Refresh & revoke; got {key!r}"
    )
    # sha256[:16] of the token — the key must not embed the token itself.
    assert len(key) == len("revoked_refresh:") + 16, (
        f"the key suffix must be sha256[:16] of the token; got {key!r}"
    )
    assert token not in key, "the revocation key must not leak the raw token"
    assert value == "1"
    # 7-day lifetime, minus the sub-second age of the token just issued.
    assert 7 * 24 * 3600 - 5 <= ttl <= 7 * 24 * 3600, (
        f"TTL must equal the refresh token's remaining lifetime (~604800s); got {ttl}"
    )


@pytest.mark.asyncio
async def test_mark_refresh_revoked_fails_closed_on_redis_error() -> None:
    """mark_refresh_revoked raises StorageUnavailableError when the Redis write errors.

    spec: spec/feature/AUTH.md §Refresh & revoke — both refresh and revoke fail
    closed on Redis unreachability.
    spec: spec/feature/AUTH.md §Failure Modes — "Redis unreachable during refresh
    or revoke" → 503 STORAGE_UNAVAILABLE.
    """
    import redis.exceptions

    from src.backend.auth.tokens import issue_refresh_token, mark_refresh_revoked
    from src.shared.exceptions import StorageUnavailableError

    failing_redis = AsyncMock()
    failing_redis.set_nx = AsyncMock(
        side_effect=redis.exceptions.RedisError("connection refused")
    )

    with pytest.raises(StorageUnavailableError):
        await mark_refresh_revoked(failing_redis, issue_refresh_token(uuid.uuid4()))


@pytest.mark.asyncio
async def test_mark_refresh_revoked_is_noop_for_undecodable_token() -> None:
    """An undecodable token names no live refresh token — no Redis write, no raise.

    mark_refresh_revoked is reached from the unauthenticated /auth/token/revoke
    route with an untrusted cookie value, so a garbage value must not propagate a
    decode error.

    spec: spec/feature/AUTH.md §Refresh & revoke — the revocation record is the
    refresh token's hash with TTL = its remaining lifetime; a value that is not a
    refresh token has neither.
    """
    from src.backend.auth.tokens import mark_refresh_revoked

    redis = AsyncMock()

    await mark_refresh_revoked(redis, "not-a-jwt")

    redis.set_nx.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_refresh_revoked_is_noop_for_empty_token() -> None:
    """The empty string — the value delete_cookie emits — is a no-op.

    A browser holding a cleared refresh cookie can send ``refresh_token=`` back,
    so the empty string is a reachable input, not a theoretical one.

    spec: spec/feature/AUTH.md §Refresh & revoke.
    """
    from src.backend.auth.tokens import mark_refresh_revoked

    redis = AsyncMock()

    await mark_refresh_revoked(redis, "")

    redis.set_nx.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_refresh_revoked_is_noop_for_wrong_signature_token() -> None:
    """A refresh-shaped token signed with the wrong key is a no-op.

    Honouring it would let an unauthenticated caller write arbitrary revocation
    keys, since the key is derived from the presented token's hash alone.

    spec: spec/feature/AUTH.md §Refresh & revoke — only a real refresh token
    (issued and signed by this service) has a revocation record to write.
    """
    import time

    import jwt

    from src.backend.auth.tokens import mark_refresh_revoked

    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "type": "refresh",
            "exp": int(time.time()) + 3600,
        },
        "an-attacker-chosen-key",
        algorithm="HS256",
    )
    redis = AsyncMock()

    await mark_refresh_revoked(redis, forged)

    redis.set_nx.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_refresh_revoked_is_noop_for_access_token() -> None:
    """A valid ACCESS token is a no-op — the revocation set holds refresh tokens only.

    spec: spec/feature/AUTH.md §Refresh & revoke — the record is keyed on the
    *refresh* token's hash (revoked_refresh:{sha256[:16]}).
    spec: spec/feature/AUTH.md §Security Considerations — token types are not
    interchangeable.
    """
    from src.backend.auth.tokens import issue_access_token, mark_refresh_revoked

    access_token, _ = issue_access_token(uuid.uuid4(), "access@example.com")
    redis = AsyncMock()

    await mark_refresh_revoked(redis, access_token)

    redis.set_nx.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_refresh_revoked_is_noop_for_expired_refresh_token() -> None:
    """An expired refresh token is a no-op — it has no remaining lifetime to cover.

    spec: spec/feature/AUTH.md §Refresh & revoke — the revocation key's TTL equals
    the token's remaining lifetime; an expired token is already unusable.
    """
    import time

    import jwt

    from src.backend.auth.tokens import mark_refresh_revoked
    from src.shared.settings import settings

    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "type": "refresh",
            "exp": int(time.time()) - 60,
            "iat": int(time.time()) - 3660,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    redis = AsyncMock()

    await mark_refresh_revoked(redis, expired)

    redis.set_nx.assert_not_awaited()
