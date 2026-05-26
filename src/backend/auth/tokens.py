"""JWT issuance and Redis refresh-token revocation.

JWT helpers extracted from src/api/auth/jwt.py and the current
src/api/routers/auth.py.  This module belongs to the backend layer
so that it can be imported by workflows and services without pulling
in FastAPI.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import redis.exceptions as _redis_exceptions

from src.shared.exceptions import AuthenticationError, StorageUnavailableError
from src.shared.settings import settings

# Groups claim — constant for every authenticated user.
# This is an extensibility hook only; route-tier gating is done by privilege.py.
GROUPS_CLAIM: tuple[str, ...] = ("de", "da", "dg")

_REFRESH_REVOCATION_KEY_PREFIX = "revoked_refresh:"


# ── Internal helpers ──────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _revocation_key(token: str) -> str:
    """Return the Redis key used to record a revoked refresh token."""
    return f"{_REFRESH_REVOCATION_KEY_PREFIX}{hashlib.sha256(token.encode()).hexdigest()[:16]}"


# ── Token issuance ─────────────────────────────────────────────────────────────


def issue_access_token(user_id: uuid.UUID, email: str) -> tuple[str, int]:
    """Return ``(encoded_token, expires_in_seconds)``.

    Payload: sub=str(user_id), email, groups=list(GROUPS_CLAIM), exp, iat.
    """
    expire_seconds = settings.jwt_access_token_expire_minutes * 60
    now = _utc_now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "groups": list(GROUPS_CLAIM),
        "exp": now + timedelta(seconds=expire_seconds),
        "iat": now,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expire_seconds


def issue_refresh_token(user_id: uuid.UUID) -> str:
    """Return a signed refresh token for *user_id*."""
    now = _utc_now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": now + timedelta(days=settings.jwt_refresh_token_expire_days),
        "iat": now,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


# ── Token decode ──────────────────────────────────────────────────────────────


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access JWT.

    Raises:
        jwt.PyJWTError      — malformed or expired token.
        AuthenticationError — valid JWT but is a refresh token (rejected).

    Callers should differentiate jwt.ExpiredSignatureError if needed.
    """
    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") == "refresh":
        raise AuthenticationError(
            error_code="UNAUTHORIZED",
            message="Refresh token cannot be used for authentication.",
        )
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Decode and validate a refresh JWT.

    Verifies ``type == 'refresh'`` in addition to standard JWT checks.

    Raises:
        AuthenticationError('UNAUTHORIZED')           — expired token.
        AuthenticationError('UNAUTHORIZED')           — malformed token.
        AuthenticationError('INVALID_REFRESH_TOKEN')  — valid JWT but not a refresh token.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError(
            error_code="UNAUTHORIZED",
            message="Refresh token has expired.",
        ) from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError(
            error_code="UNAUTHORIZED",
            message="Invalid refresh token.",
        ) from exc
    if payload.get("type") != "refresh":
        raise AuthenticationError(
            error_code="INVALID_REFRESH_TOKEN",
            message="Not a refresh token.",
        )
    return payload


# ── Revocation ────────────────────────────────────────────────────────────────


async def is_refresh_revoked(redis: Any, token: str) -> bool:
    """Return True if *token* is in the Redis revocation set.

    Raises:
        StorageUnavailableError  — fail-closed on any RedisError.
    """
    try:
        return bool(await redis.get(_revocation_key(token)))
    except _redis_exceptions.RedisError as exc:
        raise StorageUnavailableError(
            "Token revocation store unavailable; refresh denied."
        ) from exc


async def mark_refresh_revoked(redis: Any, token: str) -> None:
    """Record *token* as revoked in Redis with TTL = remaining lifetime.

    Raises:
        StorageUnavailableError  — fail-closed on any RedisError.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
        ttl = max(0, int(payload.get("exp", 0)) - int(time.time()))
        if ttl > 0:
            await redis.set_nx(_revocation_key(token), "1", ttl)
    except _redis_exceptions.RedisError as exc:
        raise StorageUnavailableError(
            "Token revocation store unavailable; revoke denied."
        ) from exc
