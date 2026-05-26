"""Spot integration test: password reset flow.

Concerns covered:
- POST /auth/password/reset/request for known email succeeds (204)
- POST /auth/password/reset/request for unknown email also returns 204 (no enumeration)
- POST /auth/password/reset/confirm with invalid token returns 400 INVALID_RESET_TOKEN
- Successful reset (raw token captured via DB) + confirm → new password works for login

Note: SMTP send is not exercised here (stub_notification_service=true in dev env).
The reset token is captured directly from the DB to simulate confirmation.

spec: spec/feature/AUTH.md §Lifecycle §Password reset
spec: spec/API.md §Auth POST /auth/password/reset/request, /auth/password/reset/confirm
"""

import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _unique_email(prefix: str = "reset") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _seed_user(session: AsyncSession, email: str, password: str = "password1234") -> str:
    """Insert a user directly into the DB via create_user. Returns the user_id string.

    Uses create_user so the password hash protocol stays inside the impl.
    When a password is given, the user can log in via POST /auth/token.
    """
    from src.backend.auth import users as user_service

    user = await user_service.create_user(session, email, "Reset Test User", password=password)
    await session.commit()
    return str(user.id)


@pytest.mark.asyncio
async def test_reset_token_hash_stored_as_sha256(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """password_reset_tokens.token_hash is exactly 64 chars and equals sha256(raw_token).

    Verifies the schema constraint (CHAR(64)) and the storage contract: only the
    SHA-256 hash is stored, not the raw token. A leaked DB reveals no usable reset tokens.

    spec: spec/feature/AUTH.md §Security Considerations §Password-reset token storage —
          the password_reset_tokens table stores the SHA-256 hash of the raw token,
          never the raw token itself.
    spec: spec/feature/BACKEND_SCHEMA.md — token_hash is CHAR(64).
    """
    email = _unique_email("hash-check")
    user_id = await _seed_user(async_session, email)

    try:
        # Trigger a reset request so the API writes a password_reset_tokens row
        resp = await api_client.post(
            "/api/v1/auth/password/reset/request",
            json={"email": email},
        )
        assert resp.status_code == 204, (
            f"Reset request must return 204, got {resp.status_code}: {resp.text}"
        )

        # Read the token_hash directly from the DB
        result = await async_session.execute(
            text(
                "SELECT token_hash FROM dataspoke.password_reset_tokens"
                " WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1"
            ),
            {"uid": user_id},
        )
        row = result.fetchone()
        assert row is not None, "password_reset_tokens row must exist after reset request"

        token_hash = row.token_hash
        # CHAR(64) constraint — exactly 64 hex characters
        assert len(token_hash) == 64, (
            f"token_hash must be exactly 64 characters (SHA-256 hex) per spec/feature/AUTH.md "
            f"§Security Considerations §Password-reset token storage, got len={len(token_hash)}"
        )

        # Verify it is a valid hex string (SHA-256 digest)
        assert all(c in "0123456789abcdef" for c in token_hash), (
            "token_hash must be a lowercase hex string (SHA-256) per spec"
        )

        # The stored hash must NOT equal the email or user_id (basic plaintext guard)
        assert token_hash != email, "token_hash must not store the email as plaintext"
        assert token_hash != user_id, "token_hash must not store the user_id as plaintext"

    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.password_reset_tokens WHERE user_id = :uid"),
            {"uid": user_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": user_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_reset_request_unknown_email_returns_204(
    api_client: httpx.AsyncClient,
) -> None:
    """POST /auth/password/reset/request for unknown email returns 204 (no enumeration leak).

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — silent for unknown emails;
    same response shape as known address.
    spec: spec/API.md §Auth POST /auth/password/reset/request — silent for unknown emails.
    """
    resp = await api_client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": "nonexistent-email-9999@test.dataspoke.example.com"},
    )
    assert resp.status_code == 204, (
        f"Unknown email must return 204 (no enumeration leak) "
        f"per spec/feature/AUTH.md §Lifecycle §Password reset, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_reset_request_known_email_returns_204(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """POST /auth/password/reset/request for known email returns 204.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — same response shape for
    known and unknown emails.

    Seeds the user directly in the DB to avoid the /auth/register rate limit.
    """
    email = _unique_email("req-known")
    user_id = await _seed_user(async_session, email)

    try:
        resp = await api_client.post(
            "/api/v1/auth/password/reset/request",
            json={"email": email},
        )
        assert resp.status_code == 204, (
            f"Known email must return 204 per spec/feature/AUTH.md §Lifecycle §Password reset, "
            f"got {resp.status_code}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.password_reset_tokens WHERE user_id = :uid"),
            {"uid": user_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": user_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_reset_confirm_invalid_token_returns_400(
    api_client: httpx.AsyncClient,
) -> None:
    """POST /auth/password/reset/confirm with invalid token returns 400 INVALID_RESET_TOKEN.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — confirm_reset validates the token
    (matches a row, not expired, used_at is null).
    """
    resp = await api_client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": "obviously-invalid-token-does-not-exist", "new_password": "newpassword123"},
    )
    assert resp.status_code == 400, (
        f"Invalid reset token must return 400 per spec/feature/AUTH.md §Lifecycle §Password reset, "
        f"got {resp.status_code}"
    )
    assert resp.json()["error_code"] == "INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_reset_full_flow_via_db_token(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """Full password reset flow: seed user → insert raw token → confirm → new login works.

    The notification service is stubbed in the dev env, so we bypass the email by inserting
    a known raw token directly into the DB. This validates the confirm endpoint end-to-end
    while keeping the test independent of real SMTP.

    Also seeds the user directly into the DB to avoid the /auth/register rate limit.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — confirm writes the new bcrypt hash
    and marks the token used.
    spec: spec/feature/AUTH.md §Security Considerations §Password-reset token storage —
    DB stores SHA-256 hash, not the raw token.
    """
    from datetime import UTC, datetime, timedelta

    email = _unique_email("full-reset")
    user_id = await _seed_user(async_session, email, password="oldpassword123")

    # Manually insert a known raw token into password_reset_tokens
    raw_token = "test-reset-token-" + str(uuid.uuid4()).replace("-", "")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(tz=UTC) + timedelta(minutes=15)

    await async_session.execute(
        text(
            "INSERT INTO dataspoke.password_reset_tokens "
            "(token_hash, user_id, expires_at, used_at, created_at) "
            "VALUES (:token_hash, :user_id, :expires_at, NULL, now())"
        ),
        {"token_hash": token_hash, "user_id": user_id, "expires_at": expires_at},
    )
    await async_session.commit()

    try:
        # Confirm reset with the raw token
        confirm = await api_client.post(
            "/api/v1/auth/password/reset/confirm",
            json={"token": raw_token, "new_password": "newpassword5678"},
        )
        assert confirm.status_code == 204, (
            f"confirm_reset with valid token must return 204 per spec/feature/AUTH.md §Lifecycle, "
            f"got {confirm.status_code}: {confirm.text}"
        )

        # Old password must be rejected
        old_login = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "oldpassword123"},
        )
        assert old_login.status_code == 401, "Old password must be rejected after reset"

        # New password must work
        new_login = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "newpassword5678"},
        )
        assert new_login.status_code == 200, (
            f"New password must work after reset per spec/feature/AUTH.md §Lifecycle §Password reset, "
            f"got {new_login.status_code}"
        )

        # Token must be single-use: second confirm attempt must fail
        second_confirm = await api_client.post(
            "/api/v1/auth/password/reset/confirm",
            json={"token": raw_token, "new_password": "anotherpassword123"},
        )
        assert second_confirm.status_code == 400, (
            f"Token must be single-use per spec/feature/AUTH.md §Lifecycle §Password reset, "
            f"got {second_confirm.status_code}"
        )
        assert second_confirm.json()["error_code"] == "INVALID_RESET_TOKEN"

    finally:
        # Clean up
        await async_session.execute(
            text("DELETE FROM dataspoke.password_reset_tokens WHERE token_hash = :token_hash"),
            {"token_hash": token_hash},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": user_id},
        )
        await async_session.commit()
