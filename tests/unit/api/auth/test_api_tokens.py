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
- list_page (the query behind both admin token reads): the token hash is never in the
  SELECT list; include_revoked / user_id filter predicates; the COUNT carries the same
  predicates and no page window; NULLS LAST + id tiebreak on every sort input
- revoke reports the token's owner and whether this call was the one that revoked it

spec: spec/feature/AUTH.md §API Tokens
spec: spec/API.md §Authentication Mechanisms
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.conftest import compiled_sql, route_db_execute

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
    route_db_execute(mock_db, [("count(", count_result)], default=user_result)
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    # No session patching here: ``mint`` writes through the caller's session only. The
    # ``last_used_at`` stamp that needs a session of its own belongs to
    # ``lookup_and_validate``, not to minting.
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
    route_db_execute(mock_db, [("count(", count_result)], default=user_result)
    mock_db.add = MagicMock(side_effect=_capture_add)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

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
    route_db_execute(mock_db, [("count(", count_result)], default=user_result)

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

    # ``lookup_and_validate`` stamps ``last_used_at`` on a session of its own, so that
    # session is stubbed here to keep this test off a real database. The patch lands on
    # the module-level ``SessionLocal`` rather than on the seam that selects it: this test
    # hands in an ``AsyncMock`` whose ``bind`` is not an engine, which is the no-usable-bind
    # case, and the module-level factory is then the only address the write can resolve to
    # (spec/feature/BACKEND.md §Shared Services, PostgreSQL row — "A session with no usable
    # bind falls back to the module-level factory, the only address available in that
    # case."). Stubbing the selector instead would suppress the engine choice entirely;
    # patching the destination leaves the real selection running.
    #
    # No test using a mock ``db`` can *observe* which engine is selected — every branch
    # converges on the module-level factory — so which database the stamp lands in is
    # asserted separately, in
    # ``test_the_last_used_at_stamp_is_written_to_the_callers_database`` below.
    mock_throttle_session = AsyncMock()
    mock_throttle_session.execute = AsyncMock()
    mock_throttle_session.commit = AsyncMock()
    mock_throttle_session.__aenter__ = AsyncMock(return_value=mock_throttle_session)
    mock_throttle_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.shared.db.session.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, effective_role, token_id = await lookup_and_validate(
            mock_db, "dsk_admin_snapshot_reader_current"
        )

    assert effective_role == "Reader", (
        "min(Admin, Reader) must be Reader — demoting a user immediately downgrades all "
        "their existing tokens per spec/feature/AUTH.md "
        "§API Tokens §Effective privilege — intersection"
    )
    assert user is mock_user
    assert token_id == mock_token.id, (
        "The authenticating api_tokens row id must be returned so a credential-creating "
        "write can re-read it under the users row lock per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )


@pytest.mark.asyncio
async def test_intersection_snapshot_reader_current_admin_returns_reader() -> None:
    """effective_role = min(snapshot=Reader, current=Admin) → Reader via lookup_and_validate.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection: promoting a user
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

    with patch("src.shared.db.session.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, effective_role, token_id = await lookup_and_validate(
            mock_db, "dsk_reader_snapshot_admin_current"
        )

    assert effective_role == "Reader", (
        "min(Reader, Admin) must be Reader — promoting a user does not auto-elevate "
        "existing tokens per spec/feature/AUTH.md §API Tokens §Effective privilege — intersection"
    )
    assert user is mock_user
    assert token_id == mock_token.id


@pytest.mark.asyncio
async def test_intersection_equal_roles_preserved() -> None:
    """effective_role = min(Editor, Editor) → Editor via lookup_and_validate.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection: equal snapshot and
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

    with patch("src.shared.db.session.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, effective_role, token_id = await lookup_and_validate(
            mock_db, "dsk_editor_snapshot_editor_current"
        )

    assert effective_role == "Editor", (
        "min(Editor, Editor) must be Editor per spec/feature/AUTH.md "
        "§API Tokens §Effective privilege — intersection"
    )
    assert user is mock_user
    assert token_id == mock_token.id


# ── lookup_and_validate — error cases ────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_and_validate_unknown_token_raises_invalid() -> None:
    """lookup_and_validate raises AuthenticationError('INVALID_API_TOKEN') for unknown token.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection:
    invalid → 401 INVALID_API_TOKEN.
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

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection:
    revoked → 401 TOKEN_REVOKED.
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
        "per spec/feature/AUTH.md §API Tokens §Effective privilege — intersection"
    )


@pytest.mark.asyncio
async def test_lookup_and_validate_expired_token_raises_token_expired() -> None:
    """lookup_and_validate raises AuthenticationError('TOKEN_EXPIRED') for expired token.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection:
    expired → 401 TOKEN_EXPIRED.
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
        "per spec/feature/AUTH.md §API Tokens §Effective privilege — intersection"
    )


# ── last_used_at throttle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_and_validate_updates_last_used_at_when_stale() -> None:
    """lookup_and_validate triggers the throttled UPDATE when last_used_at is None (stale).

    spec: spec/feature/AUTH.md §API Tokens §Audit and last_used_at —
    every successful authentication updates last_used_at; update throttled to
    per-minute granularity.
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

    with patch("src.shared.db.session.SessionLocal") as mock_session_cls:
        mock_session_cls.return_value = mock_throttle_session
        user, role, token_id = await lookup_and_validate(mock_db, "dsk_test_token_stale")

    assert throttle_execute_called, (
        "The throttled UPDATE for last_used_at must be called when last_used_at is None "
        "per spec/feature/AUTH.md §API Tokens §Audit and last_used_at"
    )
    assert user is mock_user
    assert role == "Reader"
    assert token_id == mock_token.id


@pytest.mark.asyncio
async def test_the_last_used_at_stamp_is_written_to_the_callers_database() -> None:
    """The throttled UPDATE runs against the engine the caller's session is bound to.

    The stamp is written on a session of its own — the authenticating request commonly
    belongs to a read-only GET that never commits, so riding the caller's transaction
    would drop it. That independence is satisfiable by a session on *any* database,
    including one nobody in this call is reading: a module-level factory is bound at
    import time to the app-runtime connection settings, which a caller holding a session
    built somewhere else does not share. A stamp written there is written nowhere as far
    as ``GET /auth/api-tokens`` is concerned, and nothing in this function's return value
    or in the tests above would notice — the write is fire-and-forget.

    So both halves are held at once: a session distinct from the caller's, on the
    database the caller handed in. A distinct engine is installed as the module-level
    ``SessionLocal`` for the duration, so "the caller's engine" is a discriminating
    reading rather than the only engine in the process.

    ``AsyncSession.execute``/``commit`` are stubbed at the class so the statements are
    observed without a connection ever being opened; the caller's ``db`` is a mock and is
    unaffected. Only ``Update`` statements are counted as stamps: the throttle lives in the
    UPDATE's own ``WHERE`` clause, so the stamp *is* the UPDATE and nothing else the
    stamping session may issue is one. Filtering by statement type rather than counting
    every statement keeps this assertion off the shape of the session's other traffic.
    Engines are never connected to: SQLAlchemy defers connection until a statement runs,
    and none does.

    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — "A write that must
        commit on its own terms while the caller holds a session of its own — one that has
        to survive a rollback the caller is about to take, or land on a read-only request
        that never commits — opens a session from a factory built on the **bind of the
        injected session**, so it reaches the database the caller is actually using."
    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — the module-level
        factory is bound at import time to values "which an in-process caller carrying a
        session on another engine does not have; the write would otherwise be aimed at a
        different address than every other statement in the same call, with no diagnostic
        distinguishing that from success" — hence the distinct fallback engine below, and
        hence this assertion existing at all.
    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "Every successful API-token
        authentication updates ``api_tokens.last_used_at``", i.e. the row of the token
        that just authenticated, which lives in the database the caller is querying.
    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "The update is throttled to
        per-minute granularity — the authentication path issues the ``UPDATE`` with a
        ``WHERE`` clause that makes it a no-op below 60s" — the throttle rides the UPDATE,
        which is why one UPDATE per authentication is the spec'd shape.
    spec: spec/feature/AUTH.md §Lifecycle endpoints — ``GET /auth/api-tokens`` returns
        ``last_used_at`` to the user; a stamp on another database never reaches that read.
    """
    from sqlalchemy import Update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.backend.auth.api_tokens import lookup_and_validate

    callers_engine = create_async_engine("postgresql+asyncpg://u:p@callers-db:5432/d")
    fallback_engine = create_async_engine("postgresql+asyncpg://u:p@module-level-db:5432/d")

    mock_token = MagicMock()
    mock_token.revoked_at = None
    mock_token.expires_at = None
    mock_token.role_snapshot = "Reader"
    mock_token.id = uuid.uuid4()
    mock_token.last_used_at = None

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.role = "Reader"

    mock_result = MagicMock()
    mock_result.first.return_value = (mock_token, mock_user)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.bind = callers_engine  # the caller's session carries its own engine

    executed: list[tuple[object, object]] = []  # (session, statement)
    committed: list[object] = []

    async def _record_execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        executed.append((self, statement))
        return MagicMock()

    async def _record_commit(self) -> None:  # type: ignore[no-untyped-def]
        committed.append(self)

    try:
        with (
            patch.object(AsyncSession, "execute", _record_execute),
            patch.object(AsyncSession, "commit", _record_commit),
            patch(
                "src.shared.db.session.SessionLocal",
                async_sessionmaker(fallback_engine, class_=AsyncSession, expire_on_commit=False),
            ),
        ):
            await lookup_and_validate(mock_db, "dsk_stamp_goes_to_the_callers_database")
    finally:
        await callers_engine.dispose()
        await fallback_engine.dispose()

    stamps = [(session, stmt) for session, stmt in executed if isinstance(stmt, Update)]

    # Backstop: the UPDATE really was issued. Without it every assertion below passes on
    # an implementation that stopped stamping altogether — which nothing else observes,
    # since the caller never reads the stamp back.
    assert len(stamps) == 1, (
        f"a successful authentication must issue exactly one last_used_at UPDATE; got "
        f"{len(stamps)} (all statements: {[type(s).__name__ for _, s in executed]!r}). "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'Every successful API-token "
        "authentication updates api_tokens.last_used_at.'"
    )
    stamp_session, _stamp_stmt = stamps[0]

    assert stamp_session.bind is callers_engine, (
        f"the last_used_at stamp must be written against the engine the caller's session "
        f"is bound to ({callers_engine.url.host}), so it lands on the row "
        f"GET /auth/api-tokens reads back; it went to "
        f"{getattr(stamp_session.bind, 'url', stamp_session.bind)}. A write aimed at the "
        f"module-level factory's engine ({fallback_engine.url.host}) is lost with no "
        "diagnostic distinguishing it from success. spec: spec/feature/BACKEND.md "
        "§Shared Services (PostgreSQL row) — the factory is built on the bind of the "
        "injected session."
    )

    # The stamp is worthless uncommitted: the authenticating request is commonly a
    # read-only GET whose own session never commits, so nothing else will flush it.
    assert committed == [stamp_session], (
        f"the stamping session must commit on its own terms, or the UPDATE is discarded "
        f"when the session closes; sessions that committed: {committed!r}. "
        "spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — a write that "
        "must 'land on a read-only request that never commits' commits on its own terms."
    )

    # Shape, not statement count: the caller's session must carry no stamp at all. Counting
    # its statements instead would pin how the lookup is issued — splitting the join into
    # two selects is behaviour-neutral and must stay free.
    caller_stamps = [
        call for call in mock_db.execute.await_args_list if isinstance(call.args[0], Update)
    ]
    assert caller_stamps == [], (
        f"the caller's session must never carry the stamp — it belongs to a session of its "
        f"own, so writing it on both is not 'a session of its own'; the caller's session ran "
        f"{len(caller_stamps)} UPDATE(s). spec: spec/feature/BACKEND.md "
        "§Shared Services (PostgreSQL row)."
    )


# ── last_used_at stamp is best-effort ─────────────────────────────────────────
#
# The stamp is an audit side effect of an authentication that has already succeeded. A
# failure to write it must not reach the caller, and — because nothing reads the column
# in band — the log record is the only trace that it was lost.


class _StampSession:
    """Stand-in for the independent stamping session; fails at the requested point.

    ``at`` is ``"execute"`` (the UPDATE itself faults — a connection lost mid-statement)
    or ``"commit"`` (the write cannot land), or ``None`` for a session that works.
    """

    def __init__(self, failure: BaseException | None = None, at: str | None = None) -> None:
        self._failure = failure
        self._at = at
        self.statements: list[object] = []
        self.commits = 0

    async def __aenter__(self) -> _StampSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: object, *args: object, **kwargs: object) -> object:
        if self._at == "execute":
            assert self._failure is not None
            raise self._failure
        self.statements.append(statement)
        return MagicMock()

    async def commit(self) -> None:
        if self._at == "commit":
            assert self._failure is not None
            raise self._failure
        self.commits += 1


def _valid_token_and_user() -> tuple[MagicMock, MagicMock]:
    """A token that passes every validation check, and its owner.

    ``spec=`` on both so a renamed or mistyped ORM field fails loud instead of answering
    with a fresh auto-mock (spec/TESTING.md §Unit Testing → Mocking rules).
    """
    from src.shared.db.models import ApiToken, User

    token = MagicMock(spec=ApiToken)
    token.revoked_at = None
    token.expires_at = None
    token.role_snapshot = "Editor"
    token.id = uuid.uuid4()
    token.last_used_at = None

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role = "Editor"
    return token, user


def _db_returning(token: object, user: object) -> AsyncMock:
    """A caller session whose one query answers with ``(token, user)``.

    Safe to give ``spec=AsyncSession`` even though ``bind`` is an instance attribute
    absent from the class: every consumer of this helper patches
    ``independent_sessionmaker``, so nothing reads the bind.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    result = MagicMock()
    result.first.return_value = (token, user)
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock(return_value=result)
    return db


# ── The throttle window ───────────────────────────────────────────────────────


def _throttle_window_seconds(stmt: object) -> float:
    """Return the length in seconds of the interval *stmt*'s WHERE clause subtracts from now().

    The window is read out of the statement's clause tree rather than out of the source
    line, so an argument landing in the wrong slot of a seven-positional-argument SQL
    function is observable. Two spellings are accepted — a Postgres
    ``make_interval(years, months, weeks, days, hours, mins, secs)`` call and a bound
    ``timedelta`` — because the spec fixes the *window*, not the expression the
    implementation builds it with; a refactor between those two forms is behaviour-neutral
    and must stay free.

    Month and year slots are converted at 30 and 365 days. Nothing depends on the exact
    conversion: it exists only so a value that lands in one of those slots reads as a
    number of seconds far from the spec'd window rather than as the window itself.
    """
    from datetime import timedelta

    from sqlalchemy.sql.elements import BindParameter
    from sqlalchemy.sql.functions import Function

    slot_seconds = (365 * 86400, 30 * 86400, 7 * 86400, 86400, 3600, 60, 1)

    def _walk(element: object) -> Iterator[object]:
        yield element
        children = getattr(element, "get_children", None)
        if children is None:
            return
        for child in children():
            yield from _walk(child)

    windows: list[float] = []
    for element in _walk(stmt):
        if isinstance(element, Function) and element.name == "make_interval":
            args = [getattr(clause, "value", None) for clause in element.clauses]
            assert len(args) == len(slot_seconds), (
                f"make_interval takes {len(slot_seconds)} positional arguments "
                f"(years, months, weeks, days, hours, mins, secs); the statement passes "
                f"{len(args)}: {args!r}"
            )
            windows.append(sum(a * s for a, s in zip(args, slot_seconds, strict=True)))
        elif isinstance(element, BindParameter) and isinstance(element.value, timedelta):
            windows.append(element.value.total_seconds())

    assert len(windows) == 1, (
        f"the throttle WHERE clause must subtract exactly one interval from now(); found "
        f"{len(windows)} ({windows!r}) in {stmt!r}. If the implementation now spells the "
        f"window some third way, teach this reader that spelling — do not delete the "
        f"assertion, or the window stops being checked anywhere."
    )
    return windows[0]


@pytest.mark.asyncio
async def test_the_throttle_holds_the_stamp_off_for_sixty_seconds() -> None:
    """The interval the WHERE clause subtracts from ``now()`` is 60 seconds.

    Nothing else observes this number. The predicate is Postgres SQL, so no unit test
    connects an engine to it; a window that is too large produces no error at all — the
    first authentication matches the ``last_used_at IS NULL`` leg and stamps, every later
    one evaluates the comparison as false and quietly declines — so an integration test
    that authenticates twice cannot see it either. Both of its assertions hold identically
    under a 60-second window and a 60-*year* one, which is exactly the mistake a
    seven-positional-argument function invites. This test is the only place the number
    lives.

    The value asserted is the spec's, not the module constant's: reading
    ``_LAST_USED_THROTTLE_SECONDS`` back out of the module would agree with itself no
    matter what it held.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "The update is throttled to
        per-minute granularity — the authentication path issues the ``UPDATE`` with a
        ``WHERE`` clause that makes it a no-op below 60s — so a high-frequency client
        doesn't flood the DB."
    spec: spec/feature/BACKEND_SCHEMA.md §``api_tokens`` — the stamp is "throttled to
        per-minute granularity to avoid DB pressure".
    """
    from sqlalchemy import Update

    from src.backend.auth.api_tokens import lookup_and_validate

    token, user = _valid_token_and_user()
    stamp_session = _StampSession()

    with patch(
        "src.backend.auth.api_tokens.independent_sessionmaker",
        MagicMock(return_value=lambda: stamp_session),
    ):
        await lookup_and_validate(_db_returning(token, user), "dsk_throttle_window")

    stamps = [s for s in stamp_session.statements if isinstance(s, Update)]
    # Backstop: the UPDATE this test reads the window out of really was issued, so the
    # assertion below cannot pass on an implementation that stopped stamping.
    assert len(stamps) == 1, (
        f"a successful authentication must issue exactly one last_used_at UPDATE to read "
        f"the throttle window out of; got {len(stamps)}. spec: spec/feature/AUTH.md "
        "§Audit and last_used_at."
    )

    window = _throttle_window_seconds(stamps[0])
    assert window == 60, (
        f"the throttle must make the UPDATE a no-op below 60s; the WHERE clause subtracts "
        f"{window}s from now(). A window in the wrong unit is "
        f"silent at every other tier: too large freezes last_used_at at its first value "
        f"forever, too small removes the DB-pressure guard the throttle exists for. "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'a WHERE clause that makes "
        "it a no-op below 60s'."
    )


# ── The stamp's target row, predicate direction, and SET clause ───────────────
#
# The window reader above answers one question about the UPDATE — how long the interval
# is. The rest of the statement carries just as much spec, and none of it is observable
# anywhere else: the caller never reads the stamp back, and an integration test that
# authenticates with its own token sees a correct stamp on that token whether or not the
# statement also stamped every other row in the table, inverted its comparison, or wrote
# the wrong column. So the whole statement is read here, the same way — out of the clause
# tree, by meaning rather than by spelling.


_STAMP_NOW = datetime(2031, 7, 4, 12, 0, 0, tzinfo=UTC)
"""The instant ``now()`` is taken to return while the stamp's clause tree is evaluated."""


def _sql_function_value(fn: Any) -> Any:
    """Evaluate a SQL function call appearing in the stamp statement.

    Two spellings of the throttle interval are accepted for the same reason
    ``_throttle_window_seconds`` accepts two — the spec fixes the window, not the
    expression that builds it — and any third spelling raises rather than being skipped.
    """
    if fn.name in {"now", "current_timestamp", "statement_timestamp", "transaction_timestamp"}:
        return _STAMP_NOW
    if fn.name == "make_interval":
        slot_seconds = (365 * 86400, 30 * 86400, 7 * 86400, 86400, 3600, 60, 1)
        args = [getattr(clause, "value", None) for clause in fn.clauses]
        assert len(args) == len(slot_seconds), (
            f"make_interval takes {len(slot_seconds)} positional arguments (years, months, "
            f"weeks, days, hours, mins, secs); the statement passes {len(args)}: {args!r}"
        )
        return timedelta(seconds=sum(a * s for a, s in zip(args, slot_seconds, strict=True)))
    raise AssertionError(
        f"the stamp statement calls SQL function {fn.name!r}, which this reader cannot "
        f"evaluate. Teach it that spelling — do not delete the assertions that depend on "
        f"it, or the predicate stops being checked anywhere."
    )


def _sql_eval(element: Any, row: dict[str, Any]) -> Any:
    """Evaluate SQL expression *element* against *row*, in SQL's three-valued logic.

    ``None`` is SQL ``NULL`` — as a scalar and as the UNKNOWN truth value, which is what
    SQL itself does, so a ``WHERE`` clause selects a row only when this returns ``True``
    (``NULL`` and ``False`` both mean "not this row"). Comparison and arithmetic operators
    are applied by calling the operator SQLAlchemy stored on the node, so a predicate
    respelled with the operands swapped, or with the conjunction restructured, evaluates
    the same — this reads meaning, not spelling.

    Any node the reader does not recognise raises. A clause silently skipped would leave
    the caller asserting on whatever predicate remained.
    """
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.elements import (
        BinaryExpression,
        BindParameter,
        BooleanClauseList,
        ColumnClause,
        False_,
        Grouping,
        Null,
        True_,
        UnaryExpression,
    )
    from sqlalchemy.sql.functions import Function

    if isinstance(element, Grouping):
        return _sql_eval(element.element, row)
    if isinstance(element, Null):
        return None
    if isinstance(element, True_):
        return True
    if isinstance(element, False_):
        return False
    if isinstance(element, BindParameter):
        return element.value
    if isinstance(element, Function):
        return _sql_function_value(element)
    if isinstance(element, ColumnClause):
        table_name = getattr(getattr(element, "table", None), "name", None)
        assert table_name == "api_tokens", (
            f"the stamp predicate reads column {element.name!r} of table {table_name!r}; "
            f"the UPDATE targets api_tokens alone, so no other table's column belongs in it."
        )
        assert element.name in row, (
            f"the stamp predicate reads api_tokens.{element.name}, which this test does not "
            f"model (it seeds {sorted(row)!r}). Extend the seeded row so the new column is "
            f"actually exercised rather than assumed."
        )
        return row[element.name]
    if isinstance(element, BooleanClauseList):
        parts = [_sql_eval(clause, row) for clause in element.clauses]
        if element.operator is operators.and_:
            if any(p is False for p in parts):
                return False
            return None if any(p is None for p in parts) else True
        if element.operator is operators.or_:
            if any(p is True for p in parts):
                return True
            return None if any(p is None for p in parts) else False
        raise AssertionError(f"unsupported boolean connective {element.operator!r} in the stamp")
    if isinstance(element, UnaryExpression) and element.operator is operators.inv:
        inner = _sql_eval(element.element, row)
        return None if inner is None else not inner
    if isinstance(element, BinaryExpression):
        left = _sql_eval(element.left, row)
        right = _sql_eval(element.right, row)
        op = element.operator
        # ``IS`` / ``IS NOT`` are the two operators that are never UNKNOWN: they compare
        # NULL as a value rather than propagating it.
        same = (left is None) == (right is None) and (left is None or left == right)
        if op is operators.is_:
            return same
        if op is operators.is_not:
            return not same
        if left is None or right is None:
            return None  # every other operator propagates NULL
        comparisons = {operators.eq, operators.ne, operators.lt, operators.le}
        comparisons |= {operators.gt, operators.ge}
        if op in comparisons:
            return bool(op(left, right))
        if op in {operators.add, operators.sub}:
            return op(left, right)
        raise AssertionError(f"unsupported operator {op!r} in the stamp predicate")
    raise AssertionError(
        f"the stamp statement contains {type(element).__name__}, which this reader cannot "
        f"evaluate: {element!r}. Teach it that node — skipping it would leave the caller "
        f"asserting on whatever predicate remained."
    )


def _stamp_matches(stmt: Any, row: dict[str, Any]) -> bool:
    """True when *stmt*'s WHERE clause selects *row* — i.e. that row would be stamped."""
    assert stmt.whereclause is not None, (
        "the last_used_at UPDATE must carry a WHERE clause: an unqualified UPDATE stamps "
        "every row in api_tokens. spec: spec/feature/AUTH.md §Audit and last_used_at."
    )
    return _sql_eval(stmt.whereclause, row) is True


def _stamp_set_clause(stmt: Any) -> dict[str, Any]:
    """Return the UPDATE's SET clause as ``{column name: evaluated value}``.

    ``_values`` is the accessor SQLAlchemy 2.0 offers for an ``Update``'s assignment map;
    compiling to SQL instead would make the assertion read the dialect's spelling.
    """
    assert stmt.table.name == "api_tokens", (
        f"the stamp must update api_tokens; it targets {stmt.table.name!r}. spec: "
        "spec/feature/BACKEND_SCHEMA.md §api_tokens — last_used_at lives on that table."
    )
    return {column.name: _sql_eval(value, {}) for column, value in dict(stmt._values).items()}


async def _authenticate_and_capture_the_stamp() -> tuple[Any, Any]:
    """Authenticate once with a valid token; return ``(the last_used_at UPDATE, that token)``.

    The ``len(stamps) == 1`` check is the backstop every caller relies on: without it a
    reader asserting on "the stamp" would have nothing to read, and callers would fail on
    an IndexError rather than on the spec'd claim that an authentication stamps.
    """
    from sqlalchemy import Update

    from src.backend.auth.api_tokens import lookup_and_validate

    token, user = _valid_token_and_user()
    stamp_session = _StampSession()

    with patch(
        "src.backend.auth.api_tokens.independent_sessionmaker",
        MagicMock(return_value=lambda: stamp_session),
    ):
        await lookup_and_validate(_db_returning(token, user), "dsk_read_the_stamp_statement")

    stamps = [s for s in stamp_session.statements if isinstance(s, Update)]
    assert len(stamps) == 1, (
        f"a successful authentication must issue exactly one last_used_at UPDATE to read "
        f"the statement out of; got {len(stamps)} (issued {stamp_session.statements!r}). "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'Every successful API-token "
        "authentication updates api_tokens.last_used_at.'"
    )
    return stamps[0], token


@pytest.mark.asyncio
async def test_the_stamp_lands_on_the_authenticating_row_and_on_no_other() -> None:
    """The WHERE clause selects the token that just authenticated — not every token.

    §Audit stamps the row of the token that just authenticated; a WHERE clause that dropped
    the identity leg would stamp every row in ``api_tokens`` on every PAT authentication,
    rewriting every other user's audit column with this caller's clock. Nothing observes
    that. The caller never reads the stamp back, and an integration test authenticating
    with its own token sees exactly the correct value on exactly the row it inspects — its
    own — whether or not every other row was stamped alongside it. So both sides are seeded
    here, per spec/TESTING.md §Assertion Discipline ("a test of a filter, query predicate,
    or matching rule must seed both rows that match and rows that do not"): the
    authenticating row, and a stranger's row identical in every other respect.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "Every successful API-token
        authentication updates ``api_tokens.last_used_at``", i.e. the row of the token that
        authenticated; nothing in §Audit reaches any other token's row.
    spec: spec/feature/BACKEND_SCHEMA.md §``api_tokens`` — ``last_used_at`` is "Null until
        first use", which stops being true of every unused token the moment a stranger's
        authentication stamps it.
    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "``last_used_at`` is for human
        inspection"; an inspector reading a stamp on a token nobody used is reading a
        fabrication.
    """
    stmt, token = await _authenticate_and_capture_the_stamp()

    a_strangers_token_id = uuid.uuid4()
    assert a_strangers_token_id != token.id  # sanity: two distinct rows are being compared

    assert _stamp_matches(stmt, {"id": token.id, "last_used_at": None}), (
        "the row of the token that just authenticated must be selected by the stamp's "
        "WHERE clause, or no authentication ever stamps anything. spec: "
        "spec/feature/AUTH.md §Audit and last_used_at."
    )
    assert not _stamp_matches(stmt, {"id": a_strangers_token_id, "last_used_at": None}), (
        "an unrelated token's row must NOT be selected: the stamp records that *this* token "
        "was used, and a WHERE clause without the identity leg rewrites every user's "
        "last_used_at on every PAT authentication — silently, since nothing reads the "
        "column in band. spec: spec/feature/BACKEND_SCHEMA.md §api_tokens — last_used_at is "
        "'Null until first use'."
    )
    assert not _stamp_matches(
        stmt, {"id": a_strangers_token_id, "last_used_at": _STAMP_NOW - timedelta(days=30)}
    ), (
        "a stranger's long-stale row must not be selected either — staleness is only ever "
        "asked about the authenticating row. spec: spec/feature/AUTH.md §Audit and "
        "last_used_at."
    )


@pytest.mark.asyncio
async def test_the_stamp_selects_never_used_and_stale_rows_and_declines_fresh_ones() -> None:
    """First use stamps; a row stamped inside the window does not; a stale row does again.

    This is the throttle's actual contract, evaluated rather than inspected. Three
    independent mistakes produce a statement that still compiles, still runs, and still
    satisfies every other assertion in this file: inverting the comparison (stamp only rows
    *newer* than the cutoff), dropping or negating the ``IS NULL`` leg (a token's first use
    never stamps, so ``last_used_at`` stays NULL forever and every token in the system
    reads as never used), and a window in the wrong unit. An integration test that
    authenticates twice cannot separate them: under the inverted comparison the first
    authentication still stamps through the ``IS NULL`` leg, and the second still declines.

    The rows straddle the 60s boundary rather than sitting far from it, so the window's own
    magnitude is exercised by the same truth table.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "Every successful API-token
        authentication updates ``api_tokens.last_used_at``. The update is throttled to
        per-minute granularity — the authentication path issues the ``UPDATE`` with a
        ``WHERE`` clause that makes it a no-op below 60s — so a high-frequency client
        doesn't flood the DB."
    spec: spec/feature/BACKEND_SCHEMA.md §``api_tokens`` — ``last_used_at`` is "Updated per
        use (throttled to per-minute granularity to avoid DB pressure). Null until first
        use." — that NULL start state is what the ``IS NULL`` leg exists to stamp out of.
    """
    stmt, token = await _authenticate_and_capture_the_stamp()

    def stamped(last_used_at: datetime | None) -> bool:
        return _stamp_matches(stmt, {"id": token.id, "last_used_at": last_used_at})

    assert stamped(None), (
        "a token's first use must stamp: last_used_at is 'Null until first use', so without "
        "the IS NULL leg the column never leaves NULL and every token reads as unused "
        "forever. spec: spec/feature/BACKEND_SCHEMA.md §api_tokens."
    )
    assert not stamped(_STAMP_NOW - timedelta(seconds=1)), (
        "a row stamped one second ago must not be stamped again — the throttle exists so a "
        "high-frequency client doesn't flood the DB. spec: spec/feature/AUTH.md §Audit and "
        "last_used_at."
    )
    assert not stamped(_STAMP_NOW - timedelta(seconds=59)), (
        "a row stamped 59 seconds ago is still inside the 60s window and must not be "
        "stamped again. spec: spec/feature/AUTH.md §Audit and last_used_at — 'a WHERE "
        "clause that makes it a no-op below 60s'."
    )
    assert stamped(_STAMP_NOW - timedelta(seconds=61)), (
        "a row stamped 61 seconds ago is outside the window and must be stamped again, or "
        "last_used_at freezes at its first value and stops being an audit trail at all. "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'Every successful API-token "
        "authentication updates api_tokens.last_used_at.'"
    )
    assert stamped(_STAMP_NOW - timedelta(days=30)), (
        "a long-dormant token must be stamped on use. spec: spec/feature/AUTH.md §Audit and "
        "last_used_at."
    )


@pytest.mark.asyncio
async def test_the_stamp_writes_the_current_time_into_last_used_at_and_nothing_else() -> None:
    """The SET clause assigns ``now()`` to ``last_used_at`` — that column, that value.

    The two ways to get this wrong both leave a statement that runs cleanly and a suite
    that stays green: assigning NULL (``last_used_at`` is *cleared* on every use, so a
    constantly-used token reads exactly like one that has never been used — the reading
    §Audit warns is not evidence of disuse), and assigning the timestamp to a neighbouring
    column such as ``created_at``, which rewrites the token's creation record while leaving
    the audit column stale. Neither is visible to a caller: nothing reads ``last_used_at``
    in band, and the UPDATE's row count is the same either way.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "Every successful API-token
        authentication updates ``api_tokens.last_used_at``."
    spec: spec/feature/BACKEND_SCHEMA.md §``api_tokens`` — ``last_used_at`` is "Updated per
        use"; ``created_at`` is a separate column that a use does not touch.
    spec: spec/feature/AUTH.md §Lifecycle endpoints — ``GET /auth/api-tokens`` returns
        ``{id, name, role_snapshot, created_at, last_used_at, expires_at}``, so both columns
        are read by users and neither may be written in place of the other.
    """
    stmt, _token = await _authenticate_and_capture_the_stamp()

    set_clause = _stamp_set_clause(stmt)

    assert set(set_clause) == {"last_used_at"}, (
        f"the stamp must assign last_used_at and no other column; it assigns "
        f"{sorted(set_clause)!r}. Writing the timestamp to a neighbour (created_at) "
        "rewrites that column while leaving the audit column stale, and nothing reads "
        "either in band. spec: spec/feature/AUTH.md §Audit and last_used_at."
    )
    assert set_clause["last_used_at"] == _STAMP_NOW, (
        f"the stamp must assign the current time; it assigns "
        f"{set_clause['last_used_at']!r}. Assigning NULL clears the column on every use, so "
        "a constantly-used token reads exactly like one never used — the reading "
        "spec/feature/AUTH.md §Audit and last_used_at warns about. spec: "
        "spec/feature/BACKEND_SCHEMA.md §api_tokens — last_used_at is 'Updated per use'."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "at"),
    [
        # The session cannot be opened at all — a pool timeout, or an engine that
        # cannot hand out a connection.
        ("the stamping session cannot be opened", "open"),
        ("the UPDATE itself faults", "execute"),
        ("the commit cannot land", "commit"),
    ],
)
async def test_a_failed_last_used_stamp_never_fails_the_authentication(
    label: str, at: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A stamp that cannot be written is swallowed, logged at ERROR, and the caller is served.

    The identity was already earned: the token was found, is unrevoked and unexpired, and
    the effective role has been computed. Answering that request with a 500 because an
    audit column could not be stamped would deny a valid credential over a write nobody
    reads in band.

    ERROR rather than the WARNING the rest of the best-effort list uses, and the token id
    is asserted in the *formatted message* rather than as a record attribute: the deployed
    API installs no root handler, so records reach ``logging.lastResort``, which renders
    ``%(message)s`` alone. An id carried only in ``extra`` would satisfy a record-attribute
    assertion while the deployed log line named no token at all — and this record is the
    only trace of a lost stamp.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "any failure writing it — a
        lost connection, a pool timeout, a session that cannot be opened — is logged at
        ``ERROR`` and swallowed rather than surfaced: the column keeps its prior value and
        the request continues with the identity it earned."
    spec: spec/feature/BACKEND.md §Best-Effort Operations — "the ``api_tokens.last_used_at``
        stamp is logged at ``ERROR`` with ``exc_info=True``, because nothing reads that
        column in band, so the log record is the only trace of a lost stamp".
    spec: spec/feature/BACKEND.md §Best-Effort Operations (table) — "``api_tokens.last_used_at``
        throttled stamp | PAT authentication | The column keeps its prior value;
        authentication succeeds and the request proceeds."
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    token, user = _valid_token_and_user()
    mock_db = _db_returning(token, user)
    stamp_failure = RuntimeError("pool timed out")

    if at == "open":
        # The factory is derived fine and its *call* raises — a pool that cannot hand out
        # a connection. Arming the derivation instead (``independent_sessionmaker`` itself
        # raising) would exercise a shape production cannot reach, since the helper is
        # total (spec/feature/BACKEND.md §Shared Services — "the helper is total -- it
        # never propagates"), and would pin the derivation's placement relative to the
        # ``try`` as if it were load-bearing.
        seam = MagicMock(return_value=MagicMock(side_effect=stamp_failure))
    else:
        seam = MagicMock(return_value=lambda: _StampSession(stamp_failure, at))

    caplog.set_level(logging.DEBUG)
    # Module scope, not the source module: ``api_tokens`` imports the helper by name.
    with patch("src.backend.auth.api_tokens.independent_sessionmaker", seam):
        returned_user, effective_role, token_id = await lookup_and_validate(
            mock_db, "dsk_stamp_fails_but_auth_stands"
        )

    assert (returned_user, effective_role, token_id) == (user, "Editor", token.id), (
        f"{label}: authentication must return the identity it earned; got "
        f"{(returned_user, effective_role, token_id)!r}. spec: spec/feature/AUTH.md "
        "§Audit and last_used_at — 'the request continues with the identity it earned'."
    )

    # Scoped to this module's logger as well as to the exception object: without the name
    # filter a record emitted anywhere else in the process — a shared handler, a wrapper
    # that re-logs the same exception instance — would satisfy the level and message
    # assertions below on behalf of an ``api_tokens`` that logged nothing at all.
    carrying = [
        r
        for r in caplog.records
        if r.name == "src.backend.auth.api_tokens"
        and r.exc_info is not None
        and r.exc_info[1] is stamp_failure  # type: ignore[index]
    ]
    assert carrying, (
        f"{label}: the swallowed stamp failure must reach the log with the exception — it "
        f"is the only trace that the stamp was lost, since a stale last_used_at is "
        f"indistinguishable from a genuinely unused token; captured "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]!r}. "
        "spec: spec/feature/BACKEND.md §Best-Effort Operations."
    )
    # Non-empty by the assertion above, so neither assertion below passes vacuously.
    assert {r.levelname for r in carrying} == {"ERROR"}, (
        f"{label}: this stamp is the one best-effort operation logged at ERROR rather than "
        f"WARNING; got {sorted({r.levelname for r in carrying})!r}. "
        "spec: spec/feature/BACKEND.md §Best-Effort Operations — 'One listed row takes the "
        "same exception: the api_tokens.last_used_at stamp is logged at ERROR with "
        "exc_info=True'."
    )
    assert all(str(token.id) in r.getMessage() for r in carrying), (
        f"{label}: the token id must appear in the formatted message, not only in a record "
        f"attribute — the deployed API renders %(message)s alone through "
        f"logging.lastResort, so an id passed via extra reaches no operator; got "
        f"{[r.getMessage() for r in carrying]!r}. spec: spec/feature/AUTH.md §Audit and "
        "last_used_at — 'the ERROR log record is the only trace of that case'."
    )


@pytest.mark.asyncio
async def test_a_successful_stamp_leaves_no_error_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing is logged when the stamp lands — the ERROR means a stamp was lost, or nothing.

    The absence assertion carries its own backstop: the first leg fails the stamp and
    proves an ERROR record from this module is emitted and captured under exactly this
    configuration. Without it, the second leg would pass on an implementation that never
    logs at all, and the ERROR record is the only signal an operator gets.

    A record on the healthy path would be worse than noise: §Audit makes this record the
    evidence that a stamp was lost, so one emitted on every authentication makes the
    evidence unreadable.

    spec: spec/feature/BACKEND.md §Best-Effort Operations — the ERROR is logged on a
        *failure* of the stamp ("One listed row takes the same exception: the
        ``api_tokens.last_used_at`` stamp is logged at ``ERROR`` with ``exc_info=True``,
        because nothing reads that column in band, so the log record is the only trace of
        a lost stamp").
    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "Every successful API-token
        authentication updates ``api_tokens.last_used_at``" — the stamp is the normal
        path, so the normal path is not an error condition.
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    caplog.set_level(logging.DEBUG)

    # ── Backstop leg: a failing stamp does produce an ERROR record here ──
    failing_token, failing_user = _valid_token_and_user()
    caplog.clear()
    with patch(
        "src.backend.auth.api_tokens.independent_sessionmaker",
        # The session cannot be opened; the factory derivation itself is total and is
        # left working (spec/feature/BACKEND.md §Shared Services).
        MagicMock(return_value=MagicMock(side_effect=RuntimeError("pool timed out"))),
    ):
        await lookup_and_validate(_db_returning(failing_token, failing_user), "dsk_control_leg")
    control = [
        r
        for r in caplog.records
        if r.name == "src.backend.auth.api_tokens" and r.levelname == "ERROR"
    ]
    assert len(control) == 1, (
        f"backstop: a failed stamp must emit exactly one ERROR from this module, or the "
        f"silence asserted below proves nothing; captured "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]!r}."
    )

    # ── The healthy path ──
    token, user = _valid_token_and_user()
    stamp_session = _StampSession()
    caplog.clear()
    with patch(
        "src.backend.auth.api_tokens.independent_sessionmaker",
        MagicMock(return_value=lambda: stamp_session),
    ):
        returned_user, effective_role, token_id = await lookup_and_validate(
            _db_returning(token, user), "dsk_stamp_succeeds"
        )

    assert (returned_user, effective_role, token_id) == (user, "Editor", token.id), (
        f"the healthy leg must authenticate normally, or the silence below is the silence "
        f"of a path that never ran; got {(returned_user, effective_role, token_id)!r}. "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'the request continues with "
        "the identity it earned'."
    )
    # Backstop for the silence: the stamp really was issued and committed on this leg,
    # so the absent ERROR is the healthy path rather than a skipped one.
    assert len(stamp_session.statements) == 1 and stamp_session.commits == 1, (
        f"the healthy leg must actually stamp and commit; issued "
        f"{stamp_session.statements!r} and committed {stamp_session.commits} time(s). "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'Every successful API-token "
        "authentication updates api_tokens.last_used_at.'"
    )
    logged = [r for r in caplog.records if r.name == "src.backend.auth.api_tokens"]
    assert logged == [], (
        f"a stamp that landed must log nothing — the ERROR record is the evidence that a "
        f"stamp was lost, and one emitted on every authentication destroys that evidence; "
        f"got {[(r.levelname, r.getMessage()) for r in logged]!r}. "
        "spec: spec/feature/BACKEND.md §Best-Effort Operations."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "revoked_at", "expires_at", "row", "error_code"),
    [
        ("unknown token", None, None, None, "INVALID_API_TOKEN"),
        ("revoked token", datetime.now(tz=UTC), None, "present", "TOKEN_REVOKED"),
        (
            "expired token",
            None,
            datetime.now(tz=UTC) - timedelta(hours=1),
            "present",
            "TOKEN_EXPIRED",
        ),
    ],
)
async def test_the_stamp_guard_never_swallows_the_401_outcomes(
    label: str,
    revoked_at: datetime | None,
    expires_at: datetime | None,
    row: str | None,
    error_code: str,
) -> None:
    """The three rejections still raise even when the stamp seam is broken.

    The stamp's failure is swallowed; a rejected credential is not. The seam is armed to
    raise on every call here, so a guard widened to cover the validation checks — the
    obvious way to write this containment wrong — would turn a rejected token into a
    served request or a ``NameError``, instead of the 401 the spec requires. With a
    working seam that mutation is invisible: the guard never sees an exception to swallow.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "The swallow covers the stamp
        only: the three ``401`` outcomes above (``INVALID_API_TOKEN``, ``TOKEN_REVOKED``,
        ``TOKEN_EXPIRED``) are decided before it and still raise."
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.backend.auth.api_tokens import lookup_and_validate
    from src.shared.exceptions import AuthenticationError

    if row is None:
        first_value = None
    else:
        token, user = _valid_token_and_user()
        token.revoked_at = revoked_at
        token.expires_at = expires_at
        first_value = (token, user)

    result = MagicMock()
    result.first.return_value = first_value
    # ``spec=`` for the same reason ``_db_returning`` carries one: an attribute typo or a
    # renamed session method must fail loud rather than answer with a fresh auto-mock
    # (spec/TESTING.md §Unit Testing → Mocking rules).
    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch(
            "src.backend.auth.api_tokens.independent_sessionmaker",
            # Armed so that any stamping session opened on these paths fails; the factory
            # derivation stays total, as production's does.
            MagicMock(return_value=MagicMock(side_effect=RuntimeError("pool timed out"))),
        ),
        pytest.raises(AuthenticationError) as exc_info,
    ):
        await lookup_and_validate(mock_db, "dsk_rejected_even_with_a_broken_stamp_seam")

    assert exc_info.value.error_code == error_code, (
        f"{label}: must still raise AuthenticationError('{error_code}') with the stamp "
        f"seam broken; got '{exc_info.value.error_code}'. spec: spec/feature/AUTH.md "
        "§Audit and last_used_at — 'The swallow covers the stamp only'."
    )


@pytest.mark.asyncio
async def test_the_stamp_handler_cannot_raise_from_reading_the_token_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A token id that becomes unreadable after the stamp fails still leaves the caller served.

    This is the realistic shape of the failure, not a synthetic one: the fault that breaks
    the stamp and the fault that detaches the ORM instance are commonly the same event, so
    by the time the handler runs, ``token.id`` may no longer be readable
    (``MissingGreenlet`` on a lazy attribute read). A handler that reads the id for its own
    log line at that moment raises out of the very guard that exists to keep this request
    off the 500 path — and it raises while another exception is in flight, so the traceback
    names the log call rather than the lost stamp.

    What is held is that the ERROR handler and the return value never *re-read* the id;
    the trap is armed by making every read after the first one fail. Where the first read
    sits relative to the ``try`` is impl-incidental and was measured unkillable: moving
    ``token_id = token.id`` inside the ``try`` leaves the suite green, correctly — as the
    first statement in the guarded block, a read that fails there yields an
    ``UnboundLocalError`` from the handler instead of the original exception, and both are
    the same 500.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "any failure writing it ... is
        logged at ``ERROR`` and swallowed rather than surfaced: the column keeps its prior
        value and the request continues with the identity it earned."
    spec: spec/feature/BACKEND.md §Best-Effort Operations — "if they fail, the primary
        operation succeeds"; the stamp's row: "authentication succeeds and the request
        proceeds."
    """
    from src.backend.auth.api_tokens import lookup_and_validate

    detached = RuntimeError("MissingGreenlet: attribute read on a detached instance")

    class _TokenWhoseIdBreaksAfterTheFirstRead:
        def __init__(self, token_id: uuid.UUID) -> None:
            self._token_id = token_id
            self.reads = 0
            self.revoked_at = None
            self.expires_at = None
            self.role_snapshot = "Editor"
            self.last_used_at = None

        @property
        def id(self) -> uuid.UUID:
            self.reads += 1
            if self.reads > 1:
                raise detached
            return self._token_id

    expected_id = uuid.uuid4()
    token = _TokenWhoseIdBreaksAfterTheFirstRead(expected_id)
    user = MagicMock()
    user.id = uuid.uuid4()
    user.role = "Editor"

    stamp_failure = RuntimeError("connection lost mid-UPDATE")
    caplog.set_level(logging.DEBUG)

    with patch(
        "src.backend.auth.api_tokens.independent_sessionmaker",
        MagicMock(return_value=lambda: _StampSession(stamp_failure, "execute")),
    ):
        returned_user, effective_role, token_id = await lookup_and_validate(
            _db_returning(token, user), "dsk_id_unreadable_by_the_time_the_handler_runs"
        )

    assert (returned_user, effective_role, token_id) == (user, "Editor", expected_id), (
        f"the request must still be served the identity it earned; got "
        f"{(returned_user, effective_role, token_id)!r}. spec: spec/feature/AUTH.md "
        "§Audit and last_used_at."
    )
    # Backstop: the trap was armed and would have fired on a second read.
    assert token.reads == 1, (
        f"the token id must be read exactly once — neither the ERROR handler nor the "
        f"return may re-read it; it was read {token.reads} time(s), and every read after "
        f"the first raises."
    )
    logged = [
        r
        for r in caplog.records
        if r.name == "src.backend.auth.api_tokens" and r.levelname == "ERROR"
    ]
    assert len(logged) == 1 and str(expected_id) in logged[0].getMessage(), (
        f"the handler must still name the token whose stamp was lost; captured "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]!r}. "
        "spec: spec/feature/AUTH.md §Audit and last_used_at — 'the ERROR log record is the "
        "only trace of that case'."
    )
    assert logged[0].exc_info is not None and logged[0].exc_info[1] is stamp_failure, (
        f"the record must carry the stamp's own failure, not one raised by the handler; "
        f"got {logged[0].exc_info!r}."
    )


# ── list_page — the one query behind both admin token reads ───────────────────
#
# ``list_page`` is not observable from its return value in a unit test: it hands back
# whatever the session's result object was told to hand back. What it decides is the
# *statement* — which columns leave the database, which rows the predicates admit, and in
# what order the page is cut out of them. So the statements are compiled and read here,
# the way the sweep tests read a router's ORDER BY, rather than executed.
#
# The row-level half — that a matching row really appears and a non-matching one really
# does not, against PostgreSQL — is spot integration's
# (``tests/integration/spot/test_auth_api_tokens.py``). These tests hold the half that
# survives no DB: a predicate the impl never put in the statement cannot filter anything,
# and a column in the SELECT list is on the wire whether or not the schema serialises it.


def _normalised(sql: str) -> str:
    """Collapse the compiled statement's whitespace so clause splitting is reliable."""
    return " ".join(sql.split())


def _select_list(sql: str) -> str:
    """The compiled statement's SELECT list — everything before its top-level ``FROM``."""
    normalised = _normalised(sql)
    assert " from " in normalised, (
        f"expected a SELECT ... FROM statement to read the column list out of; got {sql!r}"
    )
    return normalised.split(" from ", 1)[0]


def _where_clause(sql: str) -> str:
    """The compiled statement's WHERE clause; ``""`` when it carries none.

    A statement with no WHERE is the *unfiltered* case and must read as such — returning
    the empty string rather than raising is what lets the filter tests below assert both
    directions (predicate present / predicate absent) with one reader.
    """
    normalised = _normalised(sql)
    if " where " not in normalised:
        return ""
    tail = normalised.split(" where ", 1)[1]
    for stop in (" order by ", " limit ", " offset "):
        tail = tail.split(stop, 1)[0]
    return tail.strip()


def _order_by_clause(sql: str) -> str:
    """The compiled statement's ORDER BY clause; ``""`` when it carries none."""
    normalised = _normalised(sql)
    if " order by " not in normalised:
        return ""
    tail = normalised.split(" order by ", 1)[1]
    for stop in (" limit ", " offset "):
        tail = tail.split(stop, 1)[0]
    return tail.strip()


async def _capture_list_page_sql(**kwargs: Any) -> tuple[str, str, Any, int]:
    """Run ``list_page`` on a recording session; return ``(rows SQL, count SQL, rows, total)``.

    The session routes by the SQL each statement compiles to — the aggregate goes to the
    count result, everything else to the rows result — per spec/TESTING.md §Unit Testing →
    Mocking rules ("use a query-routing fake session that returns results by inspecting the
    SQL/statement it receives"), never a call-ordered ``side_effect`` list.

    The two-statement assertion is the backstop every caller leans on: without it a reader
    would happily describe one statement while the other went unexamined, and the
    "same predicates" test below would compare a clause against itself.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.backend.auth.api_tokens import list_page

    captured: list[str] = []
    sentinel_rows = [object(), object()]

    rows_result = MagicMock()
    rows_result.all.return_value = sentinel_rows
    count_result = MagicMock()
    count_result.scalar_one.return_value = 7

    def _record(sql: str) -> bool:
        """Record every statement, then decline so the real routes supply the result."""
        captured.append(sql)
        return False

    db = AsyncMock(spec=AsyncSession)
    route_db_execute(db, [(_record, None), ("count(", count_result)], default=rows_result)

    rows, total = await list_page(db, **kwargs)

    assert len(captured) == 2, (
        f"list_page must issue exactly two statements — the page of rows and the matching "
        f"COUNT — for these assertions to have both to read; it issued {len(captured)}: "
        f"{captured!r}"
    )
    rows_sql = next(sql for sql in captured if "count(" not in sql)
    count_sql = next(sql for sql in captured if "count(" in sql)
    return rows_sql, count_sql, rows, total


@pytest.mark.asyncio
async def test_list_page_never_puts_the_token_hash_on_the_wire() -> None:
    """``token_hash`` is in no part of the statement the admin reads run.

    This is the central security invariant of the two admin token reads, and it is the one
    the response schema cannot hold on its own: a Pydantic model that omits a field still
    lets the column cross the wire from PostgreSQL into an ORM instance any later code —
    a debug dump, a ``model_dump`` on the ORM row, a logged repr — can read. §Token format
    and storage makes the hash the *only* stored form of the credential, so a SELECT that
    ships it hands out the material a stolen page needs.

    The absence assertion is not vacuous: the control leg compiles a plain ``select(ApiToken)``
    and asserts this same reader *does* see ``token_hash`` there, so a reader that simply
    never finds the string cannot pass this test (spec/TESTING.md §Assertion Discipline —
    "Absence assertions require injection"). The positive leg pins the documented item
    shape, so a ``load_only`` narrowed until nothing useful is selected also fails.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens`` returns
        ``tokens: [{id, name, role_snapshot, created_at, last_used_at, expires_at,
        revoked_at, user_id, user_email}]`` — "the token hash is never returned".
    spec: spec/feature/AUTH.md §API Tokens §Token format and storage — "Only the SHA-256
        hash of the token is stored in the ``api_tokens`` table (column ``token_hash``).
        The raw token is returned **once** ... and never retrievable again."
    """
    from sqlalchemy import select

    from src.shared.db.models import ApiToken

    # ── Control leg: the reader can see token_hash when a statement really selects it ──
    unrestricted = _select_list(compiled_sql(select(ApiToken)))
    assert "token_hash" in unrestricted, (
        f"backstop: a plain select(ApiToken) must show token_hash in its SELECT list, or "
        f"the absence asserted below is the absence of a working reader rather than of the "
        f"column; got {unrestricted!r}"
    )

    rows_sql, count_sql, _rows, _total = await _capture_list_page_sql()

    assert "token_hash" not in rows_sql, (
        f"the admin token reads must never select api_tokens.token_hash — it is the only "
        f"stored form of the credential, and a column that reaches the ORM instance is "
        f"readable by anything downstream regardless of what the response schema "
        f"serialises. Statement: {rows_sql!r}. spec: spec/API.md §Admin — 'the token hash "
        f"is never returned'."
    )
    assert "token_hash" not in count_sql, (
        f"the COUNT must not name token_hash either. Statement: {count_sql!r}"
    )

    # Positive leg: the documented item shape is what the SELECT list carries, so a
    # load_only narrowed past usefulness fails here rather than passing the absence check.
    select_list = _select_list(rows_sql)
    for column in (
        "api_tokens.id",
        "api_tokens.name",
        "api_tokens.role_snapshot",
        "api_tokens.created_at",
        "api_tokens.last_used_at",
        "api_tokens.expires_at",
        "api_tokens.revoked_at",
        "api_tokens.user_id",
        "users.email",
    ):
        assert column in select_list, (
            f"the admin item shape needs {column} and the SELECT list does not carry it: "
            f"{select_list!r}. spec: spec/API.md §Admin — 'tokens: [{{id, name, "
            f"role_snapshot, created_at, last_used_at, expires_at, revoked_at, user_id, "
            f"user_email}}]'."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_revoked", "predicate_expected"),
    [(False, True), (True, False)],
)
async def test_include_revoked_governs_the_revoked_at_predicate(
    include_revoked: bool, predicate_expected: bool
) -> None:
    """``revoked_at IS NULL`` is the default filter, and ``include_revoked=true`` drops it.

    Both directions are asserted from one parametrization because either alone is
    satisfiable by a broken impl: a statement that always carries the predicate hides the
    opt-in (revoked rows become unreachable, and §Revoked-token visibility says incident
    review needs them), and one that never carries it hides the default (revoked
    credentials pad every page).

    The clause is also checked to name *nothing else*: §Revoked-token visibility says
    "``revoked_at IS NULL`` is the whole of the default filter", and an ``expires_at``
    predicate smuggled in beside it would silently turn the list into a liveness view —
    the exact reading that section rules out.

    spec: spec/feature/AUTH.md §Revoked-token visibility — "Both admin reads exclude
        revoked rows by default and take ``include_revoked=true`` to bring them back";
        "``revoked_at IS NULL`` is the whole of the default filter. Expiry is not filtered".
    spec: spec/API.md §Admin — "``?include_revoked=true`` also returns rows with
        ``revoked_at`` set; default ``false``".
    """
    rows_sql, _count_sql, _rows, _total = await _capture_list_page_sql(
        include_revoked=include_revoked
    )
    where = _where_clause(rows_sql)

    if predicate_expected:
        assert "api_tokens.revoked_at is null" in where, (
            f"the default read must filter revoked rows out; its WHERE clause is {where!r}. "
            f"spec: spec/feature/AUTH.md §Revoked-token visibility."
        )
    else:
        assert "revoked_at" not in where, (
            f"include_revoked=true must drop the revocation predicate so withdrawn "
            f"credentials come back; its WHERE clause is {where!r}. spec: "
            f"spec/feature/AUTH.md §Revoked-token visibility — 'Those rows stay reachable "
            f"because incident review needs to see when a credential was withdrawn'."
        )

    assert "expires_at" not in where, (
        f"expiry must not be filtered — a token past expires_at 'sits in the default page "
        f"like any other row', and the item's own expires_at is what identifies it; the "
        f"WHERE clause is {where!r}. spec: spec/feature/AUTH.md §Revoked-token visibility."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [True, False])
async def test_user_id_narrows_to_one_owner_and_omitting_it_spans_every_user(
    scoped: bool,
) -> None:
    """``user_id`` adds an owner predicate; omitting it leaves the inventory deployment-wide.

    Both directions again, and for the same reason: a predicate that is always present
    would make ``GET /admin/api-tokens`` unable to answer the question it exists for
    ("what long-lived credentials stand against this deployment"), while one that is never
    present would make ``GET /admin/users/{id}/api-tokens`` return everyone's tokens on a
    page an admin opened from one user's row.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens`` is "every user's API tokens — the
        deployment-wide inventory ... ``?user_id=`` narrows to one owner".
    spec: spec/API.md §Admin — ``GET /admin/users/{id}/api-tokens`` is "one user's API
        tokens ... The ``id`` is an owner filter".
    """
    owner_id = uuid.uuid4()
    rows_sql, _count_sql, _rows, _total = await _capture_list_page_sql(
        user_id=owner_id if scoped else None
    )
    where = _where_clause(rows_sql)

    if scoped:
        assert "api_tokens.user_id =" in where, (
            f"a user_id must become an owner predicate on the rows query; its WHERE clause "
            f"is {where!r}. spec: spec/API.md §Admin — 'the id is an owner filter'."
        )
    else:
        assert "api_tokens.user_id" not in where, (
            f"with no user_id the inventory must span every owner; its WHERE clause is "
            f"{where!r}. spec: spec/API.md §Admin — 'every user's API tokens — the "
            f"deployment-wide inventory'."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("default view", {}),
        ("revoked included", {"include_revoked": True}),
        ("one owner", {"user_id": uuid.uuid4()}),
        ("one owner, revoked included", {"include_revoked": True, "user_id": uuid.uuid4()}),
    ],
)
async def test_the_count_matches_the_rows_query_filters_and_carries_no_page_window(
    label: str, kwargs: dict[str, Any]
) -> None:
    """``total_count`` counts the same filtered set the page is cut from, unpaged.

    Two independent ways to get this wrong both produce a response that looks fine: a COUNT
    over a *different* predicate set (say, one that forgot ``include_revoked``) reports a
    total no page can ever reach, so a client paging to ``total_count`` walks off the end
    into empty pages; and a COUNT that inherits the rows query's ``LIMIT``/``OFFSET``
    reports the page size instead of the collection size, so pagination controls collapse
    to one page. Neither is visible from a single request's body.

    The equality is asserted on the *clause*, not on the presence of individual predicates,
    so a filter added to one query and not the other fails here whatever it is.

    spec: spec/API_DESIGN_PRINCIPLE_en.md §5 (Pagination — the standard envelope) —
        "``total_count`` is the unpaged size of the filtered collection, letting clients
        render page counts without a second request."
    spec: spec/feature/AUTH.md §Revoked-token visibility — "Both routes express their
        filter, ordering, and page bounds in SQL, so a request transfers and materialises
        one page rather than the whole matching set."
    """
    rows_sql, count_sql, _rows, _total = await _capture_list_page_sql(limit=5, offset=10, **kwargs)

    assert _where_clause(count_sql) == _where_clause(rows_sql), (
        f"{label}: the COUNT must filter exactly as the rows query does, or total_count "
        f"describes a collection the page is not drawn from. rows WHERE "
        f"{_where_clause(rows_sql)!r} vs count WHERE {_where_clause(count_sql)!r}. spec: "
        f"spec/API_DESIGN_PRINCIPLE_en.md §5 — 'total_count is the unpaged size of the "
        f"filtered collection'."
    )
    assert " limit " not in _normalised(count_sql) and " offset " not in _normalised(count_sql), (
        f"{label}: the COUNT must be unpaged — a LIMIT/OFFSET on it reports the page size "
        f"as the collection size. Statement: {count_sql!r}. spec: "
        f"spec/API_DESIGN_PRINCIPLE_en.md §5."
    )
    assert " limit " in _normalised(rows_sql) and " offset " in _normalised(rows_sql), (
        f"{label}: the page window must be expressed in SQL rather than sliced in Python. "
        f"Statement: {rows_sql!r}. spec: spec/feature/AUTH.md §Revoked-token visibility — "
        f"'Both routes express their filter, ordering, and page bounds in SQL'."
    )


@pytest.mark.asyncio
async def test_the_count_backstop_sees_a_predicate_at_all() -> None:
    """Backstop for the equality above: the default view's WHERE is not empty.

    ``_where_clause`` returns ``""`` for an unfiltered statement, so
    ``count_where == rows_where`` is satisfied by two statements that both carry no
    predicate at all — which is precisely what a ``list_page`` that dropped every filter
    would produce. This asserts the compared clause is a real one in the case the routes
    serve by default.

    spec: spec/feature/AUTH.md §Revoked-token visibility — "``revoked_at IS NULL`` is the
        whole of the default filter."
    """
    rows_sql, count_sql, _rows, _total = await _capture_list_page_sql()
    assert _where_clause(rows_sql) and _where_clause(count_sql), (
        f"the default read must carry a WHERE clause on both statements, or the equality "
        f"asserted elsewhere in this module compares nothing to nothing. rows: "
        f"{rows_sql!r}; count: {count_sql!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "order_by_spec", "expected_key", "expected_direction"),
    [
        ("sort omitted", None, "api_tokens.created_at", "desc"),
        ("created_at_desc", ("created_at", "desc"), "api_tokens.created_at", "desc"),
        ("created_at_asc", ("created_at", "asc"), "api_tokens.created_at", "asc"),
        ("last_used_at_desc", ("last_used_at", "desc"), "api_tokens.last_used_at", "desc"),
        ("last_used_at_asc", ("last_used_at", "asc"), "api_tokens.last_used_at", "asc"),
    ],
)
async def test_every_sort_input_orders_nulls_last_and_tiebreaks_on_the_row_id(
    label: str,
    order_by_spec: tuple[str, str] | None,
    expected_key: str,
    expected_direction: str,
) -> None:
    """The compiled ordering is ``<key> <dir> NULLS LAST, api_tokens.id ASC`` — always.

    Three claims, none of which a single request can expose:

    1. **The default is ``created_at`` descending.** ``spec/API.md §Admin`` fixes it for
       both routes; the router forwards ``order_by=None`` when ``sort`` is omitted, so the
       default lives here.
    2. **NULLS LAST.** ``last_used_at`` is NULL for every never-used token and PostgreSQL
       sorts NULLs *first* under a bare ``DESC``. Without this, ``sort=last_used_at_desc``
       fills the front of the page with tokens that have never authenticated — the exact
       inverse of what an auditor asked for, on a page that is otherwise perfectly
       well-formed. It is applied to every sort input rather than to ``last_used_at``
       alone so the rule does not need re-deriving each time a sort key is added.
    3. **The ``id`` tiebreak.** Neither sort column is unique — tokens minted in one
       transaction share a ``created_at``, and every unused token shares a NULL
       ``last_used_at`` — so without a unique final key PostgreSQL may order the tied
       block differently between the page-1 and page-2 queries. A credential can then
       appear twice, or in no page at all: an inventory that silently omits a live
       credential is worse than one that fails.

    The reader keys on meaning (which column, which direction, which position in the
    clause), not on the whole rendered string, so respelling the expression is free.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens`` and
        ``GET /admin/users/{id}/api-tokens`` are "sortable by ``created_at``/``last_used_at``
        (default ``created_at_desc``)".
    spec: spec/feature/AUTH.md §Revoked-token visibility — "Either ordering places nulls
        last and is tiebroken by token id, so paging an inventory returns each token exactly
        once regardless of the requested ``sort``."
    spec: spec/feature/AUTH.md §Revoked-token visibility — "ties are certain under
        ``last_used_at``, where every never-used token shares a null, and reachable under
        ``created_at``, where tokens minted in one transaction share a timestamp — an
        unspecified order within a tie can shift between the page-1 and page-2 queries and
        drop a live credential from every page."
    spec: spec/API_DESIGN_PRINCIPLE_en.md §5 (Pagination) — ``offset``/``limit`` paging over
        a collection whose ``total_count`` "lets clients render page counts": walking those
        pages returns each row once only if the ordering is total.
    """
    from src.shared.db.models import ApiToken

    order_by = None
    if order_by_spec is not None:
        column_name, direction = order_by_spec
        order_by = getattr(getattr(ApiToken, column_name), direction)()

    rows_sql, count_sql, _rows, _total = await _capture_list_page_sql(order_by=order_by)
    order_clause = _order_by_clause(rows_sql)

    assert order_clause, (
        f"{label}: the rows query must carry an ORDER BY; got {rows_sql!r}"
    )
    keys = [part.strip() for part in order_clause.split(",")]
    assert len(keys) == 2, (
        f"{label}: the ordering must be the requested key plus exactly one tiebreak; got "
        f"{keys!r}"
    )

    primary, tiebreak = keys
    # ``endswith`` on the first word: the compiled name is schema-qualified
    # (``dataspoke.api_tokens.<column>``) and the schema is not what is being asserted.
    assert primary.split()[0].endswith(expected_key), (
        f"{label}: must order by {expected_key} first; got {primary!r}. spec: "
        f"spec/API.md §Admin — sortable by created_at/last_used_at, default created_at_desc."
    )
    if expected_direction == "desc":
        assert " desc" in primary, f"{label}: must order {expected_key} descending; got {primary!r}"
    else:
        assert " desc" not in primary, (
            f"{label}: must order {expected_key} ascending; got {primary!r}"
        )
    assert "nulls last" in primary, (
        f"{label}: the requested key must sort NULLS LAST, or a sort by last_used_at fills "
        f"the front of the page with tokens that have never been used; got {primary!r}."
    )
    assert tiebreak.split()[0].endswith("api_tokens.id") and " desc" not in tiebreak, (
        f"{label}: the ordering must be tie-broken on the unique row id so a page boundary "
        f"falling inside a block of rows sharing a sort value cannot show a credential "
        f"twice or drop it; got {tiebreak!r}."
    )

    assert not _order_by_clause(count_sql), (
        f"{label}: the COUNT must not order — ordering an aggregate is work that changes no "
        f"answer; got {count_sql!r}."
    )


@pytest.mark.asyncio
async def test_list_page_returns_the_rows_and_the_count_it_read() -> None:
    """The page comes from the rows query and the total from the COUNT — not vice versa.

    The two results are distinguishable here (a two-element rows sequence, a total of 7),
    so a return that paired the page length with itself — the shape that silently caps
    ``total_count`` at ``limit`` and makes pagination look complete on page one — cannot
    pass.

    spec: spec/API_DESIGN_PRINCIPLE_en.md §5 (Pagination — the standard envelope) —
        "``total_count`` is the unpaged size of the filtered collection".
    """
    _rows_sql, _count_sql, rows, total = await _capture_list_page_sql(limit=2)

    assert len(rows) == 2, f"the page must be the rows query's result; got {rows!r}"
    assert total == 7, (
        f"total_count must be the COUNT's scalar, not the page length; got {total!r}. "
        f"spec: spec/API_DESIGN_PRINCIPLE_en.md §5."
    )


# ── revoke — what the admin route's audit event is built from ─────────────────


def _revoke_session(token: Any) -> AsyncMock:
    """A session whose one SELECT answers with *token* (or ``None`` for a missing row)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    result = MagicMock()
    result.scalar_one_or_none.return_value = token

    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_revoke_reports_the_owner_and_that_this_call_ended_the_token() -> None:
    """A first revoke sets ``revoked_at`` and reports ``(owner, revoked=True)``.

    The admin route books its ``AUTH.API_TOKEN_REVOKED`` event on the **owner**, and it
    reaches ``revoke`` holding only a token id — the path's user id is not checked against
    the row, which is what makes the route usable for incident response. So the owner the
    event names can only come from here: a ``revoke`` that reported the wrong id would file
    a lost credential on a stranger's timeline, and nothing downstream could tell.

    spec: spec/feature/AUTH.md §Admin revoke audit — "It emits one
        ``AUTH.API_TOKEN_REVOKED`` event against the token's owner ... The event carries
        the token and its owner, not the acting admin."
    spec: spec/feature/BACKEND.md §Event Catalogue — ``AUTH`` (``user``,
        ``entity_id=user_id`` of the token's owner) / ``API_TOKEN_REVOKED``.
    spec: spec/feature/AUTH.md §Lifecycle endpoints — "Revoke a user's token (admin;
        incident response)"; the write is ``revoked_at = now()``.
    """
    from src.backend.auth.api_tokens import revoke
    from src.shared.db.models import ApiToken

    owner_id = uuid.uuid4()
    token_id = uuid.uuid4()
    token = MagicMock(spec=ApiToken)
    token.id = token_id
    token.user_id = owner_id
    token.revoked_at = None

    db = _revoke_session(token)
    result = await revoke(db, token_id=token_id)

    assert result.owner_user_id == owner_id, (
        f"revoke must report the token's owner — the admin route has no other way to know "
        f"whose timeline the AUTH.API_TOKEN_REVOKED event belongs on; got "
        f"{result.owner_user_id!r}, expected {owner_id!r}. spec: spec/feature/AUTH.md "
        f"§Admin revoke audit."
    )
    assert result.revoked is True, (
        "a token that was live before the call must report revoked=True, or the route "
        "writes no event for a credential it just killed. spec: spec/feature/AUTH.md "
        "§Admin revoke audit — 'Setting revoked_at is the whole of what ends a token's "
        "life, so the write is the security event'."
    )
    assert token.revoked_at is not None, (
        "revoke must actually set revoked_at; a result object reporting revoked=True over "
        "an untouched row is a report of nothing. spec: spec/feature/AUTH.md §Lifecycle "
        "endpoints — 'Revoke a user's token'."
    )


@pytest.mark.asyncio
async def test_revoking_an_already_revoked_token_reports_no_second_kill() -> None:
    """A repeat revoke reports ``revoked=False`` and leaves the original timestamp alone.

    The event exists because the *write* is the security act. A second call writes nothing,
    so a second event would claim a credential was killed twice — noise on the one timeline
    §Admin revoke audit puts every credential a user loses onto. The original ``revoked_at``
    must also survive: it is when the credential was actually withdrawn, which is what
    incident review reads.

    spec: spec/feature/AUTH.md §Admin revoke audit — "Setting ``revoked_at`` is the whole
        of what ends a token's life, so the write **is** the security event".
    spec: spec/feature/AUTH.md §Revoked-token visibility — revoked rows "stay reachable
        because incident review needs to see when a credential was withdrawn, which is what
        ``revoked_at`` carries."
    """
    from src.backend.auth.api_tokens import revoke
    from src.shared.db.models import ApiToken

    owner_id = uuid.uuid4()
    token_id = uuid.uuid4()
    already_revoked_at = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)
    token = MagicMock(spec=ApiToken)
    token.id = token_id
    token.user_id = owner_id
    token.revoked_at = already_revoked_at

    db = _revoke_session(token)
    result = await revoke(db, token_id=token_id)

    assert result.revoked is False, (
        "a repeat revoke wrote nothing, so it must report revoked=False — the route keys "
        "its event off this flag, and an event for a no-op describes an act that did not "
        "happen. spec: spec/feature/AUTH.md §Admin revoke audit."
    )
    assert result.owner_user_id == owner_id, (
        f"the owner must still be reported on the no-op path; got {result.owner_user_id!r}"
    )
    assert token.revoked_at == already_revoked_at, (
        f"the original revocation timestamp must survive a repeat call — it is when the "
        f"credential was withdrawn, which is what incident review reads; got "
        f"{token.revoked_at!r}. spec: spec/feature/AUTH.md §Revoked-token visibility."
    )
    db.flush.assert_not_awaited()
