"""Spot integration test: the `users` row lock really serializes credential-creating writes.

spec/feature/AUTH.md §Serialization of credential-creating writes says each
credential-creating self-service write "takes the `users` row lock and re-checks,
under it, the state that authorised it", and concludes: "Because each takes the lock
the bind transaction holds, none can commit before it; because each re-reads after
acquiring it, none can commit a credential authorised by state the bind superseded."

Both halves of that sentence are claims about **two concurrent transactions**, so
they are provable only against a real PostgreSQL — which is why this file sits at
spot (spec/TESTING.md §Spot integration tests: "a spot test may call dataspoke
Python directly (e.g., a backend service or a workflow stub) **or** call the API
over HTTP"). A single-session fake can show the post-lock comparison declining on a
moved epoch, but nothing in it ever waits, so it cannot show that
``SELECT ... FOR UPDATE`` orders anything at all.

Coverage — every row of the spec table, the PAT-carried variant, and the deleted-user
branch, each as a declining case paired with a positive control. Declined as: no token
row for ``reset/request`` and the deleted-owner branch; ``401 UNAUTHORIZED`` for the
JWT-carried ``api-tokens`` / ``PATCH /auth/me``; ``401 TOKEN_REVOKED`` for their
PAT-carried twins; ``400 INVALID_RESET_TOKEN`` for ``reset/confirm``.

- ``POST /auth/password/reset/request``, driven in-process through
  ``reset.issue_reset_token`` — the epoch decline is invisible in the route's
  response, which "still returns `204`, unchanged, since the route reports the same
  outcome for known and unknown emails and must not become an oracle for account
  state".
- The five HTTP rows are driven against the in-cluster API so each route's own
  wiring to its re-check (``revalidate_under_user_lock``; ``confirm_reset``'s own
  lock) is what is under test rather than a patched helper. They are one
  parametrized pair of tests over :data:`_HTTP_WRITES`, so the declining case and its
  control stay sibling-visible per row. AUTH.md singles the mint row out: "Needed
  here in particular because the API-token authentication path runs no epoch check,
  so a token committed after the reset would otherwise stay live."
- ``issue_reset_token``'s ``locked is None`` branch — the owner row hard-deleted
  between the address lookup and the lock — driven in-process, the real
  ``users.hard_delete`` standing in for the delete committing on the other side.

The other side of every bind race is the **real** ``users.bind_google_identity``
running uncommitted in a second session, not a hand-rolled stand-in: the sentence
under test names "the lock **the bind transaction holds**", so the bind is what must
hold it. The declining tests give it an unbound row — the branch that takes the lock,
clears the row's credentials, increments ``session_epoch``, revokes the row's API
tokens and deletes its unused reset tokens. The controls give it a row already
carrying the same ``sub``, which is the branch that "writes nothing and emits
nothing" while still holding the lock for its transaction's life. The deleted-user
pair swaps the bind for ``users.hard_delete``, which holds the same lock by virtue of
its ``DELETE``; its control rolls that delete back.

Blocked-ness is *observed*, not assumed: ``pg_blocking_pids`` is polled from a third
connection until a backend is seen waiting on the holder, and the budget's
exhaustion is a failure rather than a skip (spec/TESTING.md §Assertion Discipline —
"a wait that exhausts its budget is a failure, not a skip"). The HTTP races
additionally probe that the holder blocks *nobody* before the request is dispatched
and require exactly one waiter afterwards, stuck on a ``FOR UPDATE`` against
``dataspoke.users``. That is not optional: for every HTTP row the pre-lock
authorisation failure and the post-lock re-check failure are byte-identical
(`401 UNAUTHORIZED` / `401 TOKEN_REVOKED` / `400 INVALID_RESET_TOKEN`, same message),
so the identity of the waiter is the only thing that can tell them apart.

That observation is what fixes the ordering each test depends on: the contender's
authorising read necessarily completed *before* the bind committed, because the
contender can only reach the lock after that read passed. A refusal afterwards is
therefore the post-lock re-check catching superseded state — not the ordinary
pre-lock check seeing an already-committed epoch.

The PAT-carried rows carry one more ordering constraint. ``lookup_and_validate``
stamps ``api_tokens.last_used_at`` on a session of its own whenever the column is
NULL or older than 60 s, and a freshly minted token has it NULL. That stamp is an
``UPDATE`` of the very ``api_tokens`` row the bind's ``revoke_all_for_user`` holds
uncommitted, so the contender would block *during authentication, on the stamp* —
before ever reaching the ``users`` lock — and the observed wait would be the wrong
one. The seeded PAT therefore gets ``last_used_at = now()``, which makes the stamp's
``WHERE`` match nothing so it takes no row lock; the 60 s throttle window comfortably
outlasts the pre-lock phase. If that ever stops holding, the query-text narrowing
above fails loudly rather than letting the wrong wait pass.

The controls — the variants where the holder supersedes nothing — are not
decoration. They are what makes the absence assertions non-vacuous
(spec/TESTING.md §Assertion Discipline — "Absence assertions require injection"):
without them, "no row was written" would also pass if the block itself, or a broken
fixture, had killed the write.

spec: spec/feature/AUTH.md §Serialization of credential-creating writes
spec: spec/feature/AUTH.md §Session epoch
spec: spec/feature/AUTH.md §API Tokens
spec: spec/feature/AUTH.md §Password reset
spec: spec/feature/AUTH.md §Failure Modes
"""

import asyncio
import contextlib
import hashlib
import logging
import secrets
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import URL, text
from sqlalchemy import pool as sa_pool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The seeded row's password — held only here in plaintext; the bcrypt protocol stays
# inside src/backend/auth/users.create_user.
SEEDED_PASSWORD = "serialization-password-1"

# The password the credential-creating writes try to set (>= 10 characters, the
# schema minimum on `PATCH /auth/me` and `POST /auth/password/reset/confirm`).
NEW_PASSWORD = "serialization-new-password-2"

# Bounded so a genuinely stuck lock fails the test instead of hanging pytest. The
# contender reaches the lock in well under a second against the dev cluster; the
# budget is slack for a laptop→cluster round trip, not a tuning knob.
_BLOCK_OBSERVE_BUDGET_S = 20.0
_BLOCK_POLL_INTERVAL_S = 0.1
_CONTENDER_COMPLETION_BUDGET_S = 30.0

# The HTTP request must outlive the whole race: the window in which its block is
# observed, plus the window it is then given to finish once the lock is released.
# Stated at the call site rather than inherited from the `api_client` fixture's
# default, which this file neither owns nor coordinates with.
_HTTP_RACE_TIMEOUT_S = _BLOCK_OBSERVE_BUDGET_S + _CONTENDER_COMPLETION_BUDGET_S

# `pg_blocking_pids` rather than `pg_stat_activity.wait_event_type`: the blocking-pid
# relation carries no role restriction, so this reads the same whether or not the
# tests and the API pod connect as the same PostgreSQL role.
_WAITERS_SQL = text(
    "SELECT pid FROM pg_stat_activity"
    " WHERE pid <> pg_backend_pid()"
    "   AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
)

_RESET_LOGGER = "src.backend.auth.reset"
_DECLINED_EVENT = "password_reset_token_declined"


class _RecordingNotifier:
    """Stand-in for ``NotificationService``, and the race's synchronisation point.

    ``issue_reset_token`` takes the notification service as a plain argument, so no
    patching is involved — this is the collaborator the caller supplies. It matters
    twice: it removes the need for a configured SMTP peripheral, and ``sent`` gives
    a deterministic signal that the contender has passed ``send_email`` and is
    heading for the row lock. The lock is taken after the send, never around it
    (spec/feature/AUTH.md §Serialization of credential-creating writes).
    """

    def __init__(self) -> None:
        self.sent = asyncio.Event()
        self.recipients: list[str] = []

    # `subject` / `body_html` are unused on purpose: the signature mirrors the
    # keyword call `issue_reset_token` makes, so a change to that call fails here.
    async def send_email(self, *, to: list[str], subject: str, body_html: str) -> None:
        self.recipients.extend(to)
        self.sent.set()


async def _backend_pid(session: AsyncSession) -> int:
    """Return the PostgreSQL backend pid serving *session*'s connection.

    Called before the race so a contender can be identified positively in
    ``pg_stat_activity`` rather than inferred.
    """
    result = await session.execute(text("SELECT pg_backend_pid() AS pid"))
    return int(result.scalar_one())


async def _waiters_on(session: AsyncSession, blocker_pid: int) -> list[int]:
    """Return the pids currently waiting on a lock *blocker_pid* holds.

    Rolls back after reading: ``pg_stat_activity`` is snapshotted per transaction, so
    a repeated poll on one session would otherwise keep answering with its first
    observation until the budget expired.
    """
    result = await session.execute(_WAITERS_SQL, {"blocker": blocker_pid})
    pids = [int(row.pid) for row in result.fetchall()]
    await session.rollback()
    return pids


async def _waiters_snapshot(
    session_factory: async_sessionmaker[AsyncSession], blocker_pid: int
) -> list[int]:
    """One-shot :func:`_waiters_on` on a connection of its own."""
    async with session_factory() as observer:
        return await _waiters_on(observer, blocker_pid)


async def _executing_query(session_factory: async_sessionmaker[AsyncSession], pid: int) -> str:
    """Return the SQL text *pid* is currently executing, as ``pg_stat_activity`` reports it.

    Additive narrowing on top of ``pg_blocking_pids``, used only to name *which*
    statement a waiter is stuck on. Readable here because the tests connect as the
    same role the API pod uses — ``DATASPOKE_DEV_POSTGRES_USER`` is populated from
    the same ``dataspoke-secrets`` the API reads.
    """
    async with session_factory() as observer:
        result = await observer.execute(
            text("SELECT query FROM pg_stat_activity WHERE pid = CAST(:pid AS integer)"),
            {"pid": pid},
        )
        row = result.fetchone()
        return str(row.query) if row is not None and row.query is not None else ""


async def _wait_until_blocked_by(
    session_factory: async_sessionmaker[AsyncSession],
    blocker_pid: int,
    *,
    expect_pid: int | None,
    contender: str,
) -> list[int]:
    """Poll until a backend is waiting on a lock *blocker_pid* holds; return the waiters' pids.

    *expect_pid* pins the waiter when the test owns its connection (the in-process
    races). It is ``None`` for the HTTP races, where the waiter is a backend inside
    the API pod and only its existence is knowable from here — those callers bracket
    this with an emptiness probe before dispatch and a ``len(...) == 1`` check after,
    which is what makes the observation specific.

    Raises:
        AssertionError — the budget expired with no waiter observed. Exhaustion is a
            failure, not a skip (spec/TESTING.md §Assertion Discipline).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _BLOCK_OBSERVE_BUDGET_S
    seen: list[int] = []

    async with session_factory() as observer:
        while loop.time() < deadline:
            seen = await _waiters_on(observer, blocker_pid)
            if seen and (expect_pid is None or expect_pid in seen):
                return seen
            await asyncio.sleep(_BLOCK_POLL_INTERVAL_S)

    raise AssertionError(
        f"{contender} never blocked on the users row lock held by backend {blocker_pid} "
        f"within {_BLOCK_OBSERVE_BUDGET_S}s (last pg_blocking_pids observation: "
        f"{seen or 'no waiting backend'}; expected waiter: {expect_pid or 'any'}). "
        "spec/feature/AUTH.md §Serialization of credential-creating writes requires the "
        "write to take the lock the bind transaction holds — a write that never waits "
        "has not taken it."
    )


@pytest_asyncio.fixture
async def session_factory(
    integration_db_url: URL,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Per-test engine whose sessions each own a distinct PostgreSQL connection.

    ``NullPool`` is what makes the race real: the bind and the contender must sit on
    two connections, or the second would be waiting for the first's *connection*
    rather than for its row lock, and every test here would pass without the lock
    existing. Function-scoped because an asyncpg connection is bound to the event
    loop that opened it.
    """
    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@contextlib.asynccontextmanager
async def _seeded_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    prebound: bool,
) -> AsyncGenerator[dict[str, object]]:
    """Seed one row, yield its identifiers, and hard-delete it afterwards.

    *prebound* selects which branch of ``users.bind_google_identity`` the holder
    transaction takes when handed the row's ``google_sub``: an unbound row gets the
    real bind, a row already carrying that same ``sub`` gets the no-op branch.

    The access token is the JWT credential whose ``ses`` claim the JWT-carried HTTP
    rows have re-compared under the lock. ``api_tokens`` and
    ``password_reset_tokens`` follow the row by CASCADE, so the PATs and reset
    tokens the races seed need no cleanup of their own; the delete tolerates a row
    a test already removed.
    """
    from src.backend.auth import users as user_service
    from src.backend.auth.tokens import issue_access_token

    email = f"lockrace-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    google_sub = f"lockrace-sub-{uuid.uuid4()}"
    user_id: uuid.UUID | None = None
    try:
        async with session_factory() as session:
            user = await user_service.create_user(
                session,
                email,
                "Lock Race Subject",
                password=SEEDED_PASSWORD,
                google_sub=google_sub if prebound else None,
            )
            await session.commit()
            user_id = user.id
            epoch = user.session_epoch

        access_token, _ = issue_access_token(user_id, email, session_epoch=epoch)

        yield {
            "user_id": user_id,
            "email": email,
            "google_sub": google_sub,
            "epoch": epoch,
            "access_token": access_token,
        }
    finally:
        if user_id is not None:
            async with session_factory() as session:
                await session.execute(
                    text("DELETE FROM dataspoke.users WHERE id = :id"),
                    {"id": str(user_id)},
                )
                await session.commit()


@pytest_asyncio.fixture
async def unbound_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, object]]:
    """A password-registered row with no Google identity — what a first Google sign-in binds.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "No | Yes, and
    that row has `google_sub IS NULL` | Bind `google_sub` onto the row ... run the
    [credential reset](#credential-reset-on-link), and log in."
    """
    async with _seeded_user(session_factory, prebound=False) as row:
        yield row


@pytest_asyncio.fixture
async def prebound_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, object]]:
    """A row already carrying the ``sub`` the bind presents — its write-nothing branch.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "No | Yes, and
    that row already carries **this** `sub` | Log in, exactly as the `sub`-known
    branch. No bind, no reset, no epoch bump, no event."
    """
    async with _seeded_user(session_factory, prebound=True) as row:
        yield row


# ── State readers ─────────────────────────────────────────────────────────────


async def _reset_rows(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[str]:
    """Return the ``password_reset_tokens`` hashes belonging to *user_id*."""
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT token_hash FROM dataspoke.password_reset_tokens WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        return [str(row.token_hash) for row in result.fetchall()]


async def _reset_used_at(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> dict[str, datetime | None]:
    """Return ``{token_hash: used_at}`` for the reset-token rows belonging to *user_id*."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT token_hash, used_at FROM dataspoke.password_reset_tokens"
                " WHERE user_id = :uid"
            ),
            {"uid": str(user_id)},
        )
        return {str(row.token_hash): row.used_at for row in result.fetchall()}


async def _api_token_ids(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[str]:
    """Return the ``api_tokens`` ids belonging to *user_id*."""
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT id FROM dataspoke.api_tokens WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        return [str(row.id) for row in result.fetchall()]


async def _revoked_api_token_ids(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[str]:
    """Return the ids of *user_id*'s ``api_tokens`` rows that carry ``revoked_at``."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT id FROM dataspoke.api_tokens"
                " WHERE user_id = :uid AND revoked_at IS NOT NULL"
            ),
            {"uid": str(user_id)},
        )
        return [str(row.id) for row in result.fetchall()]


async def _current_epoch(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> int:
    """Return the row's committed ``session_epoch``."""
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT session_epoch FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        return int(result.scalar_one())


async def _password_hash(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> str | None:
    """Return the row's committed ``password_hash`` (None once a bind has cleared it)."""
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT password_hash FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        value = result.scalar_one()
        return None if value is None else str(value)


async def _password_verifies(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, password: str
) -> bool:
    """Return whether *password* matches the row's committed hash.

    Delegates to ``users.verify_password`` so the SHA-256 + bcrypt protocol stays in
    ``src/`` rather than being re-implemented here.
    """
    from src.backend.auth import users as user_service

    async with session_factory() as session:
        user = await user_service.get_by_id(session, user_id)
        assert user is not None, "the seeded row must still exist when its password is checked"
        return await user_service.verify_password(user, password)


async def _user_exists(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> bool:
    """Return whether the ``users`` row *user_id* is still committed."""
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT count(*) FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        return int(result.scalar_one()) == 1


# ── Credential seeding ────────────────────────────────────────────────────────


async def _seed_pat(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> tuple[str, str]:
    """Mint a PAT through the real ``api_tokens.mint``; return ``(raw_token, token_id)``.

    ``last_used_at`` is stamped to ``now()`` straight away — load-bearing, see the
    module docstring: a NULL ``last_used_at`` would make the contender's
    authentication ``UPDATE`` the same ``api_tokens`` row the bind holds and block on
    that instead of the ``users`` lock.
    """
    from src.backend.auth import api_tokens

    async with session_factory() as session:
        raw_token, token = await api_tokens.mint(session, user_id, "serialization-race-seeded-pat")
        await session.execute(
            text("UPDATE dataspoke.api_tokens SET last_used_at = now() WHERE id = :id"),
            {"id": str(token.id)},
        )
        await session.commit()
        return raw_token, str(token.id)


async def _seed_reset_token(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> tuple[str, str]:
    """Insert a live reset-token row; return ``(raw_token, token_hash)``.

    The hash is the SHA-256 hex digest of the raw token, per spec/feature/AUTH.md
    §Password reset — "SHA-256 hash of a random opaque token, 15-min TTL" — rather
    than an import of the module-private hasher, so a change to the stored form
    fails here instead of being silently followed.
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO dataspoke.password_reset_tokens (token_hash, user_id, expires_at)"
                " VALUES (:hash, :uid, now() + interval '15 minutes')"
            ),
            {"hash": token_hash, "uid": str(user_id)},
        )
        await session.commit()
    return raw_token, token_hash


# ── Holders: the transaction a contender must wait for ────────────────────────


@dataclass
class _Holder:
    """A session holding the ``users`` row lock uncommitted, plus its backend pid."""

    session: AsyncSession
    pid: int

    async def rollback_quietly(self) -> None:
        """Release the lock without letting a failure here mask the test's own."""
        with contextlib.suppress(Exception):
            await self.session.rollback()


@contextlib.asynccontextmanager
async def _hold_bind(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    *,
    moves_epoch: bool,
) -> AsyncGenerator[_Holder]:
    """Run the **real** ``bind_google_identity`` uncommitted; yield the session holding its lock.

    *moves_epoch* names the branch the caller expects and is asserted, so a fixture
    that drifted into the other branch fails here rather than quietly turning a
    declining test into a control: ``True`` is the credential-resetting bind
    (``bound`` and epoch ``+1``), ``False`` the write-nothing branch for a row that
    already carries the ``sub`` (not ``bound``, epoch unchanged).

    The holder is rolled back on exit, so a failed test never leaves the row locked.
    """
    from src.backend.auth.users import bind_google_identity

    async with session_factory() as session:
        holder = _Holder(session=session, pid=await _backend_pid(session))
        try:
            bind = await bind_google_identity(session, row["user_id"], str(row["google_sub"]))
            if moves_epoch:
                assert bind.bound is True, (
                    "the fixture's row is unbound, so this must be the branch that binds and "
                    "resets per spec/feature/AUTH.md §Credential reset on link"
                )
                assert bind.user.session_epoch == int(row["epoch"]) + 1, (
                    "the bind increments session_epoch by exactly one per spec/feature/AUTH.md "
                    f"§Session epoch; got {bind.user.session_epoch}"
                )
            else:
                assert bind.bound is False, (
                    "a row already carrying this sub is a login, not a bind, so nothing is "
                    "written per spec/feature/AUTH.md §Credential reset on link"
                )
                assert bind.user.session_epoch == int(row["epoch"]), (
                    "no bind, no reset, no epoch bump per spec/feature/AUTH.md §Google OAuth "
                    f"registration & login; got {bind.user.session_epoch}"
                )
            yield holder
        finally:
            await holder.rollback_quietly()


@contextlib.asynccontextmanager
async def _hold_delete(
    session_factory: async_sessionmaker[AsyncSession], row: dict[str, object]
) -> AsyncGenerator[_Holder]:
    """Run the real ``users.hard_delete`` uncommitted; yield the session holding its row lock.

    ``hard_delete`` is the repository's own hard-delete service (the one behind
    ``DELETE /admin/users/{id}``, minus the DataHub retraction the route adds around
    it), so the delete is a real one and the lock is the one a real delete takes. Only
    the lock and the row's disappearance matter to ``issue_reset_token``'s
    ``locked is None`` branch, and the DataHub side effects are not part of that claim.

    The holder is rolled back on exit, so a failed test never leaves the row locked.
    """
    from src.backend.auth import users as user_service

    async with session_factory() as session:
        holder = _Holder(session=session, pid=await _backend_pid(session))
        try:
            await user_service.hard_delete(session, row["user_id"])
            assert await user_service.get_by_id(session, row["user_id"]) is None, (
                "the holder must see its own uncommitted delete, or the race below is not "
                "about a deleted row"
            )
            yield holder
        finally:
            await holder.rollback_quietly()


# ── In-process race: issue_reset_token ────────────────────────────────────────


async def _race_reset_request(
    session_factory: async_sessionmaker[AsyncSession],
    held: _Holder,
    email: str,
    *,
    release: Literal["commit", "rollback"],
) -> _RecordingNotifier:
    """Race ``issue_reset_token`` against *held*; end the holder per *release*; return the notifier.

    The notifier's ``sent`` event proves the contender's authorising read is already
    behind it (the send precedes the lock); the contender is then observed blocked on
    exactly the holder before the holder is committed or rolled back.
    """
    from src.backend.auth.reset import issue_reset_token

    notifier = _RecordingNotifier()
    task: asyncio.Task[None] | None = None

    async with session_factory() as contender:
        try:
            contender_pid = await _backend_pid(contender)

            async def _request_a_reset() -> None:
                await issue_reset_token(contender, notifier, email)
                await contender.commit()

            task = asyncio.create_task(_request_a_reset())

            # The send happens before the lock is taken, so this is the proof that the
            # contender's authorising read is already behind it.
            try:
                await asyncio.wait_for(notifier.sent.wait(), timeout=_BLOCK_OBSERVE_BUDGET_S)
            except TimeoutError as exc:
                raise AssertionError(
                    "issue_reset_token must send the email before it takes the row lock — "
                    "'the route sends before it writes' per spec/feature/AUTH.md §Failure "
                    f"Modes; no send observed within {_BLOCK_OBSERVE_BUDGET_S}s"
                ) from exc

            waiters = await _wait_until_blocked_by(
                session_factory,
                held.pid,
                expect_pid=contender_pid,
                contender="issue_reset_token",
            )
            assert waiters == [contender_pid], (
                "the reset request must wait for the lock the holder transaction owns per "
                f"spec/feature/AUTH.md §Serialization of credential-creating writes; "
                f"waiters on backend {held.pid} were {waiters}"
            )
            assert not task.done(), (
                "the reset request must not be able to finish while the holder owns the "
                "row lock per spec/feature/AUTH.md §Serialization of credential-creating "
                "writes"
            )

            if release == "commit":
                await held.session.commit()
            else:
                await held.session.rollback()
            await asyncio.wait_for(task, timeout=_CONTENDER_COMPLETION_BUDGET_S)
        finally:
            # Release the lock first so a contender still waiting can finish; after the
            # release above this rollback is a no-op.
            await held.rollback_quietly()
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            with contextlib.suppress(Exception):
                await contender.rollback()
    return notifier


def _declined_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the ``password_reset_token_declined`` records ``issue_reset_token`` emitted."""
    return [
        record
        for record in caplog.records
        if record.name == _RESET_LOGGER and record.getMessage() == _DECLINED_EVENT
    ]


# ── POST /auth/password/reset/request ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bind_committing_mid_flight_declines_the_reset_token_insert(
    session_factory: async_sessionmaker[AsyncSession],
    unbound_user: dict[str, object],
) -> None:
    """A reset request that blocks on the bind's lock writes no token row once it lands.

    Ordering, which is the load-bearing part: the contender resolves the address and
    sends the email while the bind is still uncommitted, so its authorising read
    predates the commit. It then blocks — observed via ``pg_blocking_pids`` before
    the bind commits — and only afterwards sees the moved epoch. The decline is
    therefore the post-lock re-check, not a pre-lock read of an already-committed
    increment.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "`POST /auth/password/reset/request` | Re-compare `session_epoch` against the
    value read before the token row was prepared; if it has moved, complete
    **without** writing the token row."
    spec: spec/feature/AUTH.md §Failure Modes — "A Google bind commits while
    `POST /auth/password/reset/request` is in flight | The request declines its token
    INSERT on the epoch re-check ..., but the email has already been sent — the route
    sends before it writes."
    """
    user_id = unbound_user["user_id"]

    async with _hold_bind(session_factory, unbound_user, moves_epoch=True) as held:
        notifier = await _race_reset_request(
            session_factory, held, str(unbound_user["email"]), release="commit"
        )

    assert await _current_epoch(session_factory, user_id) == int(unbound_user["epoch"]) + 1, (
        "the bind's increment must be the committed state the re-check observed"
    )
    assert await _reset_rows(session_factory, user_id) == [], (
        "a reset request whose epoch moved under the lock completes without writing the "
        "token row per spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    assert notifier.recipients == [str(unbound_user["email"])], (
        "the email is sent before the write, so the decline does not suppress it, per "
        f"spec/feature/AUTH.md §Failure Modes; got {notifier.recipients!r}"
    )


@pytest.mark.asyncio
async def test_the_reset_token_insert_lands_when_the_epoch_holds_still(
    session_factory: async_sessionmaker[AsyncSession],
    prebound_user: dict[str, object],
) -> None:
    """The same blocked reset request writes its row when the bind supersedes nothing.

    The positive control for the declining test above. Same race, same wait on the
    same lock taken by the same ``bind_google_identity`` — only the row already
    carries the incoming ``sub``, so the bind is the branch that "writes nothing and
    emits nothing" and the epoch stands still. It is what proves that test's empty
    ``password_reset_tokens`` result comes from the epoch re-check rather than from
    the block, the notifier stub, or the fixture (spec/TESTING.md §Assertion
    Discipline — "Absence assertions require injection").

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "if it has moved, complete **without** writing the token row" (so a request whose
    epoch did not move writes it).
    spec: spec/feature/AUTH.md §Password reset — "If the email exists, DataSpoke
    writes a single-use token row (SHA-256 hash of a random opaque token, 15-min
    TTL)".
    spec: spec/feature/AUTH.md §Credential reset on link — "A callback that finds the
    row already carrying its own `sub` writes nothing and emits nothing."
    """
    user_id = prebound_user["user_id"]

    async with _hold_bind(session_factory, prebound_user, moves_epoch=False) as held:
        notifier = await _race_reset_request(
            session_factory, held, str(prebound_user["email"]), release="commit"
        )

    assert await _current_epoch(session_factory, user_id) == int(prebound_user["epoch"]), (
        "this bind supersedes nothing, so the epoch must be exactly where it started"
    )
    rows = await _reset_rows(session_factory, user_id)
    assert len(rows) == 1, (
        "a reset request that waited out the lock and found its epoch intact writes its "
        f"single-use token row per spec/feature/AUTH.md §Password reset; got {len(rows)} rows"
    )
    assert notifier.recipients == [str(prebound_user["email"])], (
        f"the token is emailed to the address of record; got {notifier.recipients!r}"
    )


# ── issue_reset_token: the owner row deleted while the request is in flight ───


@pytest.mark.asyncio
async def test_a_delete_committing_mid_flight_declines_the_reset_token_insert(
    session_factory: async_sessionmaker[AsyncSession],
    unbound_user: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reset request that blocks on a deleting transaction completes without a token row.

    The ``locked is None`` branch of ``issue_reset_token``: the contender resolved the
    address while the delete was still uncommitted, sent the email, and blocked on the
    row lock the ``DELETE`` holds. Once the delete commits, ``lock_user`` finds no row
    and the request must decline and return normally — it must neither raise (the route
    reports one outcome for every address) nor carry on to the INSERT, whose foreign
    key to the vanished row would fail. Completing cleanly is therefore itself
    discriminating, and the ``reason`` on the decline record says it was *this* branch
    that declined rather than the epoch comparison.

    No spec line names this branch directly; it is the corollary of "complete
    **without** writing the token row" for an owner who no longer exists, and of
    spec/feature/AUTH.md §Failure Modes — "`POST /auth/password/reset/confirm` with a
    token whose `users` row has been hard-deleted | Resolves as the route's ordinary
    invalid-token outcome; no existence signal is emitted." The
    ``password_reset_token_declined`` / ``user_deleted`` record is the implementation's
    own contract (``src/backend/auth/reset.py``), not a spec'd one.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "`POST /auth/password/reset/request` | ... complete **without** writing the token
    row."
    spec: spec/feature/AUTH.md §Deletion — "The DataSpoke `users` row is removed
    physically (hard delete)".
    """
    user_id = unbound_user["user_id"]
    email = str(unbound_user["email"])

    with caplog.at_level(logging.INFO, logger=_RESET_LOGGER):
        async with _hold_delete(session_factory, unbound_user) as held:
            notifier = await _race_reset_request(session_factory, held, email, release="commit")

    assert not await _user_exists(session_factory, user_id), (
        "the delete committed, so the row the request was racing no longer exists"
    )
    assert await _reset_rows(session_factory, user_id) == [], (
        "a reset request whose owner row was deleted under the lock writes no token row per "
        "spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    assert notifier.recipients == [email], (
        "the email is sent before the lock, so the decline does not suppress it; "
        f"got {notifier.recipients!r}"
    )
    declined = _declined_records(caplog)
    assert len(declined) == 1, (
        f"exactly one `{_DECLINED_EVENT}` record must report the decline; got "
        f"{[(r.name, r.getMessage()) for r in caplog.records]!r}"
    )
    assert getattr(declined[0], "reason", None) == "user_deleted", (
        "the decline must name the deleted owner, not a moved epoch; got "
        f"{getattr(declined[0], 'reason', None)!r}"
    )


@pytest.mark.asyncio
async def test_the_reset_token_insert_lands_when_the_delete_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    unbound_user: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same blocked reset request writes its row when the deleting transaction rolls back.

    The positive control for the deleted-user decline above. Same race, same wait, same
    ``DELETE`` holding the same lock — the holder simply rolls back, so the contender's
    ``lock_user`` returns the row with its epoch intact. It is what proves the decline
    above comes from the row being *gone* rather than from the block, the notifier
    stub, or the fixture (spec/TESTING.md §Assertion Discipline — "Absence assertions
    require injection"), and that no ``user_deleted`` record is emitted when nothing
    was deleted.

    spec: spec/feature/AUTH.md §Password reset — "If the email exists, DataSpoke
    writes a single-use token row (SHA-256 hash of a random opaque token, 15-min
    TTL)".
    """
    user_id = unbound_user["user_id"]
    email = str(unbound_user["email"])

    with caplog.at_level(logging.INFO, logger=_RESET_LOGGER):
        async with _hold_delete(session_factory, unbound_user) as held:
            notifier = await _race_reset_request(session_factory, held, email, release="rollback")

    assert await _user_exists(session_factory, user_id), (
        "the delete rolled back, so the row must still exist"
    )
    assert await _current_epoch(session_factory, user_id) == int(unbound_user["epoch"]), (
        "nothing was bound, so the epoch must be exactly where it started"
    )
    rows = await _reset_rows(session_factory, user_id)
    assert len(rows) == 1, (
        "a reset request that waited out a rolled-back delete writes its single-use token row "
        f"per spec/feature/AUTH.md §Password reset; got {len(rows)} rows"
    )
    assert notifier.recipients == [email], (
        f"the token is emailed to the address of record; got {notifier.recipients!r}"
    )
    assert _declined_records(caplog) == [], (
        f"a request that wrote its row must not log `{_DECLINED_EVENT}`; got "
        f"{[(r.name, r.getMessage()) for r in caplog.records]!r}"
    )


# ── HTTP races: PATCH /auth/me, POST /auth/api-tokens, POST /auth/password/reset/confirm ──


@dataclass(frozen=True)
class _Armed:
    """What a row's seeding produced — the request's credentials and what to read back."""

    headers: dict[str, str]
    body: dict[str, object]
    pat_id: str | None = None
    reset_token_hash: str | None = None


_StateCheck = Callable[
    [async_sessionmaker[AsyncSession], dict[str, object], _Armed, httpx.Response], Awaitable[None]
]


@dataclass(frozen=True)
class _HttpWrite:
    """One row of the spec table (or its PAT variant), as an HTTP request plus its read-backs.

    ``build_body`` receives the carrier's raw secret (the reset token for
    ``reset_token``, ``""`` otherwise). ``refusal`` is the ``(status, error_code)`` the
    re-check raises; ``landed_status`` is what the same request returns when the epoch
    holds still. The two state checks read back the concrete side effect of each
    outcome (spec/TESTING.md §Assertion Discipline — "Mutation tests verify a concrete
    side effect").
    """

    id: str
    carrier: Literal["jwt", "pat", "reset_token"]
    method: str
    path: str
    build_body: Callable[[str], dict[str, object]]
    refusal: tuple[int, str]
    landed_status: int
    spec_anchor: str
    assert_refused_state: _StateCheck
    assert_landed_state: _StateCheck

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


async def _arm(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    write: _HttpWrite,
) -> _Armed:
    """Seed the credential *write* is carried by, before any holder exists.

    Seeding precedes the bind so the bind's credential reset has something to
    supersede: the PAT it revokes, the reset-token row it deletes.
    """
    user_id = row["user_id"]
    if write.carrier == "jwt":
        return _Armed(
            headers={"Authorization": f"Bearer {row['access_token']}"},
            body=write.build_body(""),
        )
    if write.carrier == "pat":
        raw_token, token_id = await _seed_pat(session_factory, user_id)
        return _Armed(
            headers={"Authorization": f"Bearer {raw_token}"},
            body=write.build_body(""),
            pat_id=token_id,
        )
    raw_token, token_hash = await _seed_reset_token(session_factory, user_id)
    return _Armed(headers={}, body=write.build_body(raw_token), reset_token_hash=token_hash)


async def _dispatch_and_pin(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    held: _Holder,
    write: _HttpWrite,
    armed: _Armed,
) -> httpx.Response:
    """Send *write*, observe it blocked on the holder's ``users`` lock, then commit the holder.

    The pre-lock and post-lock failures are byte-identical for every row, so this is
    where the test earns the right to call a refusal "the re-check": nothing waits on
    the holder before dispatch, exactly one backend waits afterwards, that backend is
    stuck on a ``FOR UPDATE`` against ``dataspoke.users``, and the request is still in
    flight while it waits.
    """
    task: asyncio.Task[httpx.Response] | None = None
    try:
        # Nothing is waiting on the holder yet, so the single waiter observed after
        # dispatch can only be this request.
        assert await _waiters_snapshot(session_factory, held.pid) == [], (
            f"no backend may already be waiting on the holder before {write.label} is "
            "dispatched, or the observation below could not identify the request"
        )

        task = asyncio.create_task(
            api_client.request(
                write.method,
                write.path,
                json=armed.body,
                headers=armed.headers,
                timeout=_HTTP_RACE_TIMEOUT_S,
            )
        )

        waiters = await _wait_until_blocked_by(
            session_factory, held.pid, expect_pid=None, contender=write.label
        )
        assert len(waiters) == 1, (
            f"exactly one backend — {write.label} — must be waiting on the holder per "
            f"spec/feature/AUTH.md §Serialization of credential-creating writes; got {waiters}"
        )
        waiting_query = await _executing_query(session_factory, waiters[0])
        assert "dataspoke.users" in waiting_query and "FOR UPDATE" in waiting_query.upper(), (
            f"{write.label} must be waiting on the users row lock specifically per "
            f"spec/feature/AUTH.md §Serialization of credential-creating writes; backend "
            f"{waiters[0]} is running {waiting_query!r}"
        )
        assert not task.done(), (
            f"{write.label} must not be able to finish while the holder owns the row lock "
            "per spec/feature/AUTH.md §Serialization of credential-creating writes"
        )

        await held.session.commit()
        return await asyncio.wait_for(task, timeout=_CONTENDER_COMPLETION_BUDGET_S)
    finally:
        await held.rollback_quietly()
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task


# Refusal / landed read-backs, one pair per row.


async def _refused_mint_by_jwt(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    assert await _api_token_ids(session_factory, row["user_id"]) == [], (
        "a refused mint commits no credential per spec/feature/AUTH.md §Serialization of "
        "credential-creating writes"
    )


async def _landed_mint_by_jwt(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    body = resp.json()
    assert body["token"].startswith("dsk_"), (
        "the raw token is returned once, in the `dsk_` form, per spec/feature/AUTH.md "
        f"§Token format and storage; got {body!r}"
    )
    persisted = await _api_token_ids(session_factory, row["user_id"])
    assert persisted == [body["id"]], (
        "the mint commits exactly the token row it reported per spec/feature/AUTH.md "
        f"§API Tokens; got {persisted!r}"
    )


async def _refused_mint_by_pat(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    assert armed.pat_id is not None, "the PAT-carried row must have seeded its PAT"
    assert await _api_token_ids(session_factory, row["user_id"]) == [armed.pat_id], (
        "a refused PAT-carried mint commits no new credential — only the seeded PAT may "
        "exist — per spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    assert await _revoked_api_token_ids(session_factory, row["user_id"]) == [armed.pat_id], (
        "the bind's credential reset must have revoked the PAT that authorised the request, "
        "which is what the re-read under the lock saw, per spec/feature/AUTH.md §Credential "
        "reset on link"
    )


async def _landed_mint_by_pat(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    assert armed.pat_id is not None, "the PAT-carried row must have seeded its PAT"
    body = resp.json()
    assert body["token"].startswith("dsk_"), (
        "the raw token is returned once, in the `dsk_` form, per spec/feature/AUTH.md "
        f"§Token format and storage; got {body!r}"
    )
    persisted = await _api_token_ids(session_factory, row["user_id"])
    assert sorted(persisted) == sorted([armed.pat_id, body["id"]]), (
        "the mint commits exactly the token row it reported, beside the PAT that authorised "
        f"it, per spec/feature/AUTH.md §API Tokens; got {persisted!r}"
    )
    assert await _revoked_api_token_ids(session_factory, row["user_id"]) == [], (
        "a bind that supersedes nothing revokes nothing, so the authorising PAT must still "
        "be active per spec/feature/AUTH.md §Credential reset on link"
    )


async def _refused_password_write(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    # The bind cleared `password_hash`, so a password write that leaked past the
    # re-check would leave it non-NULL — the injection that makes this absence real.
    assert await _password_hash(session_factory, row["user_id"]) is None, (
        "a refused password write commits no credential — the bind cleared the hash and "
        "nothing may have set it again — per spec/feature/AUTH.md §Serialization of "
        "credential-creating writes"
    )


async def _landed_password_write(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    assert await _password_verifies(session_factory, row["user_id"], NEW_PASSWORD), (
        "the write that waited out the lock and found its authorisation intact replaces "
        "the password hash per spec/feature/AUTH.md §Profile read & update"
    )
    assert not await _password_verifies(session_factory, row["user_id"], SEEDED_PASSWORD), (
        "the seeded password must no longer verify once the hash was replaced"
    )
    if armed.pat_id is not None:
        assert await _revoked_api_token_ids(session_factory, row["user_id"]) == [], (
            "a bind that supersedes nothing revokes nothing, so the authorising PAT must "
            "still be active per spec/feature/AUTH.md §Credential reset on link"
        )


async def _refused_confirm(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    assert armed.reset_token_hash is not None, "the confirm row must have seeded its reset token"
    assert await _password_hash(session_factory, row["user_id"]) is None, (
        "a refused confirm commits no credential — the bind cleared the hash and nothing may "
        "have set it again — per spec/feature/AUTH.md §Serialization of credential-creating "
        "writes"
    )
    assert await _reset_used_at(session_factory, row["user_id"]) == {}, (
        "the bind's delete removed the unused reset token the confirm was authorised by, and "
        "the refused confirm must not have resurrected or consumed it, per "
        "spec/feature/AUTH.md §Serialization of credential-creating writes"
    )


async def _landed_confirm(
    session_factory: async_sessionmaker[AsyncSession],
    row: dict[str, object],
    armed: _Armed,
    resp: httpx.Response,
) -> None:
    assert armed.reset_token_hash is not None, "the confirm row must have seeded its reset token"
    assert await _password_verifies(session_factory, row["user_id"], NEW_PASSWORD), (
        "a confirm whose token survived the lock writes the new password hash per "
        "spec/feature/AUTH.md §Password reset"
    )
    used = await _reset_used_at(session_factory, row["user_id"])
    assert list(used) == [armed.reset_token_hash], (
        f"the confirm must leave exactly the token it consumed behind; got {sorted(used)!r}"
    )
    assert used[armed.reset_token_hash] is not None, (
        "the confirm marks its token used so it cannot be replayed, per "
        'spec/feature/AUTH.md §Password reset — "marks the token used"'
    )


def _mint_body(_secret: str) -> dict[str, object]:
    return {"name": "serialization-race-mint"}


def _password_body(_secret: str) -> dict[str, object]:
    return {"password": NEW_PASSWORD}


def _confirm_body(secret: str) -> dict[str, object]:
    return {"token": secret, "new_password": NEW_PASSWORD}


_HTTP_WRITES: list[_HttpWrite] = [
    _HttpWrite(
        id="api-tokens/jwt",
        carrier="jwt",
        method="POST",
        path="/api/v1/auth/api-tokens",
        build_body=_mint_body,
        refusal=(401, "UNAUTHORIZED"),
        landed_status=201,
        spec_anchor=(
            "spec/feature/AUTH.md §Serialization of credential-creating writes — "
            '"`POST /auth/api-tokens` | Same `ses` re-comparison.", whose refusal is the '
            '`PATCH /auth/me` row\'s "mismatch → `401 UNAUTHORIZED`"'
        ),
        assert_refused_state=_refused_mint_by_jwt,
        assert_landed_state=_landed_mint_by_jwt,
    ),
    _HttpWrite(
        id="api-tokens/pat",
        carrier="pat",
        method="POST",
        path="/api/v1/auth/api-tokens",
        build_body=_mint_body,
        refusal=(401, "TOKEN_REVOKED"),
        landed_status=201,
        spec_anchor=(
            "spec/feature/AUTH.md §Serialization of credential-creating writes — a PAT-carried "
            'request "re-reads its own `api_tokens` row under the same `users` row lock and '
            'fails `401 TOKEN_REVOKED` once the reset has revoked it"'
        ),
        assert_refused_state=_refused_mint_by_pat,
        assert_landed_state=_landed_mint_by_pat,
    ),
    _HttpWrite(
        id="me-password/jwt",
        carrier="jwt",
        method="PATCH",
        path="/api/v1/auth/me",
        build_body=_password_body,
        refusal=(401, "UNAUTHORIZED"),
        landed_status=200,
        spec_anchor=(
            "spec/feature/AUTH.md §Serialization of credential-creating writes — "
            "\"`PATCH /auth/me` (`password`) | Re-compare the request's `ses` claim against "
            'the freshly read `session_epoch`; mismatch → `401 UNAUTHORIZED`."'
        ),
        assert_refused_state=_refused_password_write,
        assert_landed_state=_landed_password_write,
    ),
    _HttpWrite(
        id="me-password/pat",
        carrier="pat",
        method="PATCH",
        path="/api/v1/auth/me",
        build_body=_password_body,
        refusal=(401, "TOKEN_REVOKED"),
        landed_status=200,
        spec_anchor=(
            "spec/feature/AUTH.md §Serialization of credential-creating writes — "
            '"`PATCH /auth/me` and `POST /auth/api-tokens` are equally reachable with an API '
            "token ... it instead re-reads its own `api_tokens` row under the same `users` row "
            'lock and fails `401 TOKEN_REVOKED`"'
        ),
        assert_refused_state=_refused_password_write,
        assert_landed_state=_landed_password_write,
    ),
    _HttpWrite(
        id="reset-confirm",
        carrier="reset_token",
        method="POST",
        path="/api/v1/auth/password/reset/confirm",
        build_body=_confirm_body,
        refusal=(400, "INVALID_RESET_TOKEN"),
        landed_status=204,
        spec_anchor=(
            "spec/feature/AUTH.md §Serialization of credential-creating writes — "
            '"`POST /auth/password/reset/confirm` | Re-read the `password_reset_tokens` row, '
            "which the bind's delete has already removed; missing or used → the route's "
            'existing invalid-token failure." (`400 INVALID_RESET_TOKEN`, spec/API.md §Application '
            "Error Codes)"
        ),
        assert_refused_state=_refused_confirm,
        assert_landed_state=_landed_confirm,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _HTTP_WRITES, ids=[w.id for w in _HTTP_WRITES])
async def test_a_bind_committing_mid_flight_refuses_the_credential_write(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    unbound_user: dict[str, object],
    write: _HttpWrite,
) -> None:
    """A credential-creating request that blocks on the bind's lock is refused and persists nothing.

    Driven over HTTP so the route's own wiring to its re-check is what runs — a patched
    helper would prove only that a name was called, not that the route acts on its
    failure.

    Ordering: the request's credential — bearer JWT, PAT, or reset-token row — is
    authenticated before the bind commits, since the request can only reach the row
    lock afterwards. That is asserted, not assumed, and it has to be: the pre-lock
    authorisation gate and the post-lock re-check raise the same failure with the same
    message, so no field of the response can tell them apart. The bind is therefore
    probed to be blocking **nobody** before the request is dispatched, and the single
    waiter that appears afterwards is checked to be stuck on a ``FOR UPDATE`` against
    ``dataspoke.users``. The refusal is then the re-check under the lock.

    The refusal's state check reads back the concrete side effect — no new
    ``api_tokens`` row for a mint, ``password_hash`` still NULL for a password write
    (the bind cleared it, so a leaked write would be visible), the reset-token row
    still gone for a confirm — and the epoch must stand at the bind's increment.

    spec: the row's ``spec_anchor`` — see :data:`_HTTP_WRITES`.
    """
    user_id = unbound_user["user_id"]
    armed = await _arm(session_factory, unbound_user, write)

    async with _hold_bind(session_factory, unbound_user, moves_epoch=True) as held:
        resp = await _dispatch_and_pin(api_client, session_factory, held, write, armed)

    status, error_code = write.refusal
    assert resp.status_code == status, (
        f"a {write.label} whose authorisation the bind superseded under the lock is refused "
        f"per {write.spec_anchor}; got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == error_code, (
        f"the re-check reports the refusal as `{status} {error_code}` per {write.spec_anchor}; "
        f"got {resp.json()!r}"
    )
    assert await _current_epoch(session_factory, user_id) == int(unbound_user["epoch"]) + 1, (
        "the bind's increment must be the committed state the re-check observed"
    )
    await write.assert_refused_state(session_factory, unbound_user, armed, resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _HTTP_WRITES, ids=[w.id for w in _HTTP_WRITES])
async def test_the_credential_write_lands_when_the_epoch_holds_still(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    prebound_user: dict[str, object],
    write: _HttpWrite,
) -> None:
    """The same blocked request succeeds when the bind supersedes nothing.

    The positive control for the refusal above: same request, same carrier, same wait
    on the same lock taken by the same ``bind_google_identity`` — only the row already
    carries the incoming ``sub``, so the epoch stands still and nothing the request
    was authorised by is revoked or deleted. Without it, the refusal test's absence
    assertions would also pass against a route that refuses every request, or against
    a fixture whose credential never authenticated in the first place (spec/TESTING.md
    §Assertion Discipline — "Absence assertions require injection").

    spec: spec/feature/AUTH.md §API Tokens §Lifecycle endpoints — "`POST
    /auth/api-tokens` | Mint a new token (body `{name, expires_at?}`); response
    includes the raw token in `{token: "dsk_..."}` — only time it is returned plain".
    spec: spec/API.md — `PATCH /auth/me` "returns the updated profile", `POST
    /auth/password/reset/confirm` "returns `204` on success".
    spec: spec/feature/AUTH.md §Password reset — "`POST /auth/password/reset/confirm`
    consumes `{token, new_password}`, validates the token ..., writes the new bcrypt hash,
    and marks the token used."
    """
    user_id = prebound_user["user_id"]
    armed = await _arm(session_factory, prebound_user, write)

    async with _hold_bind(session_factory, prebound_user, moves_epoch=False) as held:
        resp = await _dispatch_and_pin(api_client, session_factory, held, write, armed)

    assert resp.status_code == write.landed_status, (
        f"a {write.label} that waited out the lock and found its authorisation intact "
        f"succeeds per {write.spec_anchor}; got {resp.status_code}: {resp.text}"
    )
    assert await _current_epoch(session_factory, user_id) == int(prebound_user["epoch"]), (
        "this bind supersedes nothing, so the epoch must be exactly where it started"
    )
    await write.assert_landed_state(session_factory, prebound_user, armed, resp)
