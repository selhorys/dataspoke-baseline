"""Credential-carrying PostgreSQL URL builder for the integration-test layer.

The integration layer connects to the dev cluster's DataSpoke Postgres from two
places — the ``integration_db_url`` session fixture (which the pgvector spot
fixtures consume) and the reset utility's ``_dataspoke_db_url``. Both read the
same ``DATASPOKE_TEST_POSTGRES_*`` env block under different fallback policies,
and both need the same connection invariant, so the URL construction lives here
once.

spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are
    carried as `sqlalchemy.URL` fields rather than interpolated into a DSN string,
    so `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver
    verbatim from this connection layer whatever characters they contain, and the
    URL's string form masks the password rather than carrying it into a log line
    or traceback." Same invariant, one layer over.
spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
    credentials from the environment (the `DATASPOKE_TEST_*` block in
    `helm-charts/.env.dev`); no credential is hardcoded in a helper." Reading the
    environment stays at the call sites, which is why this helper takes
    parameters: both read the same five keys, but the fixture's required-env
    fail-fast and the reset utility's fallback defaults are deliberately
    different policies.
"""

from sqlalchemy import URL


def build_postgres_url(host: str, port: str | int, user: str, password: str, db: str) -> URL:
    """Carry the credentials as ``URL`` fields instead of interpolating a DSN string.

    Mirrors ``src/shared/db/session.py::_build_url``. The credentials reach the
    driver verbatim whatever characters they contain. A DSN string would have to
    be escaped on write and unescaped on read by two different code paths, and any
    asymmetry between them silently rewrites the credential — an ``@`` in the
    password turns the tail of it into the host, and a ``%`` decodes into a
    different password entirely. Held as fields, there is no round-trip: the
    dialect hands ``password`` to asyncpg as-is.

    ``str()``/``repr()`` of the result mask the password as ``***``, so the URL
    cannot carry a live credential into a log line, a pytest traceback, or the
    reset utility's terminal output. Use
    ``url.render_as_string(hide_password=False)`` where the real DSN is needed.

    *port* accepts the ``str`` an environment variable yields as well as an
    ``int``: both call sites read the port out of the environment as text.

    Covered by ``tests/unit/integration_util/test_db_url.py``.
    """
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        # Explicitness, not a load-bearing conversion: ``URL.create`` already
        # coerces the port through ``_assert_port``, so a ``str`` would arrive at
        # the driver as an ``int`` regardless. Kept so the value handed to an
        # ``int``-annotated parameter is an ``int``, and as a forward guard if
        # that coercion ever goes away. No test pins it — nothing observable
        # changes without it.
        port=int(port),
        database=db,
    )
