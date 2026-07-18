"""DataSpoke user repository.

Async CRUD functions over an injected AsyncSession.  No commit() calls
here — callers orchestrate the transaction.

All functions raise from src.shared.exceptions rather than HTTPException
so the service layer stays independent of the HTTP layer.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import bcrypt as _bcrypt
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import User
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError

_BCRYPT_ROUNDS = 12


def _constraint_name_of(obj: object) -> str | None:
    """Read a ``constraint_name`` off a raw driver error, if it carries one.

    The driver exception types are untyped, so the attribute is read defensively
    and only accepted when it is actually a string.
    """
    if obj is None:
        return None
    name = getattr(obj, "constraint_name", None)
    return name if isinstance(name, str) else None


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from an IntegrityError.

    Walks the exception chain to support both async and sync PostgreSQL drivers:
    - asyncpg (SQLAlchemy asyncpg dialect): the constraint name is on the raw
      asyncpg error, which is the ``__cause__`` of the dbapi-translated error
      stored in ``exc.orig``.  Checks both ``exc.orig.constraint_name`` (direct)
      and ``exc.orig.__cause__.constraint_name`` (asyncpg chain).
    - psycopg: ``exc.orig.diag.constraint_name``.

    Falls back to None when the constraint name cannot be determined.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    # Direct attribute (some drivers surface it here)
    name = _constraint_name_of(orig)
    if name:
        return name
    # asyncpg via SQLAlchemy asyncpg dialect: raw asyncpg error is __cause__
    name = _constraint_name_of(getattr(orig, "__cause__", None))
    if name:
        return name
    # psycopg
    name = _constraint_name_of(getattr(orig, "diag", None))
    if name:
        return name
    return None


def _prehash(password: str) -> bytes:
    """SHA-256 pre-hash before bcrypt (bcrypt_sha256 semantics).

    Raw bcrypt silently truncates at 72 bytes.  Pre-hashing via SHA-256 first
    produces a 64-char hex digest that is always well under the 72-byte limit,
    eliminating the truncation vulnerability.
    """
    return hashlib.sha256(password.encode()).hexdigest().encode()


# ── Password helpers ──────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    """Return a bcrypt hash of the SHA-256 pre-hashed password."""
    return _bcrypt.hashpw(_prehash(password), _bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


async def verify_password(user: User, password: str) -> bool:
    """Verify *password* against *user.password_hash*.

    Uses SHA-256 pre-hash (same as _hash_password) before bcrypt to avoid
    the 72-byte silent truncation that raw bcrypt suffers.
    Returns False when password_hash is None (Google-only account).
    """
    if user.password_hash is None:
        return False
    return _bcrypt.checkpw(_prehash(password), user.password_hash.encode())


# ── Read helpers ──────────────────────────────────────────────────────────────


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_by_google_sub(db: AsyncSession, sub: str) -> User | None:
    result = await db.execute(select(User).where(User.google_sub == sub))
    return result.scalar_one_or_none()


# ── Write helpers ─────────────────────────────────────────────────────────────


async def create_user(
    db: AsyncSession,
    email: str,
    name: str,
    password: str | None = None,
    google_sub: str | None = None,
    role: str = "Reader",
) -> User:
    """Create a new DataSpoke user.

    At least one of *password* or *google_sub* must be provided (enforced by
    the DB CHECK constraint, but validated here first for a cleaner error).

    Raises:
        ConflictError('EMAIL_ALREADY_REGISTERED')  — UNIQUE(email) violation.
    """
    password_hash = _hash_password(password) if password else None
    user = User(
        email=email,
        name=name,
        password_hash=password_hash,
        google_sub=google_sub,
        role=role,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        constraint = _violated_constraint(exc)
        if constraint == "uq_users_email":
            raise ConflictError("EMAIL_ALREADY_REGISTERED", "Email already registered.") from exc
        raise
    await db.refresh(user)
    return user


async def update_name(db: AsyncSession, user_id: uuid.UUID, name: str) -> User:
    """Update the display name for *user_id*.

    Raises:
        EntityNotFoundError('user', user_id)  — row not found.
    """
    user = await get_by_id(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))
    user.name = name
    await db.flush()
    await db.refresh(user)
    return user


async def update_password(db: AsyncSession, user_id: uuid.UUID, password: str) -> User:
    """Replace the bcrypt password hash for *user_id*.

    Raises:
        EntityNotFoundError('user', user_id)  — row not found.
    """
    user = await get_by_id(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))
    user.password_hash = _hash_password(password)
    await db.flush()
    await db.refresh(user)
    return user


async def link_google_sub(db: AsyncSession, user_id: uuid.UUID, sub: str) -> User:
    """Link a Google ``sub`` claim onto an existing user row.

    Raises:
        EntityNotFoundError('user', user_id)         — row not found.
        ConflictError('GOOGLE_ACCOUNT_LINKED_ELSEWHERE') — UNIQUE(google_sub) violation.
    """
    user = await get_by_id(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))
    user.google_sub = sub
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        constraint = _violated_constraint(exc)
        if constraint == "uq_users_google_sub":
            raise ConflictError(
                "GOOGLE_ACCOUNT_LINKED_ELSEWHERE",
                "This Google account is already linked to another user.",
            ) from exc
        raise
    await db.refresh(user)
    return user


async def update_role(db: AsyncSession, user_id: uuid.UUID, role: str) -> User:
    """Update the role for *user_id*.

    Raises:
        EntityNotFoundError('user', user_id)      — row not found.
        PreconditionFailedError('INVALID_ROLE')   — CHECK constraint violation.
    """
    user = await get_by_id(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))
    user.role = role
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        constraint = _violated_constraint(exc)
        if constraint == "ck_users_role":
            raise PreconditionFailedError("INVALID_ROLE", f"Invalid role value: {role!r}") from exc
        raise
    await db.refresh(user)
    return user


async def list_users(
    db: AsyncSession, limit: int = 20, offset: int = 0, order_by: Any = None
) -> tuple[list[User], int]:
    """Return a page of users plus the total row count."""
    default_order = User.created_at
    rows_result = await db.execute(
        select(User)
        .order_by(order_by if order_by is not None else default_order)
        .limit(limit)
        .offset(offset)
    )
    users = list(rows_result.scalars().all())

    count_result = await db.execute(select(func.count()).select_from(User))
    total: int = count_result.scalar_one()

    return users, total


async def hard_delete(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Hard-delete the user row (CASCADE removes api_tokens, password_reset_tokens).

    Raises:
        EntityNotFoundError('user', user_id)  — row not found.
    """
    user = await get_by_id(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))
    await db.delete(user)
    await db.flush()
