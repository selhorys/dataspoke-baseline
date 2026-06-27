"""Password-reset token issuance and confirmation.

Issue creates a single-use row in ``password_reset_tokens`` (15-min TTL)
and sends an email via NotificationService.  Confirm validates the token
and rewrites the user's bcrypt hash.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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
            to=[email], subject=subject, body_html=body_html
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

    # Email delivered — write the token row.
    row = PasswordResetToken(
        token_hash=token_hash,
        user_id=user.id,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()


async def confirm_reset(
    db: AsyncSession,
    raw_token: str,
    new_password: str,
) -> None:
    """Validate a reset token and update the user's password hash.

    Marks the token row as ``used_at = now()`` so it cannot be replayed.

    Raises:
        BadRequestError('INVALID_RESET_TOKEN')  — missing, used, or expired token (HTTP 400).
    """
    token_hash = _hash(raw_token)

    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    row = result.scalar_one_or_none()

    if row is None or row.used_at is not None or row.expires_at <= datetime.now(tz=UTC):
        raise BadRequestError(
            message="The reset token is invalid or expired.",
            error_code="INVALID_RESET_TOKEN",
        )

    # Update the user's password hash (no commit — caller orchestrates).
    await _users.update_password(db, row.user_id, new_password)

    # Mark token used.
    row.used_at = datetime.now(tz=UTC)
    await db.flush()
