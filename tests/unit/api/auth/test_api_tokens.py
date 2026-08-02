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

from tests.unit.conftest import route_db_execute

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
    unaffected. Only ``Update`` statements are counted as stamps — the throttle is spec'd
    as a check made *before* issuing the UPDATE, so an implementation that reads
    ``last_used_at`` back with its own SELECT first is equally conformant and must not
    fail here. Engines are never connected to: SQLAlchemy defers connection until a
    statement runs, and none does.

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
