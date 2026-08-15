"""Tests for ``tests/integration/util/db_url.py::dataspoke_db_url`` — the integration
utility layer's connection layer for the DataSpoke operational DB (the reset CLI and
the registry reconcile build their engines from it), and the fourth credential-carrying
URL builder alongside ``src/shared/db/session.py``, ``migrations/env.py``, and the rate
limiter's Redis storage URI.

Same invariant as ``tests/unit/shared/db/test_session.py``, asserted at the same place —
the keyword arguments the asyncpg dialect would hand the driver — because "reach the
driver verbatim" is a statement about the driver's arguments, not about the shape of the
object in between. Anything that reintroduces a DSN round trip fails these tests.

The helper differs from its three siblings in only one respect: it reads the
``DATASPOKE_DEV_*`` block (the forwarded-port coordinates in ``helm-charts/.env.dev``)
rather than the app-runtime ``DATASPOKE_POSTGRES_*`` block, because it runs on a
developer machine outside the cluster.

spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried as
      `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
      `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
      from this connection layer whatever characters they contain, and the URL's string
      form masks the password rather than carrying it into a log line or traceback".
spec: TESTING.md §Running — "`conftest.py` and `util/*.py` consume the `DATASPOKE_DEV_*`
      block it contains". The on-point anchor: the helper under test sits in `util/*.py`
      and serves a provisioning reconcile as well as the reset CLI.
spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
      credentials from the environment (the `DATASPOKE_DEV_*` block in
      `helm-charts/.env.dev`); no credential is hardcoded in a helper". Covers the
      reset-CLI caller specifically.
"""

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.util.db_url import dataspoke_db_url

# The same hostile set ``tests/unit/shared/db/test_session.py`` and
# ``tests/unit/migrations/test_env.py`` use, for the same reasons. ``"p@ss"`` is the
# member that matters most here: with the interpolated DSN this helper used to build,
# the ``@`` split the netloc and the tail of the password became the host — so every caller
# (the reset CLI and the provisioning registry reconcile alike) connected to a host that
# does not exist instead of reporting a bad credential.
_HOSTILE_CREDENTIALS = ["p@ss", "p%2Fss", "pa ss", "p/s?s#x", "p:ss", "100%"]


def _url_with_env(**env: str) -> URL:
    """Call the shipped helper with exactly *env* in the environment.

    ``clear=True``: a developer runs the integration groups with
    ``helm-charts/.env.dev`` exported, so the ambient shell carries real
    ``DATASPOKE_DEV_POSTGRES_*`` values that would otherwise leak into the assertions
    below — including the fallback ones.
    """
    with patch.dict("os.environ", env, clear=True):
        return dataspoke_db_url()


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
    """asyncpg receives ``DATASPOKE_DEV_POSTGRES_PASSWORD`` exactly as the operator set it.

    The ``host`` assertion is the one that pins the specific corruption: an interpolated
    DSN carrying ``p@ss`` yields a host of ``ss@127.0.0.1``, and every run built on this
    helper then fails DNS resolution rather than reporting a bad credential.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried
    as `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
    `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
    from this connection layer whatever characters they contain".
    """
    url = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="127.0.0.1",
        DATASPOKE_DEV_POSTGRES_PORT="9201",
        DATASPOKE_DEV_POSTGRES_USER="myuser",
        DATASPOKE_DEV_POSTGRES_PASSWORD=password,
        DATASPOKE_DEV_POSTGRES_DB="mydb",
    )
    args = _connect_args(url)

    assert args["password"] == password
    assert args["user"] == "myuser"
    assert args["host"] == "127.0.0.1"
    assert args["port"] == 9201
    assert args["database"] == "mydb"


@pytest.mark.parametrize("user", _HOSTILE_CREDENTIALS)
def test_username_reaches_the_driver_verbatim(user: str) -> None:
    """The same guarantee holds for ``DATASPOKE_DEV_POSTGRES_USER``.

    The spec clause names both credentials; a username is the other half of the netloc
    and corrupts the DSN in exactly the same ways.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "`DATASPOKE_POSTGRES_USER`
    / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim from this connection layer
    whatever characters they contain".
    """
    url = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="127.0.0.1",
        DATASPOKE_DEV_POSTGRES_PORT="9201",
        DATASPOKE_DEV_POSTGRES_USER=user,
        DATASPOKE_DEV_POSTGRES_PASSWORD="secret",  # noqa: S106 - test fixture value
        DATASPOKE_DEV_POSTGRES_DB="mydb",
    )
    args = _connect_args(url)

    assert args["user"] == user
    assert args["password"] == "secret"
    assert args["host"] == "127.0.0.1"
    assert args["port"] == 9201
    assert args["database"] == "mydb"


def test_url_string_form_masks_the_password() -> None:
    """A distinctive password injected into the URL does not appear in its string form.

    The absence assertion is meaningful because the value is injected here: the URL is
    built from this exact secret, and ``url.password`` is asserted as the backstop that
    the credential is genuinely carried rather than dropped on the floor. ``str(url)`` is
    what lands in a repr or an engine traceback — and this helper's callers print reset
    progress to a developer's terminal.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "the URL's string form
    masks the password rather than carrying it into a log line or traceback".
    """
    secret = "s3cr3t-never-log-this"  # noqa: S105 - test fixture value, not a credential
    url = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="127.0.0.1",
        DATASPOKE_DEV_POSTGRES_PORT="9201",
        DATASPOKE_DEV_POSTGRES_USER="myuser",
        DATASPOKE_DEV_POSTGRES_PASSWORD=secret,
        DATASPOKE_DEV_POSTGRES_DB="mydb",
    )

    assert url.password == secret, "backstop: the URL must actually carry the credential"
    assert secret not in str(url)
    assert secret not in repr(url)
    # The real DSN stays reachable for the caller that explicitly asks for it.
    assert secret in url.render_as_string(hide_password=False)


# ── Env vars land on the URL's fields ────────────────────────────────────────


def test_url_fields_come_from_the_dev_postgres_env_block() -> None:
    """``DATASPOKE_DEV_POSTGRES_*`` populate the URL's fields; unset vars fall back.

    Asserted component-wise rather than against a rendered DSN: the rendered form masks
    the password, and comparing against a literal string is exactly the DSN round trip
    this connection layer exists to avoid.

    spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
    credentials from the environment (the `DATASPOKE_DEV_*` block in
    `helm-charts/.env.dev`); no credential is hardcoded in a helper". The assertion that
    a cleared environment yields an *empty* password rather than a working one is the
    executable form of "no credential is hardcoded".

    NOT spec-derived: the individual variable names and the remaining cleared-env
    fallbacks (``dataspoke``, ``localhost``, 9201) appear in no spec document — they are
    the forwarded-port dev conveniences `helm-charts/.env.dev` supplies, pinned here
    because they are what a developer gets with no environment at all and a silent change
    would point a reset run at a different database.
    """
    cleared = _url_with_env()

    assert cleared.drivername == "postgresql+asyncpg"
    assert cleared.username == "dataspoke"
    assert cleared.password == "", "no credential may be baked into the helper"
    assert cleared.host == "localhost"
    assert cleared.port == 9201
    assert cleared.database == "dataspoke"

    populated = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="db.example.com",
        DATASPOKE_DEV_POSTGRES_PORT="9999",
        DATASPOKE_DEV_POSTGRES_USER="myuser",
        DATASPOKE_DEV_POSTGRES_PASSWORD="p@ss/word",  # noqa: S106 - test fixture value
        DATASPOKE_DEV_POSTGRES_DB="mydb",
    )

    assert populated.drivername == "postgresql+asyncpg"
    assert populated.username == "myuser"
    assert populated.password == "p@ss/word"
    # The ``@`` pin at the URL surface the utility-layer engines are actually built from:
    # with the pre-fix interpolated DSN the host read ``ss/word@db.example.com``.
    assert populated.host == "db.example.com"
    assert populated.port == 9999
    assert populated.database == "mydb"


def test_every_env_key_the_helper_reads_is_the_dev_block() -> None:
    """The helper reads only ``DATASPOKE_DEV_POSTGRES_*``, never the app-runtime block.

    A utility-layer helper that fell back to ``DATASPOKE_POSTGRES_*`` would silently target
    whatever the app runtime is configured for — in-cluster coordinates unreachable from
    a developer machine — which is the failure mode issue #118 traced in the reporter.
    Both blocks are populated here with *different* values, so this discriminates.

    spec: TESTING.md §Running — "`conftest.py` and `util/*.py` consume the
    `DATASPOKE_DEV_*` block it contains".
    spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
    credentials from the environment (the `DATASPOKE_DEV_*` block in
    `helm-charts/.env.dev`)".
    """
    url = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="forwarded.example.com",
        DATASPOKE_DEV_POSTGRES_PORT="9201",
        DATASPOKE_DEV_POSTGRES_USER="testuser",
        DATASPOKE_DEV_POSTGRES_PASSWORD="testpass",  # noqa: S106 - test fixture value
        DATASPOKE_DEV_POSTGRES_DB="testdb",
        DATASPOKE_POSTGRES_HOST="in-cluster.example.com",
        DATASPOKE_POSTGRES_PORT="5432",
        DATASPOKE_POSTGRES_USER="runtimeuser",
        DATASPOKE_POSTGRES_PASSWORD="runtimepass",
        DATASPOKE_POSTGRES_DB="runtimedb",
    )

    assert (url.host, url.port, url.username, url.password, url.database) == (
        "forwarded.example.com",
        9201,
        "testuser",
        "testpass",
        "testdb",
    ), (
        f"dataspoke_db_url must read the DATASPOKE_DEV_* block, not the app-runtime "
        f"DATASPOKE_POSTGRES_* one; got {url.render_as_string(hide_password=False)!r}."
    )
