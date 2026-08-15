"""Tests for ``tests/integration/util/db_url.py::build_postgres_url`` — the URL builder
both integration-layer Postgres connections are constructed through: the
``integration_db_url`` session fixture in ``tests/integration/conftest.py`` (which the
pgvector spot fixtures consume) and the utility layer's ``dataspoke_db_url``. Exactly
two call sites.

**Deliberate duplication.** ``tests/unit/integration_util/test_dataspoke_db_url.py``
exercises this builder transitively — ``dataspoke_db_url`` delegates here — so most of what the
cases below prove, it proves too, and only *for as long as that delegation holds*. The
overlap is not total even today: a builder that baked in its own ``user`` default is
caught here and nowhere else, because the utility-layer caller already defaults that key and the
mutation is invisible through the delegation. This file is the pin regardless: it states
the builder's own contract with no caller in the picture. The cases are trimmed to that
contract; the callers' env-reading policies are pinned where they live —
``test_dataspoke_db_url.py`` for the utility layer's ``dataspoke_db_url``,
``tests/unit/integration_conftest/test_integration_db_url.py`` for the fixture.

Same invariant as ``tests/unit/shared/db/test_session.py``, asserted at the same place —
the keyword arguments the asyncpg dialect would hand the driver — because "reach the
driver verbatim" is a statement about the driver's arguments, not about the shape of the
object in between. Anything that reintroduces a DSN round trip fails these tests.

The builder takes parameters rather than reading the environment because its two call
sites read the *same* five ``DATASPOKE_DEV_POSTGRES_*`` keys under deliberately
different fallback policies: the fixture requires host/port/user/password and fails
loudly when `helm-charts/.env.dev` was not exported, while ``dataspoke_db_url`` falls back
to the forwarded-port dev defaults. Reading the environment therefore stays at the call
sites, and no environment patching appears here.

spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried as
      `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
      `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
      from this connection layer whatever characters they contain, and the URL's string
      form masks the password rather than carrying it into a log line or traceback".
spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
      credentials from the environment (the `DATASPOKE_DEV_*` block in
      `helm-charts/.env.dev`); no credential is hardcoded in a helper".
"""

from typing import Any

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.util.db_url import build_postgres_url

# The members of the hostile set used by ``tests/unit/shared/db/test_session.py`` and
# ``tests/unit/migrations/test_env.py`` that name the distinct corruption modes an
# interpolated DSN suffers. ``p@ss/word``: ``@`` splits the netloc, so the tail of the
# password becomes the host. ``100%``: the *encode* direction — a bare ``%`` that a DSN
# round trip must not mangle on the way out. ``p%2Fss``: the *decode* direction — an
# already-escaped sequence that an unquote on the way back in turns into a different
# password (``p/ss``). Both percent members are needed: ``unquote("100%") == "100%"``, so
# ``100%`` alone leaves the decode direction unpinned. The remaining members of the wider
# set live at the siblings; repeating all six here buys nothing this file does not
# already prove.
_HOSTILE_CREDENTIALS = ["p@ss/word", "100%", "p%2Fss"]


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
    """asyncpg receives the password exactly as the operator set it.

    The ``host`` and ``port`` assertions are what pin the specific corruption: an
    interpolated DSN carrying ``p@ss/word`` yields a host of ``ss/word@127.0.0.1``, and
    every integration run then fails DNS resolution rather than reporting a bad
    credential.

    The ``user`` assertion doubles as the username's half of the spec clause: the two
    credentials are passed to distinct ``URL.create`` keywords, and a swap between them
    shows up here and in ``test_arguments_land_on_the_urls_fields``, both of which use
    values that cannot be confused for one another.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried
    as `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
    `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
    from this connection layer whatever characters they contain".
    """
    url = build_postgres_url(
        host="127.0.0.1",
        port="9201",
        user="myuser",
        password=password,
        db="mydb",
    )
    args = _connect_args(url)

    assert args["password"] == password
    assert args["user"] == "myuser"
    assert args["host"] == "127.0.0.1"
    assert args["port"] == 9201
    assert args["database"] == "mydb"


def test_url_string_form_masks_the_password() -> None:
    """A distinctive password handed to the builder does not appear in the URL's string form.

    The absence assertion is meaningful because the value is injected here: the URL is
    built from this exact secret, and ``url.password`` is asserted as the backstop that
    the credential is genuinely carried rather than dropped on the floor. ``str(url)`` is
    what lands in a pytest traceback when an integration fixture cannot reach Postgres.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "the URL's string form
    masks the password rather than carrying it into a log line or traceback".
    """
    secret = "s3cr3t-never-log-this"  # noqa: S105 - test fixture value, not a credential
    url = build_postgres_url(
        host="127.0.0.1",
        port="9201",
        user="myuser",
        password=secret,
        db="mydb",
    )

    assert url.password == secret, "backstop: the URL must actually carry the credential"
    assert secret not in str(url)
    assert secret not in repr(url)
    # The real DSN stays reachable for the caller that explicitly asks for it.
    assert secret in url.render_as_string(hide_password=False)


# ── Arguments land on the URL's fields ───────────────────────────────────────


def test_arguments_land_on_the_urls_fields() -> None:
    """Each parameter populates its own ``URL`` field, with no DSN in between.

    Asserted component-wise rather than against a rendered DSN: the rendered form masks
    the password, and comparing against a literal string is exactly the DSN round trip
    this builder exists to avoid.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried
    as `sqlalchemy.URL` fields rather than interpolated into a DSN string".
    """
    url = build_postgres_url(
        host="db.example.com",
        port="9999",
        user="myuser",
        password="p@ss/word",  # noqa: S106 - test fixture value
        db="mydb",
    )

    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "myuser"
    assert url.password == "p@ss/word"
    # The ``@`` pin at the URL surface the integration engines are actually built from:
    # interpolated, the host would read ``ss/word@db.example.com``.
    assert url.host == "db.example.com"
    assert url.port == 9999
    assert url.database == "mydb"


def test_the_builder_bakes_in_no_credential_of_its_own() -> None:
    """Empty credentials stay empty — the builder supplies no default user or password.

    spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
    credentials from the environment (the `DATASPOKE_DEV_*` block in
    `helm-charts/.env.dev`); no credential is hardcoded in a helper". Reading the
    environment is the call site's job; this asserts the executable half of the clause
    that belongs to the builder — a helper that substituted a working credential for an
    empty one would let a run silently proceed against the wrong identity.
    """
    url = build_postgres_url(host="127.0.0.1", port="9201", user="", password="", db="")

    assert url.username == ""
    assert url.password == ""
    assert url.database == ""
