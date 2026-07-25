"""Unit tests for src/backend/auth/users.py.

Concerns covered:
- bcrypt+SHA-256 prehash round-trip (verify_password accepts the matching password)
- create_user UNIQUE-email violation maps to ConflictError("EMAIL_ALREADY_REGISTERED")
- update_role invalid role maps to PreconditionFailedError("INVALID_ROLE")
- verify_password returns False when user.password_hash is None (Google-only user)
- bind_google_identity: the bind-and-reset branch, the same-``sub`` no-op, the
  bound-to-another-account refusal, and the UNIQUE(google_sub) collision
- unbind_google_identity: release + epoch bump, the already-unbound no-op, and the
  password-less refusal

spec: spec/feature/AUTH.md §Data Model
spec: spec/feature/AUTH.md §Security Considerations §Password storage
spec: spec/feature/AUTH.md §Credential reset on link
spec: spec/feature/AUTH.md §Admin unbind
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.auth.users import verify_password
from src.shared.db.models import User
from src.shared.exceptions import ConflictError, PreconditionFailedError

# ── Password helpers ──────────────────────────────────────────────────────────


def test_hash_then_verify_round_trip() -> None:
    """Hash followed by verify_password returns True for the same password.

    Uses create_user to produce the hash (via the public API) so the test
    does not replicate the internal _hash_password protocol.

    spec: spec/feature/AUTH.md §Security Considerations §Password storage — bcrypt
    verify on the stored hash.
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

    spec: spec/feature/AUTH.md §Security Considerations §Password storage — invalid
    credentials fail bcrypt verify → 401 UNAUTHORIZED.
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
        await user_service.create_user(
            mock_db, "test2@example.com", "Test", password="correct-password-here!"
        )

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


# ── create_user — email normalisation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_user_lowercases_stored_email_and_derived_corpuser_urn() -> None:
    """create_user stores the email lowercased so the derived corpuser URN matches DataHub's.

    spec: spec/feature/AUTH.md §DataHub Projection Semantics §URN conventions —
    "The email is lowercased before URN derivation. DataSpoke users.email is CITEXT
    — case-insensitive on compare, but case-preserving on storage — while the
    corpuser URN is case-sensitive, so a row stored as `Bob@example.com` must still
    derive `urn:li:corpuser:bob@example.com` to meet the URN DataHub provisions."
    """
    from src.backend.auth.users import create_user
    from src.backend.datahub.users import corpuser_urn

    captured: list = []

    mock_db = AsyncMock()
    mock_db.add = captured.append
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    await create_user(mock_db, "Bob@example.com", "Bob Smith", password="password1234")

    assert captured, "create_user must call db.add(user)"
    stored_email = captured[0].email
    assert stored_email == "bob@example.com", (
        "create_user must store the email lowercased per spec/feature/AUTH.md "
        f"§URN conventions; got {stored_email!r}"
    )
    assert corpuser_urn(stored_email) == "urn:li:corpuser:bob@example.com", (
        "The corpuser URN derived from the stored email must be the URN DataHub's "
        "OIDC JIT provisions per spec/feature/AUTH.md §URN conventions"
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


# ── Google binding — shared fake session ──────────────────────────────────────


class _RoutingSession:
    """Query-routing fake AsyncSession for the Google-binding write helpers.

    ``execute`` dispatches on the compiled statement text rather than on call
    order, so adding, reordering, or short-circuiting a query in the code under
    test cannot shift a positional result list onto the wrong assertion
    (spec/TESTING.md §Unit Testing → Mocking rules). Every statement issued is
    recorded, which is what lets a test assert that the credential reset really
    reached ``api_tokens`` and ``password_reset_tokens`` — and that the
    write-nothing branches reached neither.
    """

    def __init__(
        self,
        user: User | None,
        *,
        active_api_tokens: int = 0,
        unused_reset_tokens: int = 0,
        flush_error: Exception | None = None,
    ) -> None:
        self.user = user
        self._active_api_tokens = active_api_tokens
        self._unused_reset_tokens = unused_reset_tokens
        self._flush_error = flush_error
        self.statements: list[str] = []
        self.flush_count = 0
        self.rollback_count = 0
        self.commit_count = 0

    async def execute(self, statement: Any) -> Any:
        sql = str(statement)
        self.statements.append(sql)
        result = MagicMock()
        if sql.startswith("SELECT") and "users" in sql:
            result.scalar_one_or_none.return_value = self.user
            return result
        if sql.startswith("UPDATE") and "api_tokens" in sql:
            result.rowcount = self._active_api_tokens
            return result
        if sql.startswith("DELETE") and "password_reset_tokens" in sql:
            result.rowcount = self._unused_reset_tokens
            return result
        raise AssertionError(f"unrouted statement in fake session: {sql}")

    async def flush(self) -> None:
        self.flush_count += 1
        if self._flush_error is not None:
            raise self._flush_error

    async def refresh(self, obj: Any) -> None:
        return None

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    # ── statement predicates ──
    def locked_the_user_row(self) -> bool:
        return any(s.startswith("SELECT") and "FOR UPDATE" in s for s in self.statements)

    def api_token_revocations(self) -> list[str]:
        return [
            s for s in self.statements if s.startswith("UPDATE") and "api_tokens" in s
        ]

    def reset_token_deletions(self) -> list[str]:
        return [
            s
            for s in self.statements
            if s.startswith("DELETE") and "password_reset_tokens" in s
        ]


def _password_row(**overrides: Any) -> User:
    """Return a detached ``users`` row shaped like a password registration.

    ``ck_users_auth_method`` forces such a row to carry a password while it is
    unbound, which is why a bind always has at least one credential to clear
    (spec/feature/AUTH.md §Credential reset on link).
    """
    row = User(
        email="squatter@imazon.example.com",
        name="Pre-registered Holder",
        password_hash="$2b$12$fake-bcrypt-hash-for-the-pre-bind-password",
        google_sub=None,
        role="Reader",
        session_epoch=1,
    )
    row.id = uuid.uuid4()
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


# ── bind_google_identity — bind + credential reset ────────────────────────────


@pytest.mark.asyncio
async def test_bind_google_identity_clears_every_pre_bind_credential() -> None:
    """A bind onto an unbound row nulls the password, bumps the epoch, and kills its tokens.

    spec: spec/feature/AUTH.md §Credential reset on link — "every credential that
    existed on the row before the bind is invalidated in the same transaction":
    Password → "password_hash set to NULL"; API tokens → "Every active token for
    the user is revoked"; JWT sessions → "killed by incrementing session_epoch";
    Password-reset tokens → "Unused password_reset_tokens rows for the user are
    deleted".
    """
    from src.backend.auth.users import bind_google_identity

    row = _password_row()
    db = _RoutingSession(row, active_api_tokens=3, unused_reset_tokens=2)

    result = await bind_google_identity(db, row.id, "google-sub-of-the-real-owner")

    assert result.bound is True
    assert row.google_sub == "google-sub-of-the-real-owner", (
        "the verified identity must take the row per spec/feature/AUTH.md "
        "§Credential reset on link"
    )
    assert row.password_hash is None, (
        "Password → password_hash set to NULL per spec/feature/AUTH.md "
        "§Credential reset on link"
    )
    assert row.session_epoch == 2, (
        "a credential reset increments session_epoch by one in the same transaction "
        "per spec/feature/AUTH.md §Session epoch §Exactness"
    )
    assert result.api_tokens_revoked == 3
    assert result.reset_tokens_deleted == 2
    assert db.api_token_revocations(), (
        "the reset must revoke the row's active API tokens per spec/feature/AUTH.md "
        "§Credential reset on link"
    )
    assert db.reset_token_deletions(), (
        "the reset must delete the row's unused password-reset tokens per "
        "spec/feature/AUTH.md §Credential reset on link"
    )
    assert db.locked_the_user_row(), (
        "the reset must hold the users row lock so credential-creating writes "
        "serialize against it per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )
    assert db.commit_count == 0, (
        "bind_google_identity must not commit — the bind, its reset, and the event "
        "row commit together or not at all (the caller commits)"
    )


@pytest.mark.asyncio
async def test_bind_google_identity_same_sub_writes_nothing() -> None:
    """A row already carrying THIS sub is a login: no bind, no reset, no epoch bump.

    The raced/retried-callback resolution. Re-running the reset here would bump the
    epoch a second time and kill the session the first callback just issued.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "Yes, and that
    row already carries **this** `sub` | Log in, exactly as the `sub`-known branch.
    No bind, no reset, no epoch bump, no event."
    """
    from src.backend.auth.users import bind_google_identity

    sub = "google-sub-already-committed-by-the-first-callback"
    row = _password_row(google_sub=sub, password_hash=None, session_epoch=2)
    db = _RoutingSession(row, active_api_tokens=3, unused_reset_tokens=2)

    result = await bind_google_identity(db, row.id, sub)

    assert result.bound is False, (
        "a row already carrying this sub must report no bind per spec/feature/AUTH.md "
        "§Google OAuth registration & login"
    )
    assert result.api_tokens_revoked == 0
    assert result.reset_tokens_deleted == 0
    assert row.session_epoch == 2, (
        "no epoch bump on the same-sub branch per spec/feature/AUTH.md "
        "§Google OAuth registration & login"
    )
    assert row.google_sub == sub
    # The session was primed with 3 revocable tokens and 2 deletable reset rows —
    # their survival is what proves the branch wrote nothing.
    assert db.api_token_revocations() == []
    assert db.reset_token_deletions() == []
    assert db.flush_count == 0, "the same-sub branch must issue no write at all"


@pytest.mark.asyncio
async def test_bind_google_identity_refuses_a_row_bound_to_another_account() -> None:
    """A row carrying a DIFFERENT google_sub is refused; nothing on it is modified.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "Yes, and that
    row carries a **different** `google_sub` | Refuse — 409
    EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT. No row is modified and no session is
    issued."
    """
    from src.backend.auth.users import bind_google_identity

    row = _password_row(
        google_sub="google-sub-of-the-previous-workspace-account",
        password_hash="$2b$12$fake-bcrypt-hash-set-after-a-reset-round-trip",
        session_epoch=4,
    )
    db = _RoutingSession(row, active_api_tokens=3, unused_reset_tokens=2)

    with pytest.raises(ConflictError) as exc_info:
        await bind_google_identity(db, row.id, "google-sub-of-the-reissued-address")

    assert exc_info.value.error_code == "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT", (
        "a bound row is never silently rebound per spec/feature/AUTH.md "
        "§Google OAuth registration & login"
    )
    assert row.google_sub == "google-sub-of-the-previous-workspace-account", (
        "no row is modified on the refusal per spec/feature/AUTH.md "
        "§Google OAuth registration & login"
    )
    assert row.password_hash == "$2b$12$fake-bcrypt-hash-set-after-a-reset-round-trip"
    assert row.session_epoch == 4
    assert db.api_token_revocations() == []
    assert db.reset_token_deletions() == []
    assert db.flush_count == 0


@pytest.mark.asyncio
async def test_bind_google_identity_unique_collision_raises_linked_elsewhere() -> None:
    """A sub already held by a different row loses the UNIQUE(google_sub) race.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "a bind whose
    incoming `sub` is already held by a different row loses the UNIQUE(google_sub)
    race and fails 409 GOOGLE_ACCOUNT_LINKED_ELSEWHERE".
    spec: spec/feature/AUTH.md §Failure Modes — "The losing bind violates
    UNIQUE(google_sub); its transaction rolls back whole, so that row keeps its
    password, tokens, and session epoch."
    """
    from sqlalchemy.exc import IntegrityError

    from src.backend.auth.users import bind_google_identity

    mock_orig = MagicMock()
    mock_orig.constraint_name = "uq_users_google_sub"
    mock_orig.diag = None
    collision = IntegrityError("statement", {}, mock_orig)

    row = _password_row()
    db = _RoutingSession(
        row, active_api_tokens=3, unused_reset_tokens=2, flush_error=collision
    )

    with pytest.raises(ConflictError) as exc_info:
        await bind_google_identity(db, row.id, "google-sub-already-taken")

    assert exc_info.value.error_code == "GOOGLE_ACCOUNT_LINKED_ELSEWHERE", (
        "a sub already linked to another row must raise "
        "ConflictError('GOOGLE_ACCOUNT_LINKED_ELSEWHERE') per spec/feature/AUTH.md "
        "§Google OAuth registration & login"
    )
    # The rollback is the whole guarantee: "its transaction rolls back whole, so
    # that row keeps its password, tokens, and session epoch". Whether the revoke
    # and delete statements were issued before it is not part of the contract —
    # a rolled-back UPDATE leaves the tokens exactly as a never-issued one does —
    # so nothing about statement ordering is asserted here.
    assert db.rollback_count == 1, (
        "the losing bind's transaction rolls back whole per spec/feature/AUTH.md "
        "§Failure Modes"
    )


@pytest.mark.asyncio
async def test_bind_google_identity_missing_row_raises_entity_not_found() -> None:
    """A user id that names no row raises rather than binding anything.

    Defensive: the callback resolves the row by email immediately beforehand, so
    reaching the lock with nothing there means the row was hard-deleted in
    between.

    spec: spec/feature/AUTH.md §Deletion — "User deletion is hard delete — no
    `deleted_at` column."
    """
    from src.backend.auth.users import bind_google_identity
    from src.shared.exceptions import EntityNotFoundError

    db = _RoutingSession(None, active_api_tokens=3, unused_reset_tokens=2)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await bind_google_identity(db, uuid.uuid4(), "a-sub-with-no-row-to-bind")

    assert exc_info.value.error_code == "USER_NOT_FOUND", (
        f"got {exc_info.value.error_code!r}"
    )
    assert db.api_token_revocations() == [], (
        "a bind that found no row must clear nothing — the credentials it would "
        "otherwise revoke belong to whoever the id names"
    )
    assert db.reset_token_deletions() == []
    assert db.flush_count == 0


# ── unbind_google_identity — admin release ────────────────────────────────────


@pytest.mark.asyncio
async def test_unbind_google_identity_releases_the_binding_and_bumps_the_epoch() -> None:
    """Unbinding clears google_sub, bumps the epoch, and leaves the API tokens alone.

    spec: spec/feature/AUTH.md §Admin unbind — "It clears `google_sub` and
    increments `session_epoch` — unbinding is a credential change, so sessions
    established under the released binding do not survive it"; "It does **not**
    revoke the row's API tokens".
    """
    from src.backend.auth.users import unbind_google_identity

    row = _password_row(
        google_sub="google-sub-of-the-deleted-workspace-account",
        password_hash="$2b$12$fake-bcrypt-hash-set-via-the-reset-round-trip",
        session_epoch=5,
    )
    db = _RoutingSession(row, active_api_tokens=3, unused_reset_tokens=2)

    result = await unbind_google_identity(db, row.id)

    assert result.unbound is True
    assert row.google_sub is None, (
        "the unbind must clear google_sub per spec/feature/AUTH.md §Admin unbind"
    )
    assert row.session_epoch == 6, (
        "unbinding is a credential change, so it increments session_epoch per "
        "spec/feature/AUTH.md §Admin unbind"
    )
    assert row.password_hash == "$2b$12$fake-bcrypt-hash-set-via-the-reset-round-trip", (
        "the unbind leaves the password in place — it is the row's remaining "
        "authentication method per spec/feature/AUTH.md §Admin unbind"
    )
    # The session was primed with 3 revocable tokens; their survival is the point.
    assert db.api_token_revocations() == [], (
        "an unbind must not revoke the row's API tokens per spec/feature/AUTH.md "
        "§Admin unbind"
    )
    assert db.locked_the_user_row(), (
        "the unbind takes the users row lock like the other credential-mutating "
        "paths per spec/feature/AUTH.md §Admin unbind"
    )
    assert db.commit_count == 0, "unbind_google_identity must not commit; the caller does"


@pytest.mark.asyncio
async def test_unbind_google_identity_already_unbound_row_is_a_noop() -> None:
    """An already-unbound row is left untouched — no release, no epoch bump.

    spec: spec/feature/AUTH.md §Admin unbind — "The route is idempotent: an
    already-unbound row is left untouched and still answers 204. There is no
    binding to release, so there is no credential change, and bumping the epoch
    there would sign the user out of every session for nothing".
    """
    from src.backend.auth.users import unbind_google_identity

    row = _password_row(session_epoch=3)  # password-only row, never bound
    db = _RoutingSession(row)

    result = await unbind_google_identity(db, row.id)

    assert result.unbound is False, (
        "an already-unbound row releases nothing per spec/feature/AUTH.md §Admin unbind"
    )
    assert row.session_epoch == 3, (
        "no epoch bump without a credential change per spec/feature/AUTH.md §Admin unbind"
    )
    assert row.google_sub is None
    assert db.flush_count == 0, "the no-op branch must issue no write"


@pytest.mark.asyncio
async def test_unbind_google_identity_refuses_a_password_less_row() -> None:
    """A bound row with no password_hash is refused — clearing it would strand the row.

    This is the normal state of a bound row, not an edge case: the bind nulls
    password_hash, so the row regains one only through PATCH /auth/me or
    POST /auth/password/reset/confirm.

    spec: spec/feature/AUTH.md §Admin unbind — "The route refuses with 409
    GOOGLE_IS_ONLY_AUTH_METHOD when the row has no `password_hash`: clearing
    `google_sub` would violate `ck_users_auth_method` and leave a row nobody can
    authenticate as."
    """
    from src.backend.auth.users import unbind_google_identity

    row = _password_row(
        google_sub="google-sub-of-the-current-holder",
        password_hash=None,
        session_epoch=2,
    )
    db = _RoutingSession(row)

    with pytest.raises(ConflictError) as exc_info:
        await unbind_google_identity(db, row.id)

    assert exc_info.value.error_code == "GOOGLE_IS_ONLY_AUTH_METHOD", (
        "a password-less bound row must be refused ConflictError"
        "('GOOGLE_IS_ONLY_AUTH_METHOD') per spec/feature/AUTH.md §Admin unbind"
    )
    assert row.google_sub == "google-sub-of-the-current-holder", (
        "the refusal happens before any write per spec/feature/AUTH.md §Failure Modes"
    )
    assert row.session_epoch == 2
    assert db.flush_count == 0
