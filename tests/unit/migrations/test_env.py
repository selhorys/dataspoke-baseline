"""Tests for ``migrations/env.py``'s URL resolution — the third credential-carrying
connection layer, alongside ``src/shared/db/session.py`` and the rate limiter's Redis
storage URI.

Alembic's ``env.py`` is a script, not a library: importing it runs the migrations
(``if context.is_offline_mode(): … else: run_migrations_online()`` at the bottom) and
touches ``alembic.context``, which is a proxy that raises outside a migration run. The
driver below therefore executes the *shipped source* of ``_resolve_url`` alone, keeping
the module's imports and dropping every module-level statement. It is the real function
under test, not a copy: editing ``migrations/env.py`` changes what these tests run.

Same invariant as ``tests/unit/shared/db/test_session.py``, asserted at the same place —
the keyword arguments the asyncpg dialect would hand the driver — because "reach the
driver verbatim" is a statement about the driver's arguments.

spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried as
      `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
      `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
      from this connection layer whatever characters they contain".
"""

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.db.session import _build_url as _session_build_url

_ENV_PY = Path(__file__).resolve().parents[3] / "migrations" / "env.py"

# The same hostile set ``tests/unit/shared/db/test_session.py`` uses, for the same
# reasons. ``"pa ss"`` is the member that matters most here: this file is where
# ``quote_plus`` lived, and it encodes a space as ``+``, which the ``unquote`` on the
# reading side does not reverse — so ``pa ss`` silently became ``pa+ss`` (issue #120).
_HOSTILE_CREDENTIALS = ["p@ss", "p%2Fss", "pa ss", "p/s?s#x", "p:ss", "100%"]

_ENV_KEYS = (
    "DATASPOKE_ALEMBIC_URL",
    "DATASPOKE_POSTGRES_HOST",
    "DATASPOKE_POSTGRES_PORT",
    "DATASPOKE_POSTGRES_USER",
    "DATASPOKE_POSTGRES_PASSWORD",
    "DATASPOKE_POSTGRES_DB",
)


# ── Test drivers ──────────────────────────────────────────────────────────────


def _load_resolve_url() -> Callable[[], str | URL | None]:
    """Return ``_resolve_url`` as defined in the shipped ``migrations/env.py``.

    Keeps the module's import statements (``os``, ``sqlalchemy.URL``) and the target
    function; drops every other module-level statement, which is what would otherwise
    call ``_load_dotenv()``, read ``alembic.context``, and run the migrations.
    """
    tree = ast.parse(_ENV_PY.read_text(), filename=str(_ENV_PY))
    tree.body = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        or (isinstance(node, ast.FunctionDef) and node.name == "_resolve_url")
    ]
    namespace: dict[str, Any] = {"__name__": "migrations_env_under_test"}
    exec(compile(tree, str(_ENV_PY), "exec"), namespace)  # noqa: S102 - shipped source

    resolve = namespace.get("_resolve_url")
    assert callable(resolve), (
        f"driver bug: no `_resolve_url` function found in {_ENV_PY}. If it was renamed, "
        "this file's tests stop covering anything until the name here is updated."
    )
    return resolve  # type: ignore[no-any-return]


def _resolve_with_env(**env: str) -> str | URL | None:
    """Call the shipped ``_resolve_url`` with exactly *env* in the environment.

    ``clear=True``: the ambient shell (and ``helm-charts/.env.dev``, which the developer
    exports for the integration groups) carries real ``DATASPOKE_POSTGRES_*`` values that
    would otherwise leak into the fallback assertions.
    """
    with patch.dict("os.environ", env, clear=True):
        return _load_resolve_url()()


def _connect_args(url: URL) -> dict[str, Any]:
    """The keyword arguments the asyncpg dialect would pass to the driver for *url*.

    The last observable point before the credential leaves DataSpoke.
    ``create_async_engine`` opens no socket — the pool is lazy — so this stays a unit
    test.
    """
    return dict(create_async_engine(url).dialect.create_connect_args(url)[1])


# ── Credentials reach the driver verbatim ────────────────────────────────────


@pytest.mark.parametrize("password", _HOSTILE_CREDENTIALS)
def test_password_reaches_the_driver_verbatim(password: str) -> None:
    """asyncpg receives ``DATASPOKE_POSTGRES_PASSWORD`` exactly as the operator set it.

    Regression for issue #120 at its third site. ``env.py`` built a DSN string and
    escaped the credentials with ``quote_plus``, whose ``+`` encoding of a space is not
    what the consuming ``unquote`` reverses: ``pa ss`` reached the driver as ``pa+ss``
    and the migration failed authentication against a correctly-configured database.
    The other members pin the same asymmetry for the netloc-terminating characters.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "`DATASPOKE_POSTGRES_USER`
    / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim ... whatever characters
    they contain".
    """
    url = _resolve_with_env(
        DATASPOKE_POSTGRES_HOST="db.example.com",
        DATASPOKE_POSTGRES_PORT="9999",
        DATASPOKE_POSTGRES_USER="myuser",
        DATASPOKE_POSTGRES_PASSWORD=password,
        DATASPOKE_POSTGRES_DB="mydb",
    )

    assert isinstance(url, URL), (
        f"components from DATASPOKE_POSTGRES_* must resolve to a URL object, not a DSN "
        f"string; got {type(url).__name__}."
    )
    args = _connect_args(url)
    assert args["password"] == password
    assert args["user"] == "myuser"
    assert args["host"] == "db.example.com"
    assert args["port"] == 9999
    assert args["database"] == "mydb"


@pytest.mark.parametrize("user", _HOSTILE_CREDENTIALS)
def test_username_reaches_the_driver_verbatim(user: str) -> None:
    """The same guarantee holds for ``DATASPOKE_POSTGRES_USER``.

    A username is the other half of the netloc and corrupts a DSN in exactly the same
    ways; the spec clause names both credentials.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "`DATASPOKE_POSTGRES_USER`
    / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim ... whatever characters
    they contain".
    """
    url = _resolve_with_env(
        DATASPOKE_POSTGRES_HOST="db.example.com",
        DATASPOKE_POSTGRES_PORT="9999",
        DATASPOKE_POSTGRES_USER=user,
        DATASPOKE_POSTGRES_PASSWORD="secret",
        DATASPOKE_POSTGRES_DB="mydb",
    )

    assert isinstance(url, URL)
    args = _connect_args(url)
    assert args["user"] == user
    assert args["password"] == "secret"
    assert args["host"] == "db.example.com"
    assert args["port"] == 9999
    assert args["database"] == "mydb"


def test_alembic_resolves_the_same_url_the_application_session_does() -> None:
    """For one environment, ``env.py`` and ``session.py`` produce identical driver args.

    Migrations that authenticate differently from the API — or land in a neighbouring
    database — are the failure this coherence check exists to catch, and it is not
    implied by either layer's own test: both could be wrong in the same direction only
    by being wrong in the same way, which asserting them against each other rules out.

    NOT spec-derived: no spec clause states the two layers must agree. It is the stated
    rationale of ``migrations/env.py``'s own ``_resolve_url`` docstring ("alembic connects
    to exactly the database ``session.py`` does"), recorded here as a test rather than a
    comment.
    """
    env = {
        "DATASPOKE_POSTGRES_HOST": "db.example.com",
        "DATASPOKE_POSTGRES_PORT": "9999",
        "DATASPOKE_POSTGRES_USER": "my@user",
        "DATASPOKE_POSTGRES_PASSWORD": "p@ss/word",
        "DATASPOKE_POSTGRES_DB": "my db",
    }
    alembic_url = _resolve_with_env(**env)
    session_url = _session_build_url(
        env["DATASPOKE_POSTGRES_HOST"],
        env["DATASPOKE_POSTGRES_PORT"],
        env["DATASPOKE_POSTGRES_USER"],
        env["DATASPOKE_POSTGRES_PASSWORD"],
        env["DATASPOKE_POSTGRES_DB"],
    )

    assert isinstance(alembic_url, URL)
    assert _connect_args(alembic_url) == _connect_args(session_url), (
        "alembic and the application session must reach the same database with the same "
        "credentials; "
        f"alembic={_connect_args(alembic_url)} session={_connect_args(session_url)}."
    )


def test_host_and_database_are_not_reparsed_as_a_query_string() -> None:
    """A ``?`` in the database name stays part of the database name.

    ``render_as_string`` quotes only ``username`` and ``password``, so a DSN string built
    from this URL would let a ``?`` in ``DATASPOKE_POSTGRES_DB`` re-parse as a query
    string — and the asyncpg dialect forwards query parameters to the driver as connect
    kwargs, so the migration would silently connect somewhere else with settings nobody
    asked for.

    NOT spec-derived: the spec clause covers the credentials. This is the additional
    rationale ``migrations/env.py``'s ``_resolve_url`` docstring gives for keeping the
    ``URL`` object all the way down, pinned so a future refactor to a DSN string fails
    here rather than in production.
    """
    url = _resolve_with_env(
        DATASPOKE_POSTGRES_HOST="db.example.com",
        DATASPOKE_POSTGRES_PORT="5432",
        DATASPOKE_POSTGRES_USER="myuser",
        DATASPOKE_POSTGRES_PASSWORD="secret",
        DATASPOKE_POSTGRES_DB="mydb?sslmode=disable",
    )

    assert isinstance(url, URL)
    args = _connect_args(url)
    assert args["database"] == "mydb?sslmode=disable"
    assert args["host"] == "db.example.com"
    assert "sslmode" not in args, (
        f"a '?' in the database name leaked into the driver's connect kwargs: {args}."
    )


# ── Source selection ─────────────────────────────────────────────────────────


def test_an_operator_supplied_alembic_url_is_passed_through_unchanged() -> None:
    """``DATASPOKE_ALEMBIC_URL`` wins over the components and is returned verbatim.

    The escape (or non-escape) of an operator-written DSN is the operator's business:
    re-quoting it here would corrupt a URL that already carries percent-escapes, which is
    the same class of double-encoding bug as issue #120 with the arrow reversed. The
    ``DATASPOKE_POSTGRES_*`` values seeded alongside are the non-matching side — they
    must not appear in the result.

    NOT spec-derived: ``DATASPOKE_ALEMBIC_URL`` appears in no spec document; it is the
    operator override ``migrations/env.py`` documents. Pinned because the precedence
    silently deciding which database gets migrated is worth a test.
    """
    operator_url = "postgresql+asyncpg://ops%2Fuser:p%40ss@ops.example.com:6432/opsdb"

    resolved = _resolve_with_env(
        DATASPOKE_ALEMBIC_URL=operator_url,
        DATASPOKE_POSTGRES_HOST="ignored.example.com",
        DATASPOKE_POSTGRES_USER="ignored",
        DATASPOKE_POSTGRES_PASSWORD="ignored",
        DATASPOKE_POSTGRES_DB="ignored",
    )

    assert resolved == operator_url
    assert isinstance(resolved, str), (
        "an operator-supplied URL must be handed on as the string it was written as, "
        f"not re-parsed into a URL object; got {type(resolved).__name__}."
    )
    assert "ignored" not in resolved


def test_no_configuration_at_all_resolves_to_no_url() -> None:
    """With neither override nor host, resolution yields ``None``.

    ``env.py`` then leaves ``sqlalchemy.url`` as ``alembic.ini`` has it, so a developer
    running ``alembic`` with no environment gets alembic's own error rather than a
    connection attempt against a fabricated localhost default. The positive legs above
    prove this function does return a URL when the environment supplies one, so the
    ``None`` here is a real absence.
    """
    assert _resolve_with_env() is None


def test_a_host_alone_is_enough_to_resolve_a_url() -> None:
    """``DATASPOKE_POSTGRES_HOST`` alone resolves; the rest fall back.

    NOT spec-derived: the fallbacks (port 5432, user/password/database ``dataspoke``) and
    the choice of ``_HOST`` as the required element appear in no spec document. They are
    impl-documented dev conveniences, pinned because they decide where an
    otherwise-unconfigured ``alembic upgrade head`` lands, and because ``_HOST`` being the
    sentinel is what makes the ``None`` case above reachable.
    """
    url = _resolve_with_env(DATASPOKE_POSTGRES_HOST="db.example.com")

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "db.example.com"
    assert url.port == 5432
    assert url.username == "dataspoke"
    assert url.password == "dataspoke"
    assert url.database == "dataspoke"
