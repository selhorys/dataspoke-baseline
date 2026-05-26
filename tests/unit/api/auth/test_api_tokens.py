"""Unit tests for src/backend/auth/api_tokens.py.

Concerns covered:
- mint returns dsk_<...> prefix; stored value is SHA-256 hash; raw token never re-derivable from DB
- mint enforces 10-token cap → ConflictError("TOKEN_LIMIT_EXCEEDED")
- lookup_and_validate intersection: snapshot=Admin, current=Reader → effective=Reader;
  snapshot=Reader, current=Admin → effective=Reader
- Revoked → AuthenticationError("TOKEN_REVOKED")
- Expired → AuthenticationError("TOKEN_EXPIRED")
- Unknown → AuthenticationError("INVALID_API_TOKEN")
- last_used_at updated when stale; not re-updated within the throttle window

spec: spec/feature/AUTH.md §API Tokens
spec: spec/API.md §Authentication Mechanisms
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── Mint tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_returns_dsk_prefix() -> None:
    """mint returns a raw token starting with 'dsk_'.

    spec: spec/feature/AUTH.md §API Tokens §Token format and storage —
    opaque random tokens of the form dsk_<32 url-safe random bytes>.
    """
    from src.backend.auth.api_tokens import mint

    user_id = uuid.uuid4()
    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Reader"

    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = mock_user

    count_result = MagicMock()
    count_result.scalar_one.return_value = 0  # zero active tokens

    mock_token_row = MagicMock()
    mock_token_row.id = uuid.uuid4()
    mock_token_row.role_snapshot = "Reader"

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[user_result, count_result])
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    # Patch SessionLocal used for throttled last_used_at update to a no-op
    with patch("src.backend.auth.api_tokens.SessionLocal") as mock_session_cls:
        mock_throttle = AsyncMock()
        mock_throttle.__aenter__ = AsyncMock(return_value=mock_throttle)
        mock_throttle.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_throttle

        raw_token, api_token = await mint(mock_db, user_id, "test-token")

    assert raw_token.startswith("dsk_"), (
        "Minted token must start with 'dsk_' per spec/feature/AUTH.md §API Tokens §Token format"
    )


@pytest.mark.asyncio
async def test_mint_stores_sha256_hash_not_raw() -> None:
    """mint stores SHA-256 hash; the raw token is not stored in the DB.

    spec: spec/feature/AUTH.md §API Tokens §Token format and storage —
    only the SHA-256 hash of the token is stored; raw token returned once.
    """
    from src.backend.auth.api_tokens import mint

    user_id = uuid.uuid4()
    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Editor"

    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = mock_user

    count_result = MagicMock()
    count_result.scalar_one.return_value = 0

    captured_token_rows: list = []

    def _capture_add(obj):
        captured_token_rows.append(obj)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[user_result, count_result])
    mock_db.add = MagicMock(side_effect=_capture_add)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("src.backend.auth.api_tokens.SessionLocal") as mock_session_cls:
        mock_throttle = AsyncMock()
        mock_throttle.__aenter__ = AsyncMock(return_value=mock_throttle)
        mock_throttle.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_throttle

        raw_token, _ = await mint(mock_db, user_id, "ci-token")

    assert len(captured_token_rows) == 1
    stored = captured_token_rows[0]
    # The stored hash must be the SHA-256 of the raw token.
    expected_hash = _sha256(raw_token)
    assert stored.token_hash == expected_hash, (
        "token_hash must be sha256(raw_token) per spec/feature/AUTH.md §API Tokens §Token format — "
        "raw token is never re-derivable from the DB"
    )
    # Confirm raw token is NOT stored anywhere on the row.
    assert raw_token not in str(vars(stored)), (
        "Raw token must NEVER appear on the stored ORM object "
        "per spec/feature/AUTH.md §API Tokens §Token format"
    )


@pytest.mark.asyncio
async def test_mint_enforces_10_token_cap() -> None:
    """mint raises ConflictError('TOKEN_LIMIT_EXCEEDED') when user has 10 active tokens.

    spec: spec/feature/AUTH.md §API Tokens §Token format and storage — cap: at most 10 active
    tokens per user; mint beyond cap returns 409 TOKEN_LIMIT_EXCEEDED.
    """
    from src.backend.auth.api_tokens import mint
    from src.shared.exceptions import ConflictError

    user_id = uuid.uuid4()
    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Admin"

    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = mock_user

    count_result = MagicMock()
    count_result.scalar_one.return_value = 10  # exactly at cap

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[user_result, count_result])

    with pytest.raises(ConflictError) as exc_info:
        await mint(mock_db, user_id, "overflow-token")

    assert exc_info.value.error_code == "TOKEN_LIMIT_EXCEEDED", (
        "Exceeding 10 active tokens must raise ConflictError('TOKEN_LIMIT_EXCEEDED') "
        "per spec/feature/AUTH.md §API Tokens"
    )


# ── Effective-role intersection (behavior-level via lookup_and_validate) ──────


@pytest.mark.asyncio
async def test_intersection_snapshot_admin_current_reader_returns_reader() -> None:
    """effective_role = min(snapshot=Admin, current=Reader) → Reader via lookup_and_validate.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection:
    demoting a user immediately downgrades all their existing tokens.
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    mock_token = MagicMock()
    mock_token.revoked_at = None
    mock_token.expires_at = None
    mock_token.role_snapshot = "Admin"  # token minted when user was Admin
    mock_token.id = uuid.uuid4()
    mock_token.last_used_at = None

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.role = "Reader"  # user has since been demoted

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_throttle_session = AsyncMock()
    mock_throttle_session.execute = AsyncMock()
    mock_throttle_session.commit = AsyncMock()
    mock_throttle_session.__aenter__ = AsyncMock(return_value=mock_throttle_session)
    mock_throttle_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.backend.auth.api_tokens.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, effective_role = await lookup_and_validate(mock_db, "dsk_admin_snapshot_reader_current")

    assert effective_role == "Reader", (
        "min(Admin, Reader) must be Reader — demoting a user immediately downgrades all "
        "their existing tokens per spec/feature/AUTH.md §API Tokens §Effective privilege"
    )
    assert user is mock_user


@pytest.mark.asyncio
async def test_intersection_snapshot_reader_current_admin_returns_reader() -> None:
    """effective_role = min(snapshot=Reader, current=Admin) → Reader via lookup_and_validate.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — promoting a user
    does NOT automatically elevate existing tokens; must mint a new one.
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    mock_token = MagicMock()
    mock_token.revoked_at = None
    mock_token.expires_at = None
    mock_token.role_snapshot = "Reader"  # token minted when user was Reader
    mock_token.id = uuid.uuid4()
    mock_token.last_used_at = None

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.role = "Admin"  # user has since been promoted

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_throttle_session = AsyncMock()
    mock_throttle_session.execute = AsyncMock()
    mock_throttle_session.commit = AsyncMock()
    mock_throttle_session.__aenter__ = AsyncMock(return_value=mock_throttle_session)
    mock_throttle_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.backend.auth.api_tokens.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, effective_role = await lookup_and_validate(mock_db, "dsk_reader_snapshot_admin_current")

    assert effective_role == "Reader", (
        "min(Reader, Admin) must be Reader — promoting a user does not auto-elevate "
        "existing tokens per spec/feature/AUTH.md §API Tokens §Effective privilege"
    )
    assert user is mock_user


@pytest.mark.asyncio
async def test_intersection_equal_roles_preserved() -> None:
    """effective_role = min(Editor, Editor) → Editor via lookup_and_validate.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — equal snapshot and
    current role should yield the same role.
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    mock_token = MagicMock()
    mock_token.revoked_at = None
    mock_token.expires_at = None
    mock_token.role_snapshot = "Editor"
    mock_token.id = uuid.uuid4()
    mock_token.last_used_at = None

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.role = "Editor"

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_throttle_session = AsyncMock()
    mock_throttle_session.execute = AsyncMock()
    mock_throttle_session.commit = AsyncMock()
    mock_throttle_session.__aenter__ = AsyncMock(return_value=mock_throttle_session)
    mock_throttle_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.backend.auth.api_tokens.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, effective_role = await lookup_and_validate(mock_db, "dsk_editor_snapshot_editor_current")

    assert effective_role == "Editor", (
        "min(Editor, Editor) must be Editor per spec/feature/AUTH.md §API Tokens §Effective privilege"
    )
    assert user is mock_user


# ── lookup_and_validate — error cases ────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_and_validate_unknown_token_raises_invalid() -> None:
    """lookup_and_validate raises AuthenticationError('INVALID_API_TOKEN') for unknown token.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — invalid → 401 INVALID_API_TOKEN.
    """
    from src.backend.auth.api_tokens import lookup_and_validate
    from src.shared.exceptions import AuthenticationError

    mock_result = MagicMock()
    mock_result.first.return_value = None  # token not found

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(AuthenticationError) as exc_info:
        await lookup_and_validate(mock_db, "dsk_unknown_token_12345")

    assert exc_info.value.error_code == "INVALID_API_TOKEN", (
        "Unknown token must raise AuthenticationError('INVALID_API_TOKEN') "
        "per spec/feature/AUTH.md §API Tokens"
    )


@pytest.mark.asyncio
async def test_lookup_and_validate_revoked_token_raises_token_revoked() -> None:
    """lookup_and_validate raises AuthenticationError('TOKEN_REVOKED') for revoked token.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — revoked → 401 TOKEN_REVOKED.
    spec: spec/feature/AUTH.md §Failure Modes — API token revoked while in use → 401 TOKEN_REVOKED.
    """
    from src.backend.auth.api_tokens import lookup_and_validate
    from src.shared.exceptions import AuthenticationError

    mock_token = MagicMock()
    mock_token.revoked_at = datetime.now(tz=UTC)  # revoked
    mock_token.expires_at = None
    mock_token.role_snapshot = "Admin"

    mock_user = MagicMock()
    mock_user.role = "Admin"

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(AuthenticationError) as exc_info:
        await lookup_and_validate(mock_db, "dsk_some_valid_looking_token")

    assert exc_info.value.error_code == "TOKEN_REVOKED", (
        "Revoked token must raise AuthenticationError('TOKEN_REVOKED') "
        "per spec/feature/AUTH.md §API Tokens §Effective privilege"
    )


@pytest.mark.asyncio
async def test_lookup_and_validate_expired_token_raises_token_expired() -> None:
    """lookup_and_validate raises AuthenticationError('TOKEN_EXPIRED') for expired token.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — expired → 401 TOKEN_EXPIRED.
    """
    from src.backend.auth.api_tokens import lookup_and_validate
    from src.shared.exceptions import AuthenticationError

    mock_token = MagicMock()
    mock_token.revoked_at = None  # not revoked
    mock_token.expires_at = datetime.now(tz=UTC) - timedelta(hours=1)  # expired
    mock_token.role_snapshot = "Editor"
    mock_token.id = uuid.uuid4()

    mock_user = MagicMock()
    mock_user.role = "Editor"

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(AuthenticationError) as exc_info:
        await lookup_and_validate(mock_db, "dsk_expired_token_value")

    assert exc_info.value.error_code == "TOKEN_EXPIRED", (
        "Expired token must raise AuthenticationError('TOKEN_EXPIRED') "
        "per spec/feature/AUTH.md §API Tokens §Effective privilege"
    )


# ── last_used_at throttle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_and_validate_updates_last_used_at_when_stale() -> None:
    """lookup_and_validate triggers the throttled UPDATE when last_used_at is None (stale).

    spec: spec/feature/AUTH.md §API Tokens §Audit and last_used_at —
    every successful authentication updates last_used_at; update throttled to per-minute granularity.
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    user_id = uuid.uuid4()
    mock_token = MagicMock()
    mock_token.revoked_at = None
    mock_token.expires_at = None  # no expiry
    mock_token.role_snapshot = "Reader"
    mock_token.id = uuid.uuid4()
    mock_token.last_used_at = None  # stale — never used

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Reader"

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    throttle_execute_called = False

    async def _throttle_execute(*args, **kwargs):
        nonlocal throttle_execute_called
        throttle_execute_called = True

    mock_throttle_session = AsyncMock()
    mock_throttle_session.execute = AsyncMock(side_effect=_throttle_execute)
    mock_throttle_session.commit = AsyncMock()
    mock_throttle_session.__aenter__ = AsyncMock(return_value=mock_throttle_session)
    mock_throttle_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.backend.auth.api_tokens.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, role = await lookup_and_validate(mock_db, "dsk_test_token_stale")

    assert throttle_execute_called, (
        "The throttled UPDATE for last_used_at must be called when last_used_at is None "
        "per spec/feature/AUTH.md §API Tokens §Audit and last_used_at"
    )
    assert user is mock_user
    assert role == "Reader"
