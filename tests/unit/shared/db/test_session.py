"""Tests for src/shared/db/session.py — the async session factory contracts from
spec/feature/BACKEND.md §Shared Services (PostgreSQL row): pool size 10, max overflow 5,
asyncpg driver, and the connection URL built from the ``DATASPOKE_POSTGRES_*`` env vars.

The credential assertions do not stop at the ``URL`` the module builds: they push one
layer further and read back what the asyncpg dialect would hand the driver
(``create_connect_args``), because "reach the driver verbatim" is a statement about the
driver's arguments, not about the shape of the object in between. Anything that
re-introduces a DSN round trip — here or in a future refactor — changes those arguments
and fails these tests.
"""

import importlib
import inspect as stdlib_inspect
import os
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.db.session import SessionLocal, _build_url, engine, get_session

# Characters an operator may legally put in a PostgreSQL credential that a DSN string
# would have to escape. Each entry is a distinct corruption mode of the string-interpolated
# DSN this module no longer builds:
#   "p@ss"     — the `@` split the DSN, so the host became "ss@db.example.com" (issue #120)
#   "p%2Fss"   — a literal `%2F` was decoded on read into "p/ss", a different credential
#   "pa ss"    — `quote_plus` (migrations/env.py) encoded the space as `+`, which the
#                consuming `unquote` does not reverse, yielding "pa+ss"
#   "p/s?s#x"  — `/`, `?` and `#` terminate the netloc, truncating the credential
#   "p:ss"     — the `:` was read as the user/password separator
#   "100%"     — a trailing `%` is an invalid percent-escape for any unquoting reader
_HOSTILE_CREDENTIALS = ["p@ss", "p%2Fss", "pa ss", "p/s?s#x", "p:ss", "100%"]

_POSTGRES_ENV_KEYS = (
    "DATASPOKE_POSTGRES_HOST",
    "DATASPOKE_POSTGRES_PORT",
    "DATASPOKE_POSTGRES_USER",
    "DATASPOKE_POSTGRES_PASSWORD",
    "DATASPOKE_POSTGRES_DB",
)


def _connect_args(url: URL) -> dict[str, Any]:
    """The keyword arguments the asyncpg dialect would pass to the driver for *url*.

    This is the last observable point before the credential leaves DataSpoke, so it is
    where "verbatim" is provable. ``create_async_engine`` opens no socket — the pool is
    lazy — so this stays a unit test.
    """
    async_engine = create_async_engine(url)
    _, kwargs = async_engine.dialect.create_connect_args(url)
    return dict(kwargs)


def test_get_session_is_async_generator() -> None:
    assert stdlib_inspect.isasyncgenfunction(get_session)


# ── Credentials reach the driver verbatim ────────────────────────────────────


@pytest.mark.parametrize("password", _HOSTILE_CREDENTIALS)
def test_password_reaches_the_driver_verbatim(password: str) -> None:
    """asyncpg receives ``DATASPOKE_POSTGRES_PASSWORD`` exactly as the operator set it.

    Regression for issue #120: the DSN was interpolated as
    ``postgresql+asyncpg://{user}:{password}@{host}…``, so a password containing ``@``
    made SQLAlchemy read the credential as the text before it and the host as the text
    after it — the API then failed DNS resolution against a host that does not exist
    instead of reporting a bad credential — and a ``%`` was silently decoded into a
    different credential. The host assertion below is what pins that specific failure.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are
    carried as `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
    `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
    from this connection layer whatever characters they contain".
    """
    args = _connect_args(_build_url("db.example.com", "9999", "myuser", password, "mydb"))

    assert args["password"] == password
    assert args["user"] == "myuser"
    assert args["host"] == "db.example.com"
    assert args["port"] == 9999
    assert args["database"] == "mydb"


@pytest.mark.parametrize("user", _HOSTILE_CREDENTIALS)
def test_username_reaches_the_driver_verbatim(user: str) -> None:
    """The same guarantee holds for ``DATASPOKE_POSTGRES_USER``.

    The spec clause names both credentials; a username is the other half of the netloc
    and corrupts the DSN in exactly the same ways.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "`DATASPOKE_POSTGRES_USER`
    / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim from this connection layer
    whatever characters they contain".
    """
    args = _connect_args(_build_url("db.example.com", "9999", user, "secret", "mydb"))

    assert args["user"] == user
    assert args["password"] == "secret"
    assert args["host"] == "db.example.com"
    assert args["port"] == 9999
    assert args["database"] == "mydb"


def test_url_string_form_masks_the_password() -> None:
    """A distinctive password injected into the URL does not appear in its string form.

    The absence assertion is meaningful because the value is injected here: the URL is
    built with this exact secret, and ``url.password`` is asserted as the backstop that
    the credential is genuinely carried rather than dropped on the floor. ``str(url)``
    is what lands in a repr, a log line, or an engine traceback.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "the URL's string form
    masks the password rather than carrying it into a log line or traceback".
    """
    secret = "s3cr3t-never-log-this"  # noqa: S105 - test fixture value, not a credential
    url = _build_url("db.example.com", "5432", "myuser", secret, "mydb")

    assert url.password == secret, "backstop: the URL must actually carry the credential"
    assert secret not in str(url)
    assert secret not in repr(url)
    # The real DSN is still reachable for the caller that explicitly asks for it.
    assert secret in url.render_as_string(hide_password=False)


# ── Env vars land on the URL's fields ────────────────────────────────────────


def test_database_url_fields_come_from_the_postgres_env_vars() -> None:
    """``DATASPOKE_POSTGRES_*`` populate the URL's fields; unset vars fall back.

    Asserted component-wise rather than against a rendered DSN: the rendered form masks
    the password, and comparing against a literal string is exactly the DSN round trip
    the connection layer exists to avoid.

    The populated password carries an ``@`` and a ``/`` on purpose, so this test states
    the issue #120 invariant on the module-level ``DATABASE_URL`` — the surface the
    engine is actually built from — and not only on the helper behind it: with the
    pre-fix interpolated DSN the ``@`` moved the tail of the password into the host.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — credentials "carried as
    `sqlalchemy.URL` fields ... whatever characters they contain". That clause covers
    the *carriage* (the ``URL``-fields shape and the driver name) and names
    ``DATASPOKE_POSTGRES_USER`` / ``_PASSWORD``.

    NOT spec-derived: the ``_HOST`` / ``_PORT`` / ``_DB`` variable names and the
    cleared-env fallbacks (``localhost``, 5432, ``dataspoke``/``dataspoke``) appear in no
    spec document — they are impl-documented dev conveniences, pinned here because they
    are the values a developer gets with no environment at all and a silent change to
    them would point a laptop at a different database.
    """
    import src.shared.db.session as mod

    try:
        cleared = {k: v for k, v in os.environ.items() if k not in _POSTGRES_ENV_KEYS}
        with patch.dict("os.environ", cleared, clear=True):
            importlib.reload(mod)
            default_url = mod.DATABASE_URL
        assert default_url.drivername == "postgresql+asyncpg"
        assert default_url.username == "dataspoke"
        assert default_url.password == "dataspoke"
        assert default_url.host == "localhost"
        assert default_url.port == 5432
        assert default_url.database == "dataspoke"

        populated = {
            "DATASPOKE_POSTGRES_HOST": "db.example.com",
            "DATASPOKE_POSTGRES_PORT": "9999",
            "DATASPOKE_POSTGRES_USER": "myuser",
            "DATASPOKE_POSTGRES_PASSWORD": "p@ss/word",
            "DATASPOKE_POSTGRES_DB": "mydb",
        }
        with patch.dict("os.environ", populated, clear=False):
            importlib.reload(mod)
            env_url = mod.DATABASE_URL
        assert env_url.drivername == "postgresql+asyncpg"
        assert env_url.username == "myuser"
        assert env_url.password == "p@ss/word"
        assert env_url.host == "db.example.com"
        assert env_url.port == 9999
        assert env_url.database == "mydb"
    finally:
        # Restore the module bound to the ambient environment for every later test.
        importlib.reload(mod)
        assert mod.DATABASE_URL.drivername == "postgresql+asyncpg"


# ── Engine and session-factory configuration ─────────────────────────────────


def test_engine_pool_size() -> None:
    """Pool size must be 10 per spec/feature/BACKEND.md §Shared Services:
    'Pool size 10, max overflow 5'."""
    assert engine.pool.size() == 10


def test_engine_max_overflow() -> None:
    """Max overflow must be 5 per spec/feature/BACKEND.md §Shared Services:
    'Pool size 10, max overflow 5'."""
    assert engine.pool._max_overflow == 5


def test_engine_pool_pre_ping_enabled() -> None:
    """Pre-ping validates and transparently replaces a connection invalidated by a
    Postgres pod reschedule before checkout, so a stale socket never surfaces as a
    query error to callers. SQLAlchemy 2.0 records this on the pool as `_pre_ping`."""
    assert engine.pool._pre_ping is True


def test_engine_pool_recycle_set() -> None:
    """A positive recycle window (1800s) drops connections that have outlived a
    backend move so they are not reused indefinitely; SQLAlchemy's default is
    -1 (recycling disabled)."""
    assert engine.pool._recycle == 1800


def test_session_factory_expire_on_commit_false() -> None:
    assert SessionLocal.kw.get("expire_on_commit") is False


# ── independent_sessionmaker: which database an independent write lands on ───
#
# The two branches are asserted separately because they fail in opposite directions.
# Choosing the module-level factory when the caller's session carries an engine sends the
# write to a different address than every other statement of the same call; choosing a
# derived factory when the session carries no usable engine has no address to derive from
# at all. Callers are ``IngestionService._report_api_health`` and
# ``auth.api_tokens.lookup_and_validate``; both hold their end-to-end assertions in their
# own test modules, and this table pins the shared decision itself.
#
# Identity checks resolve ``SessionLocal`` through the live module rather than the name
# imported at the top of this file: the ``_build_url`` tests above ``importlib.reload`` the
# module, which rebinds ``SessionLocal`` to a new object. Comparing against the stale
# top-level name makes the fallback branch look like the derived one.


def _live_session_local():
    from src.shared.db import session as session_mod

    return session_mod.SessionLocal


def test_a_session_bound_to_an_engine_yields_a_factory_on_that_engine() -> None:
    """The derived factory addresses the caller's database, not the app-runtime one.

    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — such a write "opens
        a session from a factory built on the **bind of the injected session**, so it
        reaches the database the caller is actually using".
    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — the module-level
        factory is bound to values "which an in-process caller carrying a session on
        another engine does not have; the write would otherwise be aimed at a different
        address than every other statement in the same call, with no diagnostic
        distinguishing that from success".
    """
    from unittest.mock import MagicMock

    from src.shared.db.session import independent_sessionmaker

    callers_engine = create_async_engine("postgresql+asyncpg://u:p@callers-db:5432/d")
    try:
        factory = independent_sessionmaker(MagicMock(spec_set=["bind"], bind=callers_engine))

        assert factory is not _live_session_local(), (
            "a caller carrying its own engine must not be served the module-level "
            "factory, whose address it does not share. spec: spec/feature/BACKEND.md "
            "§Shared Services (PostgreSQL row)."
        )
        assert factory.kw.get("bind") is callers_engine, (
            f"the factory must be built on the bind of the injected session; it was built "
            f"on {factory.kw.get('bind')!r}. spec: spec/feature/BACKEND.md §Shared "
            "Services (PostgreSQL row)."
        )
    finally:
        # Never connected to — only the engine's identity is read.
        callers_engine.sync_engine.dispose()


def test_the_derived_factory_keeps_expire_on_commit_false() -> None:
    """The derived factory carries the same session semantics as the module-level one.

    Not cosmetic. The callers of this helper are the same functions that run on the
    module-level factory when the fallback branch is taken, and they are written against
    ``expire_on_commit=False``: ``src/backend/admin/peripheral_health.py`` re-selects the
    row it just upserted with ``populate_existing=True`` precisely because a non-expiring
    session would otherwise hand back the stale pre-upsert instance. A derived factory
    with different semantics makes that one function behave differently depending on which
    branch its caller happened to take — the branch being an accident of how the caller
    built its session, not a decision anyone made about identity-map behaviour.

    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — the independent
        write "opens a session from a factory built on the **bind of the injected
        session**". The bind is the only property the spec varies between the two
        branches; ``expire_on_commit=False`` is the module-level factory's contract,
        pinned above in ``test_session_factory_expire_on_commit_false``. That the derived
        factory inherits it is a reading of the spec's silence, not a quoted clause.
    """
    from unittest.mock import MagicMock

    from src.shared.db.session import independent_sessionmaker

    callers_engine = create_async_engine("postgresql+asyncpg://u:p@callers-db:5432/d")
    try:
        factory = independent_sessionmaker(MagicMock(spec_set=["bind"], bind=callers_engine))

        assert factory.kw.get("expire_on_commit") is False, (
            f"the derived factory must keep expire_on_commit=False, matching the "
            f"module-level factory; got {factory.kw.get('expire_on_commit')!r}. A "
            "committed instance would otherwise be expired and re-read on a connection "
            "the caller may no longer hold (src/backend/admin/peripheral_health.py "
            "re-selects the row it just upserted)."
        )
    finally:
        callers_engine.sync_engine.dispose()


def _session_without_a_bind_attribute() -> object:
    """The shape most unit-test callers inject: ``bind`` is not in ``dir(AsyncSession)``,
    so the attribute is absent rather than present-and-None."""
    from unittest.mock import MagicMock

    return MagicMock(spec_set=[])


def _session_bound_to_none() -> object:
    from unittest.mock import MagicMock

    return MagicMock(spec_set=["bind"], bind=None)


def _session_bound_to_a_sync_engine() -> object:
    """A *sync* ``Engine`` cannot build an async factory — an in-memory SQLite engine is
    a real ``Engine`` that is never connected to."""
    from unittest.mock import MagicMock

    from sqlalchemy import create_engine

    return MagicMock(spec_set=["bind"], bind=create_engine("sqlite://"))


def _session_bound_to_a_non_engine() -> object:
    from unittest.mock import MagicMock

    return MagicMock(spec_set=["bind"], bind=object())


class _SessionWhoseBindReadRaises:
    """A session whose ``bind`` read raises the injected exception every time.

    Reading ``bind`` is itself an operation, and on a real session it is a property that
    can fail (a detached or otherwise broken instance). This shape stands in for that.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def bind(self) -> object:
        raise self._exc


def _session_whose_bind_read_raises() -> object:
    return _SessionWhoseBindReadRaises(RuntimeError("session is detached"))


def _session_whose_bind_read_raises_attribute_error() -> object:
    """A ``bind`` property that raises ``AttributeError`` from inside its own body.

    Indistinguishable at the call site from an attribute that was never set, which the
    spec states outright — see the silence assertion in
    ``test_only_an_unreadable_bind_is_diagnosed`` below.
    """
    return _SessionWhoseBindReadRaises(AttributeError("bind delegates to a gone attribute"))


_NO_USABLE_BIND_SHAPES = [
    ("a session exposing no bind attribute at all", _session_without_a_bind_attribute),
    ("a session whose bind is None", _session_bound_to_none),
    ("a session bound to a sync Engine", _session_bound_to_a_sync_engine),
    ("a session bound to something that is not an engine", _session_bound_to_a_non_engine),
    ("a session whose bind read raises", _session_whose_bind_read_raises),
    (
        "a session whose bind property raises AttributeError",
        _session_whose_bind_read_raises_attribute_error,
    ),
]


@pytest.mark.parametrize(("label", "build_session"), _NO_USABLE_BIND_SHAPES)
def test_a_session_with_no_usable_bind_falls_back_to_the_module_level_factory(
    label: str, build_session
) -> None:
    """No usable bind leaves the module-level factory as the only available address.

    The raising shapes are in this table for the same reason as the rest: the helper is
    total, so a read that fails resolves to a factory rather than propagating. A helper
    that let the read escape would fail here as an error, not as a wrong factory.

    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — "A session with no
        usable bind falls back to the module-level factory, the only address available in
        that case, and the helper is total -- it never propagates."
    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — "A bind that is
        absent (the attribute is simply not set), `None`, or not an async engine falls
        back silently ... A bind whose read fails for any other reason is logged at
        WARNING with the exception".
    """
    from src.shared.db.session import independent_sessionmaker

    factory = independent_sessionmaker(build_session())

    assert factory is _live_session_local(), (
        f"{label}: with nothing to derive an engine from, the write must go through the "
        f"module-level factory — the only address available; got {factory!r}. "
        "spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row)."
    )


def test_only_an_unreadable_bind_is_diagnosed(caplog: pytest.LogCaptureFixture) -> None:
    """The fallback is silent for the shapes the caller can see, and logged for the one it cannot.

    Both halves are asserted in one body on purpose. The silent half is an absence
    assertion, and the unreadable-bind leg that runs first is its backstop: it proves a
    record from this module is emitted and captured under exactly this configuration, so
    the silence below cannot pass merely because logging was never wired up.

    The split is the whole point of the guarded read. Logging every fallback would fire a
    WARNING on the ``spec``'d-mock shape that most unit-test callers inject — noise on a
    condition nobody can act on — while logging none of them leaves the one shape the
    caller cannot see in what it injected undiagnosable: the write silently lands on the
    app-runtime database instead of the caller's.

    The ``AttributeError``-from-inside-a-property case is silent by the spec's own
    statement, not by preference: the unset attribute is recognised by the
    ``AttributeError`` its read raises, so the two are indistinguishable.

    spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — "A bind that is
        absent (the attribute is simply not set), `None`, or not an async engine falls
        back silently -- those are shapes the caller can see in what it injected. A bind
        whose read fails for any other reason is logged at WARNING with the exception,
        because that shape is invisible to the caller. The unset case is recognised by the
        `AttributeError` the read raises, so an `AttributeError` raised from *inside* a
        `bind` property is indistinguishable from an unset attribute and is silent too."
    """
    import logging

    from src.shared.db.session import independent_sessionmaker

    caplog.set_level(logging.DEBUG)
    unreadable = RuntimeError("session is detached")

    # ── The logged leg (also the backstop for the silent leg below) ──
    caplog.clear()
    factory = independent_sessionmaker(_SessionWhoseBindReadRaises(unreadable))

    assert factory is _live_session_local(), (
        f"an unreadable bind still falls back to the module-level factory; got {factory!r}. "
        "spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row)."
    )
    diagnosed = [r for r in caplog.records if r.name == "src.shared.db.session"]
    assert len(diagnosed) == 1, (
        f"a bind whose read fails for a reason other than an unset attribute must be "
        f"diagnosed exactly once — it is the shape the caller cannot see, and the write "
        f"otherwise lands on the app-runtime database with no trace; captured "
        f"{[(r.levelname, r.getMessage()) for r in diagnosed]!r}. "
        "spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row)."
    )
    assert diagnosed[0].levelname == "WARNING", (
        f"the unreadable-bind fallback is logged at WARNING; got {diagnosed[0].levelname}. "
        "spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row)."
    )
    assert diagnosed[0].exc_info is not None and diagnosed[0].exc_info[1] is unreadable, (
        f"the record must carry the exception that made the bind unreadable, or the "
        f"operator learns a fallback happened without learning why; got "
        f"{diagnosed[0].exc_info!r}. spec: spec/feature/BACKEND.md §Shared Services "
        "(PostgreSQL row) — 'logged at WARNING with the exception'."
    )

    # ── The silent legs ──
    for label, build_session in _NO_USABLE_BIND_SHAPES:
        if build_session is _session_whose_bind_read_raises:
            continue  # the logged leg, asserted above
        caplog.clear()
        factory = independent_sessionmaker(build_session())

        assert factory is _live_session_local(), (
            f"{label}: expected the module-level factory; got {factory!r}."
        )
        records = [r for r in caplog.records if r.name == "src.shared.db.session"]
        assert records == [], (
            f"{label}: this shape is visible to the caller in what it injected, so the "
            f"fallback is silent; it emitted "
            f"{[(r.levelname, r.getMessage()) for r in records]!r}. "
            "spec: spec/feature/BACKEND.md §Shared Services (PostgreSQL row) — such a bind "
            "'falls back silently'."
        )
