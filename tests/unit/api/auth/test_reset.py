"""Unit tests for src/backend/auth/reset.py.

Concerns covered:
- Unknown email → issue_reset_token no-ops silently (no enumeration leak)
- Known email + SMTP success → row written AFTER send_email (email-first ordering),
  SHA-256 hash stored, 15-min TTL
- Known email + SMTP unconfigured → PeripheralNotConfiguredError re-raised, no DB write
- Known email + SMTP delivery failure → StorageUnavailableError raised, no DB write
- Known email whose session_epoch moved under the row lock → the token row is NOT
  written, and the call still returns normally (no enumeration oracle)
- confirm_reset with invalid/expired/used token → BadRequestError("INVALID_RESET_TOKEN")
- confirm_reset whose token row was deleted by a bind between the first read and the
  post-lock re-read → BadRequestError("INVALID_RESET_TOKEN"), no password write
- Successful confirm writes the new bcrypt hash AND marks used_at

spec: spec/feature/AUTH.md §Lifecycle §Password reset
spec: spec/feature/AUTH.md §Serialization of credential-creating writes
spec: spec/feature/AUTH.md §Failure Modes
spec: spec/API.md §Auth — POST /auth/password/reset/request, POST /auth/password/reset/confirm
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import BadRequestError, StorageUnavailableError


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_UNSET = object()


class _ResetRoutingSession:
    """Query-routing fake AsyncSession for src/backend/auth/reset.py.

    ``execute`` dispatches on the compiled statement rather than on call order
    (spec/TESTING.md §Unit Testing → Mocking rules). Three statements reach it:

    - ``SELECT … users … FOR UPDATE`` — the row lock taken by ``users.lock_user``
    - ``SELECT … users`` — the plain email / id lookup
    - ``SELECT … password_reset_tokens`` — the reset-token read

    Routing by statement is what lets the lock read be modelled faithfully, and
    the fidelity that matters is **object identity**. ``users.lock_user`` runs with
    ``execution_options(populate_existing=True)``, so it returns the *same* ORM
    instance the first read produced, refreshed in place — it does not hand back a
    second object. That is precisely why ``reset.py`` snapshots ``epoch_at_read``
    into a local int before taking the lock: without the snapshot,
    ``locked.session_epoch != user.session_epoch`` compares an attribute against
    itself and is False no matter what the lock read.

    ``epoch_under_lock`` therefore mutates ``self.user`` in place when the
    ``FOR UPDATE`` statement arrives and returns that same instance — the arrangement
    a snapshot-less implementation cannot survive. A two-object model would let such
    an implementation decline correctly and hide the defect.

    ``locked_user`` covers the other lock outcome, where identity is not in play:
    the row is gone and the lock read returns None.

    ``token_reads`` holds successive answers to the *token* statement alone. It is
    ordered on purpose: the code under test issues that one statement twice — once
    to resolve the owner, once again under the lock — and a row that vanishes
    between the two is exactly what the re-read exists to catch
    (spec/feature/AUTH.md §Serialization of credential-creating writes). The last
    entry repeats if more reads occur.
    """

    def __init__(
        self,
        *,
        user: Any = None,
        locked_user: Any = _UNSET,
        epoch_under_lock: int | None = None,
        token_reads: list[Any] | None = None,
    ) -> None:
        self.user = user
        # Default: the lock reads the same state the first read saw — the ordinary,
        # uncontended case.
        self.locked_user = user if locked_user is _UNSET else locked_user
        self.epoch_under_lock = epoch_under_lock
        self.token_reads = list(token_reads) if token_reads else [None]
        self._token_read_count = 0
        self.statements: list[str] = []
        self.added: list[Any] = []
        self.flush_count = 0

    async def execute(self, statement: Any) -> Any:
        sql = str(statement)
        self.statements.append(sql)
        result = MagicMock()
        if "password_reset_tokens" in sql:
            index = min(self._token_read_count, len(self.token_reads) - 1)
            self._token_read_count += 1
            result.scalar_one_or_none.return_value = self.token_reads[index]
            return result
        if "users" in sql:
            if "FOR UPDATE" in sql and self.epoch_under_lock is not None:
                # The refresh a competing bind's commit makes visible: same
                # instance, new epoch — exactly what populate_existing produces.
                self.user.session_epoch = self.epoch_under_lock
                result.scalar_one_or_none.return_value = self.user
                return result
            result.scalar_one_or_none.return_value = (
                self.locked_user if "FOR UPDATE" in sql else self.user
            )
            return result
        raise AssertionError(f"unrouted statement in fake session: {sql}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1

    async def refresh(self, obj: Any) -> None:
        return None

    def took_the_user_row_lock(self) -> bool:
        return any("FOR UPDATE" in s for s in self.statements)

    def token_read_count(self) -> int:
        return self._token_read_count


def _live_user(session_epoch: int = 0) -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "known@example.com"
    user.session_epoch = session_epoch
    return user


def _live_token_row(user_id: uuid.UUID) -> MagicMock:
    row = MagicMock()
    row.used_at = None
    row.expires_at = datetime.now(tz=UTC) + timedelta(minutes=10)
    row.user_id = user_id
    return row


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

    # Notification must not be sent for unknown email
    # per spec/feature/AUTH.md §Lifecycle §Password reset (no enumeration leak)
    mock_notification.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_issue_reset_token_peripheral_not_configured_reraises_no_db_write() -> None:
    """PeripheralNotConfiguredError from send_email propagates; no DB row written.

    The global exception handler maps this to 503 PERIPHERAL_NOT_CONFIGURED
    with detail.peripheral="smtp".  No password_reset_tokens row must be written
    so there is no orphan token in the DB on SMTP misconfiguration.

    spec: spec/feature/AUTH.md §Failure Modes — SMTP peripheral missing →
          503 PERIPHERAL_NOT_CONFIGURED, zero rows in password_reset_tokens.
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

    # Must re-raise PeripheralNotConfiguredError — not swallowed
    with pytest.raises(PeripheralNotConfiguredError) as exc_info:
        await issue_reset_token(mock_db, mock_notification, "exists@example.com")

    assert exc_info.value.detail["peripheral"] == "smtp", (
        "PeripheralNotConfiguredError must carry detail.peripheral='smtp' "
        "per spec/feature/AUTH.md §Failure Modes"
    )

    # No DB write must have occurred
    # db.add must NOT be called when SMTP is not configured
    # per spec/feature/AUTH.md §Failure Modes (zero rows in password_reset_tokens)
    mock_db.add.assert_not_called()
    # db.flush must NOT be called when SMTP is not configured
    # per spec/feature/AUTH.md §Failure Modes
    mock_db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_issue_reset_token_notification_error_raises_storage_unavailable_no_db_write() -> (
    None
):
    """NotificationError from send_email is wrapped as StorageUnavailableError; no DB row written.

    The global exception handler maps StorageUnavailableError to 503 STORAGE_UNAVAILABLE.

    spec/feature/AUTH.md §Failure Modes — SMTP configured but delivery fails
    (transport error, auth rejection, queue full) during password-reset request →
    503 STORAGE_UNAVAILABLE; no DB write.
    """
    from src.backend.auth.reset import issue_reset_token
    from src.shared.exceptions import NotificationError

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
        side_effect=NotificationError("SMTP transport refused connection")
    )

    # Must raise StorageUnavailableError (wrapping the NotificationError)
    with pytest.raises(StorageUnavailableError):
        await issue_reset_token(mock_db, mock_notification, "exists@example.com")

    # No DB write must have occurred
    # db.add must NOT be called when email delivery fails
    # per spec/feature/AUTH.md §Failure Modes (zero rows in password_reset_tokens)
    mock_db.add.assert_not_called()
    # db.flush must NOT be called when email delivery fails
    # per spec/feature/AUTH.md §Failure Modes
    mock_db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_issue_reset_token_known_email_writes_row_with_sha256_hash() -> None:
    """For a known email, issue_reset_token writes a DB row with the SHA-256 hash and 15-min TTL.

    spec: spec/feature/AUTH.md §Security Considerations §Password-reset token storage —
    the table stores the SHA-256 hash, not the raw token; raw token exists only in email body.
    spec: spec/feature/AUTH.md §Lifecycle §Password reset — 15-min TTL.
    """
    import re

    from src.backend.auth.reset import issue_reset_token

    mock_user = _live_user(session_epoch=4)
    # The lock reads the same epoch the request was authorised against, so the
    # re-check passes and the row is written. This is the matching half of the
    # pair whose non-matching half is the epoch-moved test below
    # (spec/TESTING.md §Assertion Discipline — filter tests seed both sides).
    db = _ResetRoutingSession(user=mock_user)

    sent_raw_tokens: list[str] = []

    async def _capture_email(to, subject, body_html):
        match = re.search(r"<strong>([^<]+)</strong>", body_html)
        if match:
            sent_raw_tokens.append(match.group(1))

    mock_notification = AsyncMock()
    mock_notification.send_email = AsyncMock(side_effect=_capture_email)

    await issue_reset_token(db, mock_notification, "known@example.com")

    assert len(db.added) == 1, (
        "One PasswordResetToken row must be written per spec/feature/AUTH.md §Lifecycle §Password "
        "reset"
    )
    assert db.took_the_user_row_lock(), (
        "the token row is inserted under the users row lock per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )
    row = db.added[0]

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


# ── issue_reset_token — the epoch re-check under the row lock ─────────────────


@pytest.mark.asyncio
async def test_issue_reset_token_declines_the_write_when_the_epoch_moved_under_the_lock() -> None:
    """A bind that commits while the request is in flight stops the token row landing.

    The reset token is a live 15-minute password-write capability that no epoch
    governs, so locking around the INSERT alone would not contain it: the bind
    holds the lock, releases it on commit, and the insert would then land *after*
    the bind's delete of unused rows. The epoch comparison is what closes it.

    The email has already gone out — the route sends before it writes — so the
    recipient holds a link that matches no row, and the caller still gets the same
    outcome every address gets.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "POST /auth/password/reset/request | Re-compare `session_epoch` against the
    value read before the token row was prepared; if it has moved, complete
    **without** writing the token row."
    spec: spec/feature/AUTH.md §Failure Modes — "A Google bind commits while
    POST /auth/password/reset/request is in flight | The request declines its
    token INSERT on the epoch re-check ... but the email has already been sent".
    """
    from src.backend.auth.reset import issue_reset_token

    user = _live_user(session_epoch=4)
    # The lock refreshes the *same* instance in place — that is what
    # populate_existing does — so only the local snapshot taken before the lock
    # still holds the pre-bind value. An implementation that compared
    # locked.session_epoch against user.session_epoch would compare 5 to 5 here
    # and write the row, which is the defect this arrangement exists to catch.
    db = _ResetRoutingSession(user=user, epoch_under_lock=5)

    mock_notification = AsyncMock()

    # Completes normally: the route reports the same outcome for every address and
    # must not become an oracle for account state.
    await issue_reset_token(db, mock_notification, "known@example.com")

    # Backstop: the request really reached the write stage — the address resolved
    # and the email went out — so the absent row below is the decline, not an
    # earlier bail-out.
    mock_notification.send_email.assert_awaited_once()
    assert db.took_the_user_row_lock(), (
        "the re-check happens under the users row lock per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )

    assert db.added == [], (
        "a request whose read predates the bind must complete without writing the "
        "token row per spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    assert db.flush_count == 0, "no INSERT is flushed on the declined path"


@pytest.mark.asyncio
async def test_issue_reset_token_declines_the_write_when_the_owner_row_is_gone() -> None:
    """A row hard-deleted between the lookup and the lock gets no token row either.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes — the
    write re-reads "the state that authorised it" under the lock.
    spec: spec/feature/AUTH.md §Deletion — "User deletion is hard delete".
    """
    from src.backend.auth.reset import issue_reset_token

    db = _ResetRoutingSession(user=_live_user(session_epoch=0), locked_user=None)
    mock_notification = AsyncMock()

    await issue_reset_token(db, mock_notification, "known@example.com")

    mock_notification.send_email.assert_awaited_once()
    assert db.took_the_user_row_lock()
    assert db.added == [], (
        "no reset token may be minted against a row that no longer exists per "
        "spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    assert db.flush_count == 0


# ── confirm_reset ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_reset_invalid_token_raises_bad_request() -> None:
    """confirm_reset raises BadRequestError('INVALID_RESET_TOKEN') for unknown token.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset —
    confirm_reset validates token (matches a row, not expired, used_at is null).
    """
    from src.backend.auth import users as _users
    from src.backend.auth.reset import confirm_reset

    db = _ResetRoutingSession(token_reads=[None])  # token not found

    with patch.object(_users, "update_password") as mock_update_password:
        with pytest.raises(BadRequestError) as exc_info:
            await confirm_reset(db, "nonexistent-raw-token", "newpassword123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN", (
        "Unknown reset token must raise BadRequestError('INVALID_RESET_TOKEN') "
        "per spec/feature/AUTH.md §Lifecycle §Password reset"
    )
    mock_update_password.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_reset_expired_token_raises_bad_request() -> None:
    """confirm_reset raises BadRequestError('INVALID_RESET_TOKEN') for expired token.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — not expired condition.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.reset import confirm_reset

    mock_row = _live_token_row(uuid.uuid4())
    mock_row.expires_at = datetime.now(tz=UTC) - timedelta(minutes=1)  # expired

    db = _ResetRoutingSession(token_reads=[mock_row])

    with patch.object(_users, "update_password") as mock_update_password:
        with pytest.raises(BadRequestError) as exc_info:
            await confirm_reset(db, "expired-token", "newpassword123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN"
    mock_update_password.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_reset_used_token_raises_bad_request() -> None:
    """confirm_reset raises BadRequestError('INVALID_RESET_TOKEN') for already-used token.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset — single-use: used_at is null check.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.reset import confirm_reset

    mock_row = _live_token_row(uuid.uuid4())
    mock_row.used_at = datetime.now(tz=UTC) - timedelta(minutes=5)  # already used

    db = _ResetRoutingSession(token_reads=[mock_row])

    with patch.object(_users, "update_password") as mock_update_password:
        with pytest.raises(BadRequestError) as exc_info:
            await confirm_reset(db, "already-used-token", "newpassword123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN"
    mock_update_password.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_reset_valid_token_writes_new_hash_and_marks_used() -> None:
    """confirm_reset writes new bcrypt hash AND marks used_at on success.

    spec: spec/feature/AUTH.md §Lifecycle §Password reset —
    writes the new bcrypt hash and marks the token used (so it cannot be replayed).
    """
    from src.backend.auth import users as _users
    from src.backend.auth.reset import confirm_reset

    owner = _live_user()
    mock_row = _live_token_row(owner.id)

    # The post-lock re-read finds the same live row — nothing superseded this
    # request. The non-matching half of the pair is the deleted-under-the-lock
    # test below (spec/TESTING.md §Assertion Discipline — seed both sides).
    db = _ResetRoutingSession(user=owner, token_reads=[mock_row, mock_row])

    mock_user = MagicMock()
    mock_user.id = owner.id
    mock_user.password_hash = "old_hash"

    new_hash_captured: list[str] = []

    async def _fake_update_password(session, uid, pw):
        mock_user.password_hash = f"bcrypt_of_{pw}"
        new_hash_captured.append(mock_user.password_hash)
        return mock_user

    with patch.object(_users, "update_password", side_effect=_fake_update_password):
        await confirm_reset(db, "valid-raw-token", "new-secure-password123")

    # Password hash must have been updated
    assert len(new_hash_captured) == 1, (
        "update_password must be called exactly once per spec/feature/AUTH.md §Lifecycle §Password "
        "reset"
    )

    # used_at must be set (token marked as used)
    assert mock_row.used_at is not None, (
        "used_at must be set after successful confirm_reset to prevent token replay "
        "per spec/feature/AUTH.md §Lifecycle §Password reset"
    )

    assert db.took_the_user_row_lock(), (
        "the password write runs under the users row lock per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )

    # flush must have been called to persist both the password update and the used_at mark
    assert db.flush_count >= 1


@pytest.mark.asyncio
async def test_confirm_reset_declines_when_the_bind_deleted_the_token_under_the_lock() -> None:
    """A token the bind swept between the first read and the re-read writes no password.

    The confirm was authorised before the bind committed; by the time it holds the
    lock, the bind's delete of unused reset rows has already removed the row it
    was authorised by. It must resolve as the route's ordinary invalid-token
    failure rather than setting a password on a row that has changed hands.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "POST /auth/password/reset/confirm | Re-read the `password_reset_tokens` row,
    which the bind's delete has already removed; missing or used → the route's
    existing invalid-token failure."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.reset import confirm_reset

    owner = _live_user()
    live_row = _live_token_row(owner.id)
    # First read: the row is live and valid. Second read (under the lock): gone.
    db = _ResetRoutingSession(user=owner, token_reads=[live_row, None])

    with patch.object(_users, "update_password") as mock_update_password:
        with pytest.raises(BadRequestError) as exc_info:
            await confirm_reset(db, "swept-by-the-bind", "new-secure-password123")

    assert exc_info.value.error_code == "INVALID_RESET_TOKEN", (
        "a token removed by the bind's delete must fail as an invalid token per "
        "spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    # Backstop: the first read succeeded and the flow reached the lock, so the
    # failure is the re-read rather than the ordinary first-read rejection.
    assert db.took_the_user_row_lock(), (
        "the re-read happens under the users row lock per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )
    # ``>=`` rather than ``==``: what matters is that the token was consulted again
    # after the lock, not how many statements a given implementation spends doing
    # it — a re-read folded into a single joined SELECT ... FOR UPDATE would still
    # honour the contract.
    assert db.token_read_count() >= 2, (
        "the token row must be consulted again once the lock is held; "
        f"got {db.token_read_count()} read(s)"
    )
    mock_update_password.assert_not_called()
    assert live_row.used_at is None, (
        "a declined confirm must not consume the token either"
    )
