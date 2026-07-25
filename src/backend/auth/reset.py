"""Password-reset token issuance and confirmation.

Issue creates a single-use row in ``password_reset_tokens`` (15-min TTL)
and sends an email via NotificationService.  Confirm validates the token
and rewrites the user's bcrypt hash.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.auth import users as _users
from src.shared.db.models import PasswordResetToken
from src.shared.exceptions import (
    BadRequestError,
    NotificationError,
    PeripheralNotConfiguredError,
    StorageUnavailableError,
)

logger = logging.getLogger(__name__)

_TOKEN_TTL_MINUTES = 15


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue_reset_token(
    db: AsyncSession,
    notification_service: object,
    email: str,
) -> None:
    """Issue a password-reset token and send it by email.

    Operation order (invariant): the email is sent BEFORE the token row is
    written to the DB.  This guarantees that any failure in the SMTP layer
    leaves no orphan row — the spec requires "no DB write" when the peripheral
    is unavailable.

    If the email is unknown, returns silently (no enumeration leak).

    This is a credential-creating write: the token it mints is a live
    15-minute password-write capability, and unlike a JWT no epoch governs it.
    So the row is inserted under the ``users`` row lock, and the owner's
    ``session_epoch`` — captured on the read that resolved the address — is
    re-compared under that lock. If it has moved, a Google bind has taken the
    row in the meantime and the token row is not written. The lock alone would
    not contain this: the bind holds it, releases it on commit, and an insert
    that then proceeded would land *after* the bind's delete of unused rows,
    which sweeps only what is visible at that statement. The epoch comparison
    is what closes it.

    The lock is taken after ``send_email`` returns, never around it — holding a
    row lock across an SMTP round trip would stall every concurrent bind for
    the duration of a network call.

    A declined write still returns normally: the route reports the same outcome
    for known and unknown addresses and must not become an oracle for account
    state.

    Raises:
        PeripheralNotConfiguredError('smtp')  — SMTP not configured; propagates
            to the global handler which returns 503 PERIPHERAL_NOT_CONFIGURED.
        StorageUnavailableError               — email delivery failed for any
            other reason (wraps NotificationError so the global handler returns
            503 STORAGE_UNAVAILABLE rather than falling through to 500).
    """
    user = await _users.get_by_email(db, email)
    if user is None:
        # No-op — same response shape as a known address (no enumeration leak).
        return

    # The epoch this request is authorised against, re-compared under the lock.
    # It must be snapshotted into a local int: ``lock_user`` refreshes and
    # returns this very instance, so ``locked.session_epoch != user.session_epoch``
    # compares an attribute against itself and is False no matter what the lock
    # read — the guard below only has force because this value is detached from
    # the ORM object.
    epoch_at_read = user.session_epoch
    # The address of record, not the one the caller typed: the row was matched
    # case-insensitively through CITEXT, and a bearer credential is delivered
    # only to the authoritative address.
    recipient = user.email

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash(raw_token)
    expires_at = datetime.now(tz=UTC) + timedelta(minutes=_TOKEN_TTL_MINUTES)

    subject = "DataSpoke password reset"
    body_html = (
        "<h2>DataSpoke Password Reset</h2>"
        "<p>Use the token below to reset your password. "
        f"It expires in {_TOKEN_TTL_MINUTES} minutes.</p>"
        f"<p><strong>{raw_token}</strong></p>"
        "<p>If you did not request a password reset, you can safely ignore this email.</p>"
    )

    # Send the email first — if this raises, no DB write occurs.
    try:
        await notification_service.send_email(  # type: ignore[attr-defined]
            to=[recipient], subject=subject, body_html=body_html
        )
    except PeripheralNotConfiguredError:
        # SMTP peripheral not configured — let the global handler return 503.
        logger.warning(
            "password_reset_smtp_not_configured",
            extra={"user_id": str(user.id)},
        )
        raise
    except NotificationError as exc:
        # Delivery failure — wrap as StorageUnavailableError so the global
        # handler returns 503 STORAGE_UNAVAILABLE instead of falling through
        # to the generic 500 handler.
        logger.error(
            "password_reset_email_failed",
            extra={"user_id": str(user.id)},
            exc_info=True,
        )
        raise StorageUnavailableError("Password-reset email delivery failed.") from exc

    # Email delivered — re-validate under the row lock, then write the token row.
    locked = await _users.lock_user(db, user.id)
    if locked is None or locked.session_epoch != epoch_at_read:
        logger.info(
            "password_reset_token_declined",
            extra={
                "user_id": str(user.id),
                "reason": "user_deleted" if locked is None else "session_epoch_moved",
            },
        )
        return

    row = PasswordResetToken(
        token_hash=token_hash,
        user_id=user.id,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()


async def delete_unused_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Delete the unused reset-token rows of *user_id*; return how many were deleted.

    Part of the credential reset that runs when a Google identity binds onto the
    row (spec/feature/AUTH.md §Credential reset on link): a pending reset link is
    a live re-entry path onto a row that has changed hands. Consumed rows
    (``used_at IS NOT NULL``) authenticate nothing and are left for the periodic
    housekeeping pass.

    The caller commits.
    """
    result = await db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]  # DML Result carries rowcount.


async def _load_valid_token(db: AsyncSession, token_hash: str) -> PasswordResetToken:
    """Return the live reset-token row for *token_hash*.

    Raises:
        BadRequestError('INVALID_RESET_TOKEN')  — missing, used, or expired token (HTTP 400).
    """
    result = await db.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()

    if row is None or row.used_at is not None or row.expires_at <= datetime.now(tz=UTC):
        raise BadRequestError(
            message="The reset token is invalid or expired.",
            error_code="INVALID_RESET_TOKEN",
        )
    return row


async def confirm_reset(
    db: AsyncSession,
    raw_token: str,
    new_password: str,
) -> None:
    """Validate a reset token and update the user's password hash.

    Marks the token row as ``used_at = now()`` so it cannot be replayed.

    The password write is a credential-creating write, so it runs under the
    ``users`` row lock and re-reads the reset-token row once the lock is held
    (spec/feature/AUTH.md §Serialization of credential-creating writes). A
    Google bind holding that lock deletes the row as part of its credential
    reset, so a confirm that was authorised before the bind committed finds
    nothing on the re-read and fails as an invalid token.

    Raises:
        BadRequestError('INVALID_RESET_TOKEN')  — missing, used, or expired token (HTTP 400).
    """
    token_hash = _hash(raw_token)

    # First read resolves the owner whose row must be locked.
    row = await _load_valid_token(db, token_hash)

    if await _users.lock_user(db, row.user_id) is None:
        raise BadRequestError(
            message="The reset token is invalid or expired.",
            error_code="INVALID_RESET_TOKEN",
        )

    # Re-validate the authorising credential now that the lock is held.
    row = await _load_valid_token(db, token_hash)

    # Update the user's password hash (no commit — caller orchestrates).
    await _users.update_password(db, row.user_id, new_password)

    # Mark token used.
    row.used_at = datetime.now(tz=UTC)
    await db.flush()
