"""Opaque personal access token (PAT) lifecycle.

Token format: ``dsk_<secrets.token_urlsafe(32)>``.
Storage: only ``sha256(raw).hexdigest()`` in ``api_tokens.token_hash``.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import ApiToken, User
from src.shared.db.session import SessionLocal
from src.shared.exceptions import AuthenticationError, ConflictError, EntityNotFoundError

_TOKEN_PREFIX = "dsk_"
_MAX_ACTIVE_PER_USER = 10
_LAST_USED_THROTTLE_SECONDS = 60

# Role rank — lower rank = fewer privileges.
_role_rank: dict[str, int] = {"Reader": 0, "Editor": 1, "Admin": 2}


def _intersect_role(snapshot: str, current: str) -> str:
    """Return the lower-privileged of the two role strings."""
    return snapshot if _role_rank.get(snapshot, 0) <= _role_rank.get(current, 0) else current


def _hash(raw: str) -> str:
    """Return the SHA-256 hex digest of *raw*."""
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Mint ──────────────────────────────────────────────────────────────────────


async def mint(
    db: AsyncSession,
    user_id: uuid.UUID,
    name: str,
    expires_at: datetime | None = None,
) -> tuple[str, ApiToken]:
    """Generate a new PAT for *user_id*.

    Returns ``(raw_token, ApiToken)``.  The raw token is returned once and
    never retrievable again.

    Raises:
        ConflictError('TOKEN_LIMIT_EXCEEDED')  — user already has 10 active tokens.
        EntityNotFoundError('user', user_id)   — user not found.
    """
    # Verify user exists and read current role.
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise EntityNotFoundError("user", str(user_id))

    current_role: str = user.role

    # Count active tokens.
    count_result = await db.execute(
        select(func.count())
        .select_from(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
    )
    active_count: int = count_result.scalar_one()
    if active_count >= _MAX_ACTIVE_PER_USER:
        raise ConflictError(
            "TOKEN_LIMIT_EXCEEDED",
            f"User already has {_MAX_ACTIVE_PER_USER} active tokens.",
        )

    raw_token = f"{_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    token_hash = _hash(raw_token)

    token = ApiToken(
        user_id=user_id,
        name=name,
        token_hash=token_hash,
        role_snapshot=current_role,
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    await db.refresh(token)
    return raw_token, token


# ── List ──────────────────────────────────────────────────────────────────────


async def list_active(db: AsyncSession, user_id: uuid.UUID) -> list[ApiToken]:
    """Return the user's non-revoked tokens."""
    result = await db.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at)
    )
    return list(result.scalars().all())


async def list_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[ApiToken]:
    """Return all tokens for *user_id* (includes revoked — for admin view)."""
    result = await db.execute(
        select(ApiToken).where(ApiToken.user_id == user_id).order_by(ApiToken.created_at)
    )
    return list(result.scalars().all())


def sort_tokens(tokens: list[ApiToken], sort: str | None) -> list[ApiToken]:
    """Order a materialised token list by the requested ``sort`` expression.

    Only ``created_at`` is sortable; the default (``sort`` omitted) is
    ``created_at`` descending — newest first — matching the standard list
    ordering. Used by the router-side pagination of the token list endpoints
    (``GET /auth/api-tokens``, ``GET /admin/users/{id}/api-tokens``), whose data
    source is a small per-user in-memory list.
    """
    if sort is None:
        return sorted(tokens, key=lambda t: t.created_at, reverse=True)
    for suffix, reverse in (("_desc", True), ("_asc", False)):
        if sort.endswith(suffix) and sort[: -len(suffix)] == "created_at":
            return sorted(tokens, key=lambda t: t.created_at, reverse=reverse)
    return tokens


# ── Revoke ────────────────────────────────────────────────────────────────────


async def revoke(
    db: AsyncSession,
    token_id: uuid.UUID,
    owner_user_id: uuid.UUID | None = None,
) -> None:
    """Set ``revoked_at = now()`` on *token_id*.

    When *owner_user_id* is provided, enforce ownership (raises 404 to avoid
    leaking existence).  No-op if already revoked.
    """
    where_clauses: list[Any] = [ApiToken.id == token_id]
    if owner_user_id is not None:
        where_clauses.append(ApiToken.user_id == owner_user_id)

    result = await db.execute(select(ApiToken).where(*where_clauses))
    token = result.scalar_one_or_none()
    if token is None:
        raise EntityNotFoundError("token", str(token_id))

    if token.revoked_at is None:
        token.revoked_at = datetime.now(tz=UTC)
        await db.flush()


# ── Lookup and validate ───────────────────────────────────────────────────────


async def lookup_and_validate(db: AsyncSession, raw_token: str) -> tuple[User, str]:
    """Authenticate *raw_token* and return ``(User, effective_role)``.

    Raises:
        AuthenticationError('INVALID_API_TOKEN')  — token not found.
        AuthenticationError('TOKEN_REVOKED')       — token has been revoked.
        AuthenticationError('TOKEN_EXPIRED')       — token has passed expires_at.
    """
    token_hash = _hash(raw_token)

    result = await db.execute(
        select(ApiToken, User)
        .join(User, ApiToken.user_id == User.id)
        .where(ApiToken.token_hash == token_hash)
    )
    row = result.first()
    if row is None:
        raise AuthenticationError(error_code="INVALID_API_TOKEN", message="Invalid API token.")

    token: ApiToken = row[0]
    user: User = row[1]

    if token.revoked_at is not None:
        raise AuthenticationError(error_code="TOKEN_REVOKED", message="API token has been revoked.")

    if token.expires_at is not None and token.expires_at <= datetime.now(tz=UTC):
        raise AuthenticationError(error_code="TOKEN_EXPIRED", message="API token has expired.")

    effective_role = _intersect_role(token.role_snapshot, user.role)

    # Throttled last_used_at update — dedicated session so the commit is
    # independent of the caller's session (which may be a read-only GET request
    # that never calls commit).  The WHERE clause makes this a no-op below 60s.
    async with SessionLocal() as throttle_session:
        await throttle_session.execute(
            update(ApiToken)
            .where(
                ApiToken.id == token.id,
                (ApiToken.last_used_at.is_(None))
                | (
                    ApiToken.last_used_at
                    < func.now()
                    # Safe: hardcoded constant. If this becomes config-driven,
                    # switch to bound parameters.
                    - text("INTERVAL '60 seconds'")
                ),
            )
            .values(last_used_at=func.now())
        )
        await throttle_session.commit()

    return user, effective_role
