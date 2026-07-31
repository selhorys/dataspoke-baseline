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

Two of the four spec'd rows are covered here, each with its positive control:

- ``POST /auth/password/reset/request``, driven in-process through
  ``reset.issue_reset_token`` — the epoch decline is invisible in the route's
  response, which "still returns `204`, unchanged, since the route reports the same
  outcome for known and unknown emails and must not become an oracle for account
  state".
- ``POST /auth/api-tokens``, driven over **HTTP** against the in-cluster API so the
  route's own wiring to ``revalidate_under_user_lock`` is what is under test rather
  than a patched helper. AUTH.md singles this row out: "Needed here in particular
  because the API-token authentication path runs no epoch check, so a token
  committed after the reset would otherwise stay live."

The other side of every race is the **real** ``users.bind_google_identity`` running
uncommitted in a second session, not a hand-rolled stand-in: the sentence under test
names "the lock **the bind transaction holds**", so the bind is what must hold it.
The declining tests give it an unbound row — the branch that takes the lock, clears
the row's credentials and increments ``session_epoch``. The controls give it a row
already carrying the same ``sub``, which is the branch that "writes nothing and
emits nothing" while still holding the lock for its transaction's life.

Blocked-ness is *observed*, not assumed: ``pg_blocking_pids`` is polled from a third
connection until a backend is seen waiting on the bind, and the budget's exhaustion
is a failure rather than a skip (spec/TESTING.md §Assertion Discipline — "a wait
that exhausts its budget is a failure, not a skip"). The HTTP races additionally
probe that the bind blocks *nobody* before the request is dispatched and require
exactly one waiter afterwards, because the mint's pre-lock authentication gate and
its post-lock re-check raise byte-identical `401 UNAUTHORIZED` failures — the
identity of the waiter is the only thing that can tell them apart.

That observation is what fixes the ordering each test depends on: the contender's
authorising read necessarily completed *before* the bind committed, because the
contender can only reach the lock after that read passed. A refusal afterwards is
therefore the post-lock re-check catching superseded state — not the ordinary
pre-lock check seeing an already-committed epoch.

The controls — the variants where the bind commits without moving the epoch — are
not decoration. They are what makes the absence assertions non-vacuous
(spec/TESTING.md §Assertion Discipline — "Absence assertions require injection"):
without them, "no row was written" would also pass if the block itself, or a broken
fixture, had killed the write.

Not covered here, and deliberately so — the remaining rows of the same spec table
need their own construction: ``PATCH /auth/me`` (`password`),
``POST /auth/password/reset/confirm``, the PAT-carried variant that must fail
`401 TOKEN_REVOKED` under the same lock, and ``issue_reset_token``'s deleted-user
branch.

spec: spec/feature/AUTH.md §Serialization of credential-creating writes
spec: spec/feature/AUTH.md §Session epoch
spec: spec/feature/AUTH.md §Failure Modes
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import pool as sa_pool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The seeded row's password — held only here in plaintext; the bcrypt protocol stays
# inside src/backend/auth/users.create_user.
SEEDED_PASSWORD = "serialization-password-1"

# Bounded so a genuinely stuck lock fails the test instead of hanging pytest. The
# contender reaches the lock in well under a second against the dev cluster; the
# budget is slack for a laptop→cluster round trip, not a tuning knob.
_BLOCK_OBSERVE_BUDGET_S = 20.0
_BLOCK_POLL_INTERVAL_S = 0.1
_CONTENDER_COMPLETION_BUDGET_S = 30.0

# The mint request must outlive the whole race: the window in which its block is
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
    same role the API pod uses — ``DATASPOKE_TEST_POSTGRES_USER`` is populated from
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
    integration_db_url: str,
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

    The access token is the JWT credential whose ``ses`` claim the two
    ``/auth/api-tokens`` tests have re-compared under the lock. ``api_tokens`` and
    ``password_reset_tokens`` follow the row by CASCADE.
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
    from src.backend.auth.reset import issue_reset_token
    from src.backend.auth.users import bind_google_identity

    user_id = unbound_user["user_id"]
    notifier = _RecordingNotifier()
    task: asyncio.Task[None] | None = None

    async with session_factory() as holder, session_factory() as contender:
        try:
            holder_pid = await _backend_pid(holder)
            contender_pid = await _backend_pid(contender)

            # The Google bind's credential reset, mid-transaction: the real thing,
            # holding the users row lock and having incremented session_epoch, but
            # not yet committed.
            bind = await bind_google_identity(holder, user_id, str(unbound_user["google_sub"]))
            assert bind.bound is True, (
                "the fixture's row is unbound, so this must be the branch that binds and "
                "resets per spec/feature/AUTH.md §Credential reset on link"
            )
            assert bind.user.session_epoch == int(unbound_user["epoch"]) + 1, (
                "the bind increments session_epoch by exactly one per spec/feature/AUTH.md "
                f"§Session epoch; got {bind.user.session_epoch}"
            )

            async def _request_a_reset() -> None:
                await issue_reset_token(contender, notifier, str(unbound_user["email"]))
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
                holder_pid,
                expect_pid=contender_pid,
                contender="issue_reset_token",
            )
            assert waiters == [contender_pid], (
                "the reset request must wait for the lock the bind transaction holds per "
                f"spec/feature/AUTH.md §Serialization of credential-creating writes; "
                f"waiters on backend {holder_pid} were {waiters}"
            )
            assert not task.done(), (
                "the reset request must not be able to finish while the bind holds the "
                "row lock per spec/feature/AUTH.md §Serialization of credential-creating "
                "writes"
            )

            await holder.commit()
            await asyncio.wait_for(task, timeout=_CONTENDER_COMPLETION_BUDGET_S)
        finally:
            # Release the lock first so a contender still waiting can finish; after a
            # successful commit above this rollback is a no-op.
            with contextlib.suppress(Exception):
                await holder.rollback()
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            with contextlib.suppress(Exception):
                await contender.rollback()

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
    from src.backend.auth.reset import issue_reset_token
    from src.backend.auth.users import bind_google_identity

    user_id = prebound_user["user_id"]
    notifier = _RecordingNotifier()
    task: asyncio.Task[None] | None = None

    async with session_factory() as holder, session_factory() as contender:
        try:
            holder_pid = await _backend_pid(holder)
            contender_pid = await _backend_pid(contender)

            bind = await bind_google_identity(holder, user_id, str(prebound_user["google_sub"]))
            assert bind.bound is False, (
                "a row already carrying this sub is a login, not a bind, so nothing is "
                "written per spec/feature/AUTH.md §Credential reset on link"
            )
            assert bind.user.session_epoch == int(prebound_user["epoch"]), (
                "no bind, no reset, no epoch bump per spec/feature/AUTH.md §Google OAuth "
                f"registration & login; got {bind.user.session_epoch}"
            )

            async def _request_a_reset() -> None:
                await issue_reset_token(contender, notifier, str(prebound_user["email"]))
                await contender.commit()

            task = asyncio.create_task(_request_a_reset())

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
                holder_pid,
                expect_pid=contender_pid,
                contender="issue_reset_token",
            )
            assert waiters == [contender_pid], (
                "the reset request must wait for the lock the bind transaction holds per "
                f"spec/feature/AUTH.md §Serialization of credential-creating writes; "
                f"waiters on backend {holder_pid} were {waiters}"
            )
            assert not task.done(), (
                "the reset request must not be able to finish while the bind holds the row lock"
            )

            await holder.commit()
            await asyncio.wait_for(task, timeout=_CONTENDER_COMPLETION_BUDGET_S)
        finally:
            with contextlib.suppress(Exception):
                await holder.rollback()
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            with contextlib.suppress(Exception):
                await contender.rollback()

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


# ── POST /auth/api-tokens ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bind_committing_mid_flight_refuses_the_api_token_mint(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    unbound_user: dict[str, object],
) -> None:
    """A mint request that blocks on the bind's lock is refused and persists no token.

    Driven over HTTP so the route's own call to ``revalidate_under_user_lock`` is
    what runs — a patched helper would prove only that a name was called, not that
    the route acts on its failure.

    Ordering: the request's bearer JWT is authenticated (which reads
    ``session_epoch``) before the bind commits, since the request can only reach the
    row lock afterwards. That is asserted, not assumed, and it has to be: the
    pre-lock authentication gate and the post-lock re-check raise the same
    `401 UNAUTHORIZED` with the same message, so no field of the response can tell
    them apart. The bind is therefore probed to be blocking **nobody** before the
    request is dispatched, and the single waiter that appears afterwards is checked
    to be stuck on a ``FOR UPDATE`` against ``dataspoke.users``. The 401 is then the
    re-check under the lock.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "`POST /auth/api-tokens` | Same `ses` re-comparison. Needed here in particular
    because the API-token authentication path runs no epoch check, so a token
    committed after the reset would otherwise stay live."
    The refusal code comes from the row this one says it repeats — "Same `ses`
    re-comparison" points at spec/feature/AUTH.md §Serialization of
    credential-creating writes, "`PATCH /auth/me` (`password`) | Re-compare the
    request's `ses` claim against the freshly read `session_epoch`; mismatch →
    `401 UNAUTHORIZED`."
    """
    from src.backend.auth.users import bind_google_identity

    user_id = unbound_user["user_id"]
    task: asyncio.Task[httpx.Response] | None = None

    async with session_factory() as holder:
        try:
            holder_pid = await _backend_pid(holder)

            bind = await bind_google_identity(holder, user_id, str(unbound_user["google_sub"]))
            assert bind.bound is True, (
                "the fixture's row is unbound, so this must be the branch that binds and "
                "resets per spec/feature/AUTH.md §Credential reset on link"
            )
            assert bind.user.session_epoch == int(unbound_user["epoch"]) + 1, (
                "the bind increments session_epoch by exactly one per spec/feature/AUTH.md "
                f"§Session epoch; got {bind.user.session_epoch}"
            )

            # Nothing is waiting on the bind yet, so the single waiter observed after
            # dispatch can only be the mint.
            assert await _waiters_snapshot(session_factory, holder_pid) == [], (
                "no backend may already be waiting on the bind before the mint is "
                "dispatched, or the observation below could not identify the mint"
            )

            task = asyncio.create_task(
                api_client.post(
                    "/api/v1/auth/api-tokens",
                    json={"name": "mint-racing-a-google-bind"},
                    headers={"Authorization": f"Bearer {unbound_user['access_token']}"},
                    timeout=_HTTP_RACE_TIMEOUT_S,
                )
            )

            waiters = await _wait_until_blocked_by(
                session_factory,
                holder_pid,
                expect_pid=None,
                contender="POST /auth/api-tokens",
            )
            assert len(waiters) == 1, (
                "exactly one backend — the mint — must be waiting on the bind per "
                f"spec/feature/AUTH.md §Serialization of credential-creating writes; got "
                f"{waiters}"
            )
            waiting_query = await _executing_query(session_factory, waiters[0])
            assert "dataspoke.users" in waiting_query and "FOR UPDATE" in waiting_query.upper(), (
                "the mint must be waiting on the users row lock specifically per "
                "spec/feature/AUTH.md §Serialization of credential-creating writes; backend "
                f"{waiters[0]} is running {waiting_query!r}"
            )
            assert not task.done(), (
                "the mint must not be able to finish while the bind holds the row lock "
                "per spec/feature/AUTH.md §Serialization of credential-creating writes"
            )

            await holder.commit()
            resp = await asyncio.wait_for(task, timeout=_CONTENDER_COMPLETION_BUDGET_S)
        finally:
            with contextlib.suppress(Exception):
                await holder.rollback()
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task

    assert resp.status_code == 401, (
        "a mint whose session epoch moved under the lock is refused per "
        f"spec/feature/AUTH.md §Serialization of credential-creating writes; got "
        f"{resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "UNAUTHORIZED", (
        "the re-check reports the mismatch as `401 UNAUTHORIZED` per spec/feature/AUTH.md "
        f"§Serialization of credential-creating writes; got {resp.json()!r}"
    )
    assert await _current_epoch(session_factory, user_id) == int(unbound_user["epoch"]) + 1, (
        "the bind's increment must be the committed state the re-check observed"
    )
    assert await _api_token_ids(session_factory, user_id) == [], (
        "a refused mint commits no credential per spec/feature/AUTH.md §Serialization of "
        "credential-creating writes"
    )


@pytest.mark.asyncio
async def test_the_api_token_mint_lands_when_the_epoch_holds_still(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    prebound_user: dict[str, object],
) -> None:
    """The same blocked mint succeeds when the bind supersedes nothing.

    The positive control for the refusal above: same request, same wait on the same
    lock taken by the same ``bind_google_identity`` — only the row already carries
    the incoming ``sub``, so the epoch stands still. Without it, the refusal test's
    empty ``api_tokens`` result would also pass against a route that refuses every
    mint, or against a fixture whose JWT never authenticated in the first place
    (spec/TESTING.md §Assertion Discipline — "Absence assertions require injection").

    spec: spec/feature/AUTH.md §API Tokens §Lifecycle endpoints — "`POST
    /auth/api-tokens` | Mint a new token (body `{name, expires_at?}`); response
    includes the raw token in `{token: "dsk_..."}` — only time it is returned plain".
    spec: spec/feature/AUTH.md §Token format and storage — "Opaque random tokens of
    the form `dsk_<32 url-safe random bytes>`".
    """
    from src.backend.auth.users import bind_google_identity

    user_id = prebound_user["user_id"]
    task: asyncio.Task[httpx.Response] | None = None

    async with session_factory() as holder:
        try:
            holder_pid = await _backend_pid(holder)

            bind = await bind_google_identity(holder, user_id, str(prebound_user["google_sub"]))
            assert bind.bound is False, (
                "a row already carrying this sub is a login, not a bind, so nothing is "
                "written per spec/feature/AUTH.md §Credential reset on link"
            )
            assert bind.user.session_epoch == int(prebound_user["epoch"]), (
                "no bind, no reset, no epoch bump per spec/feature/AUTH.md §Google OAuth "
                f"registration & login; got {bind.user.session_epoch}"
            )

            assert await _waiters_snapshot(session_factory, holder_pid) == [], (
                "no backend may already be waiting on the bind before the mint is "
                "dispatched, or the observation below could not identify the mint"
            )

            task = asyncio.create_task(
                api_client.post(
                    "/api/v1/auth/api-tokens",
                    json={"name": "mint-behind-a-harmless-lock"},
                    headers={"Authorization": f"Bearer {prebound_user['access_token']}"},
                    timeout=_HTTP_RACE_TIMEOUT_S,
                )
            )

            waiters = await _wait_until_blocked_by(
                session_factory,
                holder_pid,
                expect_pid=None,
                contender="POST /auth/api-tokens",
            )
            assert len(waiters) == 1, (
                "exactly one backend — the mint — must be waiting on the bind per "
                f"spec/feature/AUTH.md §Serialization of credential-creating writes; got "
                f"{waiters}"
            )
            waiting_query = await _executing_query(session_factory, waiters[0])
            assert "dataspoke.users" in waiting_query and "FOR UPDATE" in waiting_query.upper(), (
                "the mint must be waiting on the users row lock specifically per "
                "spec/feature/AUTH.md §Serialization of credential-creating writes; backend "
                f"{waiters[0]} is running {waiting_query!r}"
            )
            assert not task.done(), (
                "the mint must not be able to finish while the bind holds the row lock"
            )

            await holder.commit()
            resp = await asyncio.wait_for(task, timeout=_CONTENDER_COMPLETION_BUDGET_S)
        finally:
            with contextlib.suppress(Exception):
                await holder.rollback()
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task

    assert resp.status_code == 201, (
        "a mint that waited out the lock and found its session epoch intact succeeds per "
        f"spec/feature/AUTH.md §API Tokens; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["token"].startswith("dsk_"), (
        "the raw token is returned once, in the `dsk_` form, per spec/feature/AUTH.md "
        f"§Token format and storage; got {body!r}"
    )
    assert await _current_epoch(session_factory, user_id) == int(prebound_user["epoch"]), (
        "this bind supersedes nothing, so the epoch must be exactly where it started"
    )
    persisted = await _api_token_ids(session_factory, user_id)
    assert persisted == [body["id"]], (
        "the mint commits exactly the token row it reported per spec/feature/AUTH.md "
        f"§API Tokens; got {persisted!r}"
    )
