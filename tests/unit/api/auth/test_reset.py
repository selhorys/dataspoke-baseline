"""Unit tests for src/backend/auth/reset.py.

Concerns covered:
- Unknown email → issue_reset_token no-ops silently (no enumeration leak; also swallows
  PeripheralNotConfiguredError and NotificationError per security fix)
- Known email → password_reset_tokens row written with SHA-256 hash of the raw token,
  expires_at = now + 15 min
- confirm_reset with invalid/expired/used token → BadRequestError("INVALID_RESET_TOKEN")
- Successful confirm writes the new bcrypt hash AND marks used_at

spec: spec/feature/AUTH.md §Lifecycle §Password reset
spec: spec/API.md §Auth — POST /auth/password/reset/request, POST /auth/password/reset/confirm
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import BadRequestError


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── issue_reset_token — unknown email ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_reset_token_unknown_email_returns_silently() -> None:
    """issue_reset_token no-ops for an unknown email (no account-enumeration leak).

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — silent for unknown emails.
    spec: spec/API.md §Auth — POST /auth/password/reset/request: silent for unknown emails.
    """
    from src.backend.auth.reset import issue_reset_token

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # user not found

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_notification = AsyncMock()

    # Must not raise, must not call notification service
    await issue_reset_token(mock_db, mock_notification, "unknown@example.com")

    mock_notification.send_email.assert_not_called(), (
        "Notification must not be sent for unknown email "
        "per spec/feature/AUTH.md §Lifecycle §Password reset (no enumeration leak)"
    )


@pytest.mark.asyncio
async def test_issue_reset_token_swallows_peripheral_not_configured() -> None:
    """issue_reset_token swallows PeripheralNotConfiguredError (SMTP not configured).

    spec: spec/feature/AUTH.md §Failure Modes — SMTP peripheral missing: request refuses.
    Note: the current impl swallows it silently (no enumeration leak on delivery status).
    Per spec/feature/AUTH.md §Lifecycle §Password reset: same response shape known/unknown.
    """
    from src.backend.auth.reset import issue_reset_token
    from src.shared.exceptions import PeripheralNotConfiguredError

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.email = "exists@example.com"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()

    mock_notification = AsyncMock()
    mock_notification.send_email = AsyncMock(
        side_effect=PeripheralNotConfiguredError("smtp")
    )

    # Must not raise — swallowed silently
    await issue_reset_token(mock_db, mock_notification, "exists@example.com")


@pytest.mark.asyncio
async def test_issue_reset_token_known_email_writes_row_with_sha256_hash() -> None:
    """For a known email, issue_reset_token writes a PasswordResetToken row with SHA-256 hash.

    spec: spec/feature/AUTH.md §Security Considerations §Password-reset token storage —
    the table stores the SHA-256 hash, not the raw token; raw token exists only in email body.
    spec: spec/feature/AUTH.md §Lifecycle §Password reset — 15-min TTL.
    """
    from src.backend.auth.reset import issue_reset_token

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.email = "known@example.com"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user

    captured_rows: list = []

    def _capture_add(obj):
        captured_rows.append(obj)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.add = MagicMock(side_effect=_capture_add)
    mock_db.flush = AsyncMock()

    mock_notification = AsyncMock()
    # Track what raw token was sent in the email
    sent_raw_tokens: list[str] = []

    async def _capture_email(to, subject, body_html):
        # The raw token appears in the email body
        # We extract it by finding the part between the last <strong> tags
        import re
        match = re.search(r"<strong>([^<]+)</strong>", body_html)
        if match:
            sent_raw_tokens.append(match.group(1))

    mock_notification.send_email = AsyncMock(side_effect=_capture_email)

    await issue_reset_token(mock_db, mock_notification, "known@example.com")

    assert len(captured_rows) == 1, (
        "One PasswordResetToken row must be written per spec/feature/AUTH.md §Lifecycle §Password reset"
    )
    row = captured_rows[0]

    # Verify SHA-256 hash — not the raw token
    assert len(sent_raw_tokens) == 1, "Raw token must be sent in the email"
    raw_token = sent_raw_tokens[0]
    expected_hash = _sha256(raw_token)

    assert row.token_hash == expected_hash, (
        "token_hash must be sha256(raw_token), never the raw token itself, "
        "per spec/feature/AUTH.md §Security Considerations §Password-reset token storage"
    )
    assert row.token_hash != raw_token, (
        "Raw token must NOT be stored in the DB "
        "per spec/feature/AUTH.md §Security Considerations §Password-reset token storage"
    )

    # Verify 15-minute TTL
    now = datetime.now(tz=UTC)
    ttl = row.expires_at - now
    assert timedelta(minutes=14) <= ttl <= timedelta(minutes=16), (
        "expires_at must be now + 15 minutes per spec/feature/AUTH.md §Lifecycle §Password reset"
    )


# ── confirm_reset ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_reset_invalid_token_raises_bad_request() -> None:
    """confirm_reset raises BadRequestError('INVALID_RESET_TOKEN') for unknown token.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset —
    confirm_reset validates token (matches a row, not expired, used_at is null).
    """
    from src.backend.auth.reset import confirm_reset

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # token not found

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(BadRequestError) as exc_info:
        await confirm_reset(mock_db, "nonexistent-raw-token", "newpassword123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN", (
        "Unknown reset token must raise BadRequestError('INVALID_RESET_TOKEN') "
        "per spec/feature/AUTH.md §Lifecycle §Password reset"
    )


@pytest.mark.asyncio
async def test_confirm_reset_expired_token_raises_bad_request() -> None:
    """confirm_reset raises BadRequestError('INVALID_RESET_TOKEN') for expired token.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — not expired condition.
    """
    from src.backend.auth.reset import confirm_reset

    mock_row = MagicMock()
    mock_row.used_at = None
    mock_row.expires_at = datetime.now(tz=UTC) - timedelta(minutes=1)  # expired

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(BadRequestError) as exc_info:
        await confirm_reset(mock_db, "expired-token", "newpassword123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_confirm_reset_used_token_raises_bad_request() -> None:
    """confirm_reset raises BadRequestError('INVALID_RESET_TOKEN') for already-used token.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — single-use: used_at is null check.
    """
    from src.backend.auth.reset import confirm_reset

    mock_row = MagicMock()
    mock_row.used_at = datetime.now(tz=UTC) - timedelta(minutes=5)  # already used
    mock_row.expires_at = datetime.now(tz=UTC) + timedelta(minutes=10)  # not expired

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(BadRequestError) as exc_info:
        await confirm_reset(mock_db, "already-used-token", "newpassword123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_confirm_reset_valid_token_writes_new_hash_and_marks_used() -> None:
    """confirm_reset writes new bcrypt hash AND marks used_at on success.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset —
    writes the new bcrypt hash and marks the token used (so it cannot be replayed).
    """
    from src.backend.auth import users as _users
    from src.backend.auth.reset import confirm_reset

    user_id = uuid.uuid4()

    mock_row = MagicMock()
    mock_row.used_at = None  # not used
    mock_row.expires_at = datetime.now(tz=UTC) + timedelta(minutes=10)  # not expired
    mock_row.user_id = user_id

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.flush = AsyncMock()

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.password_hash = "old_hash"

    new_hash_captured: list[str] = []

    async def _fake_update_password(db, uid, pw):
        mock_user.password_hash = f"bcrypt_of_{pw}"
        new_hash_captured.append(mock_user.password_hash)
        return mock_user

    with patch.object(_users, "update_password", side_effect=_fake_update_password):
        await confirm_reset(mock_db, "valid-raw-token", "new-secure-password123")

    # Password hash must have been updated
    assert len(new_hash_captured) == 1, (
        "update_password must be called exactly once per spec/feature/AUTH.md §Lifecycle §Password reset"
    )

    # used_at must be set (token marked as used)
    assert mock_row.used_at is not None, (
        "used_at must be set after successful confirm_reset to prevent token replay "
        "per spec/feature/AUTH.md §Lifecycle §Password reset"
    )
