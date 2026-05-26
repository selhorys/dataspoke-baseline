"""Unit tests for src/backend/auth/users.py.

Concerns covered:
- bcrypt+SHA-256 prehash round-trip (verify_password accepts the matching password)
- create_user UNIQUE-email violation maps to ConflictError("EMAIL_ALREADY_REGISTERED")
- update_role invalid role maps to PreconditionFailedError("INVALID_ROLE")
- verify_password returns False when user.password_hash is None (Google-only user)
- link_google_sub UNIQUE collision raises ConflictError("GOOGLE_ACCOUNT_LINKED_ELSEWHERE")

spec: spec/feature/AUTH.md §Data Model
spec: spec/feature/AUTH.md §Security Considerations §Password storage
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.auth.users import verify_password
from src.shared.exceptions import ConflictError, PreconditionFailedError


# ── Password helpers ──────────────────────────────────────────────────────────


def test_hash_then_verify_round_trip() -> None:
    """Hash followed by verify_password returns True for the same password.

    Uses create_user to produce the hash (via the public API) so the test
    does not replicate the internal _hash_password protocol.

    spec: spec/feature/AUTH.md §Lifecycle §Login — bcrypt verify on the stored hash.
    spec: spec/feature/AUTH.md §Security Considerations §Password storage.
    """
    import asyncio

    from src.backend.auth import users as user_service

    password = "correct-horse-battery-staple!"

    # Produce the hash via create_user with a mocked DB session.
    # capture[0] is set inside the mocked db.add() call (same object that flush/refresh see).
    capture: list = []

    async def _run():
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        def _add(obj):
            capture.append(obj)

        mock_db.add = _add
        await user_service.create_user(mock_db, "test@example.com", "Test", password=password)

    asyncio.run(_run())

    assert capture, "create_user must call db.add(user)"
    user_obj = capture[0]
    assert user_obj.password_hash is not None

    mock_user = MagicMock()
    mock_user.password_hash = user_obj.password_hash

    result = asyncio.run(verify_password(mock_user, password))
    assert result is True, "verify_password must return True for the matching password"


def test_hash_then_verify_wrong_password_returns_false() -> None:
    """verify_password returns False for a non-matching password.

    Uses create_user to produce the hash via the public API.

    spec: spec/feature/AUTH.md §Lifecycle §Login — invalid credentials → 401 UNAUTHORIZED.
    """
    import asyncio

    from src.backend.auth import users as user_service

    capture: list = []

    async def _run():
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        def _add(obj):
            capture.append(obj)

        mock_db.add = _add
        await user_service.create_user(mock_db, "test2@example.com", "Test", password="correct-password-here!")

    asyncio.run(_run())

    assert capture, "create_user must call db.add(user)"
    mock_user = MagicMock()
    mock_user.password_hash = capture[0].password_hash

    result = asyncio.run(verify_password(mock_user, "wrong-password-here!"))
    assert result is False


def test_verify_password_google_only_user_returns_false() -> None:
    """verify_password returns False when user.password_hash is None (Google-only account).

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    a Google-only user has password_hash=null; password login must fail.
    """
    mock_user = MagicMock()
    mock_user.password_hash = None

    import asyncio

    result = asyncio.run(verify_password(mock_user, "any-password"))
    assert result is False, (
        "verify_password must return False when password_hash is None "
        "(Google-only account per spec/feature/AUTH.md §Data Model)"
    )


# ── create_user — UNIQUE email violation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_user_duplicate_email_raises_conflict() -> None:
    """create_user raises ConflictError('EMAIL_ALREADY_REGISTERED') on duplicate email.

    spec: spec/feature/AUTH.md §Data Model — email is a UNIQUE citext column;
    second registration with same email must be rejected.
    spec: spec/API.md §Error Catalogue — 409 EMAIL_ALREADY_REGISTERED.
    """
    from sqlalchemy.exc import IntegrityError

    from src.backend.auth.users import create_user

    # Simulate an IntegrityError with the unique-email constraint name.
    mock_db = AsyncMock()
    mock_orig = MagicMock()
    mock_orig.constraint_name = "uq_users_email"
    mock_orig.diag = None  # asyncpg path: constraint_name is on orig directly

    int_err = IntegrityError("statement", {}, mock_orig)
    mock_db.flush = AsyncMock(side_effect=int_err)
    mock_db.rollback = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.refresh = AsyncMock()

    with pytest.raises(ConflictError) as exc_info:
        await create_user(mock_db, "dup@example.com", "Dup User", password="password1234")

    assert exc_info.value.error_code == "EMAIL_ALREADY_REGISTERED", (
        "Duplicate email must raise ConflictError('EMAIL_ALREADY_REGISTERED') "
        "per spec/feature/AUTH.md §Data Model"
    )


# ── update_role — invalid role ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_role_invalid_value_raises_precondition() -> None:
    """update_role raises PreconditionFailedError('INVALID_ROLE') on constraint violation.

    spec: spec/feature/AUTH.md §Privilege Model — role is Admin | Editor | Reader;
    any other value violates the CHECK constraint.
    """
    from sqlalchemy.exc import IntegrityError

    from src.backend.auth.users import update_role

    mock_orig = MagicMock()
    mock_orig.constraint_name = "ck_users_role"
    mock_orig.diag = None

    int_err = IntegrityError("statement", {}, mock_orig)

    user_id = uuid.uuid4()
    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Reader"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.flush = AsyncMock(side_effect=int_err)
    mock_db.rollback = AsyncMock()

    with pytest.raises(PreconditionFailedError) as exc_info:
        await update_role(mock_db, user_id, "SuperAdmin")

    assert exc_info.value.error_code == "INVALID_ROLE", (
        "Invalid role value must raise PreconditionFailedError('INVALID_ROLE') "
        "per spec/feature/AUTH.md §Privilege Model"
    )


# ── link_google_sub — UNIQUE collision ────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_google_sub_duplicate_raises_conflict() -> None:
    """link_google_sub raises ConflictError('GOOGLE_ACCOUNT_LINKED_ELSEWHERE') on constraint violation.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    linking preserves password access; a google_sub already linked to another user
    must be rejected.
    """
    from sqlalchemy.exc import IntegrityError

    from src.backend.auth.users import link_google_sub

    mock_orig = MagicMock()
    mock_orig.constraint_name = "uq_users_google_sub"
    mock_orig.diag = None

    int_err = IntegrityError("statement", {}, mock_orig)

    user_id = uuid.uuid4()
    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.google_sub = None

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.flush = AsyncMock(side_effect=int_err)
    mock_db.rollback = AsyncMock()

    with pytest.raises(ConflictError) as exc_info:
        await link_google_sub(mock_db, user_id, "google-sub-already-taken")

    assert exc_info.value.error_code == "GOOGLE_ACCOUNT_LINKED_ELSEWHERE", (
        "Google sub already linked to another user must raise "
        "ConflictError('GOOGLE_ACCOUNT_LINKED_ELSEWHERE') "
        "per spec/feature/AUTH.md §Lifecycle §Google OAuth"
    )
