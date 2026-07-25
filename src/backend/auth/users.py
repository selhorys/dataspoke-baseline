"""DataSpoke user repository.

Async CRUD functions over an injected AsyncSession.  No commit() calls
here — callers orchestrate the transaction.

All functions raise from src.shared.exceptions rather than HTTPException
so the service layer stays independent of the HTTP layer.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
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

    *email* is stored lowercased. ``users.email`` is CITEXT, so comparison is
    already case-insensitive, but storage is case-preserving and the DataHub
    corpuser URN derived from it (``urn:li:corpuser:<email>``) is case-
    sensitive. DataHub's OIDC JIT mints that URN from the Google email claim
    independently, so normalising on write keeps the two sides addressing the
    same corpuser.

    Raises:
        ConflictError('EMAIL_ALREADY_REGISTERED')  — UNIQUE(email) violation.
    """
    password_hash = _hash_password(password) if password else None
    user = User(
        email=email.lower(),
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


async def lock_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Take the ``users`` row lock for *user_id* and return the fresh row.

    Issues ``SELECT ... FOR UPDATE``, so callers holding this lock are
    serialized against each other for the life of their transaction. That
    ordering is what keeps a credential-creating write from committing
    alongside the credential reset run by :func:`bind_google_identity` — see
    spec/feature/AUTH.md §Serialization of credential-creating writes.

    ``populate_existing`` forces the returned instance to carry the state the
    lock just read: the session runs with ``expire_on_commit=False`` and has
    usually loaded this row already, so the identity map would otherwise return
    the pre-lock values the caller is trying to re-validate against.

    Returns None when the row does not exist.
    """
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class GoogleBindResult:
    """Outcome of :func:`bind_google_identity` — the row plus what it cleared.

    ``bound`` is False when the row already carried the incoming ``sub``: that
    call wrote nothing and cleared nothing, so the counts are zero and no event
    describes it.
    """

    user: User
    bound: bool
    api_tokens_revoked: int
    reset_tokens_deleted: int


async def bind_google_identity(db: AsyncSession, user_id: uuid.UUID, sub: str) -> GoogleBindResult:
    """Bind a provider-verified Google ``sub`` onto a user row, resetting its credentials.

    The Google identity is verified; the row it binds onto is not — its email
    was never verified — so the verified identity takes the row and every
    credential that existed on it beforehand is invalidated in the **same**
    transaction: ``password_hash`` is cleared, ``session_epoch`` is incremented
    (killing every outstanding JWT), the user's active API tokens are revoked,
    and their unused password-reset tokens are deleted. See
    spec/feature/AUTH.md §Credential reset on link.

    A row read under the lock that already carries **this** ``sub`` is a login,
    not a bind: two concurrent or retried callbacks both miss the ``sub``
    lookup, and the one that reaches the lock second finds the first's bind
    committed. It returns ``bound=False`` having written nothing. Re-running the
    reset there would bump the epoch a second time and kill the session the
    first callback just issued, leaving the user unable to finish signing in.

    The row lock taken here also serializes this reset against the
    credential-creating self-service writes, which take the same lock before
    re-validating their own authorisation.

    Clearing ``password_hash`` keeps ``ck_users_auth_method`` satisfied because
    ``google_sub`` is set in the same statement.

    The caller commits.

    Raises:
        EntityNotFoundError('user', user_id)                    — row not found.
        ConflictError('EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT')  — row already carries a
            different ``google_sub``; a bound row is never silently rebound.
        ConflictError('GOOGLE_ACCOUNT_LINKED_ELSEWHERE')        — UNIQUE(google_sub)
            violation; a competing bind claimed *sub* for another row first.
    """
    from src.backend.auth import api_tokens as _api_tokens
    from src.backend.auth import reset as _reset

    user = await lock_user(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))

    if user.google_sub is not None:
        if user.google_sub != sub:
            raise ConflictError(
                "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
                "This email is bound to a different Google account.",
            )
        return GoogleBindResult(
            user=user, bound=False, api_tokens_revoked=0, reset_tokens_deleted=0
        )

    user.google_sub = sub
    user.password_hash = None
    user.session_epoch = user.session_epoch + 1
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

    api_tokens_revoked = await _api_tokens.revoke_all_for_user(db, user_id)
    reset_tokens_deleted = await _reset.delete_unused_for_user(db, user_id)

    await db.refresh(user)
    return GoogleBindResult(
        user=user,
        bound=True,
        api_tokens_revoked=api_tokens_revoked,
        reset_tokens_deleted=reset_tokens_deleted,
    )


@dataclass(frozen=True)
class GoogleUnbindResult:
    """Outcome of :func:`unbind_google_identity` — the row and whether it released one.

    ``unbound`` is False when the row carried no binding: that call wrote
    nothing, so no event describes it.
    """

    user: User
    unbound: bool


async def unbind_google_identity(db: AsyncSession, user_id: uuid.UUID) -> GoogleUnbindResult:
    """Release the row's Google binding, ending the sessions established under it.

    The admin remedy for a binding that has gone stale — most often a re-issued
    Workspace address whose new Google account carries a new ``sub``, leaving the
    address's rightful holder refused ``EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT``
    on every callback. Clearing ``google_sub`` returns the row to the unbound
    state, so the next Google sign-in at that address binds afresh through the
    ordinary bind branch, credential reset included. See
    spec/feature/AUTH.md §Admin unbind.

    Unbinding is a credential change, so it increments ``session_epoch`` and
    takes the ``users`` row lock like the other credential-mutating paths. An
    already-unbound row releases nothing and is left exactly as it is, epoch
    included — a bump with no credential change behind it would sign the user
    out of every session to no end. It returns ``unbound=False``, and the caller
    records no event for it.

    API tokens are left alone. An unbind returns the row to its existing holder
    rather than handing it to someone new, so the tokens still belong to whoever
    minted them; if the row does later change hands, the bind's credential reset
    revokes them then.

    The caller commits.

    Raises:
        EntityNotFoundError('user', user_id)              — row not found.
        ConflictError('GOOGLE_IS_ONLY_AUTH_METHOD')       — the row has no
            ``password_hash``, so clearing ``google_sub`` would violate
            ``ck_users_auth_method`` and leave a row nobody can authenticate as.
    """
    user = await lock_user(db, user_id)
    if user is None:
        raise EntityNotFoundError("user", str(user_id))

    if user.google_sub is None:
        return GoogleUnbindResult(user=user, unbound=False)

    if user.password_hash is None:
        raise ConflictError(
            "GOOGLE_IS_ONLY_AUTH_METHOD",
            "Google is the only authentication method on this account. Have the address's "
            "holder set a password first — POST /auth/password/reset/request, then "
            "POST /auth/password/reset/confirm — and retry; the reset round-trip goes to "
            "that mailbox, so completing it proves they control the address. To remove the "
            "user instead, use DELETE /admin/users/{id}.",
        )

    user.google_sub = None
    user.session_epoch = user.session_epoch + 1
    await db.flush()
    await db.refresh(user)
    return GoogleUnbindResult(user=user, unbound=True)


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
