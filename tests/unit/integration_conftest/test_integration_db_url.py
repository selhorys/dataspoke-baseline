"""Tests for ``tests/integration/conftest.py::integration_db_url`` — the session fixture
every DB-touching integration test connects through (``async_engine``,
``schema_bootstrap``, and the pgvector spot fixtures all build their engine from it).

Why a unit test for a fixture. This is the one remaining integration-layer site that can
regress on its own: the two spot ``_dsn()`` helpers were deleted outright, and the reset
utility's ``_dataspoke_db_url`` is pinned by ``tests/unit/integration_util/
test_main_db_url.py``. Nothing else observes this fixture's return value — reverting it
to an interpolated ``f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"``
leaves the whole unit suite green, and the ``-> URL`` annotation is not a backstop
either: per ``spec/TESTING.md`` §Unit Testing, "`tests/` is ruff-gated but not
type-checked, and mypy stays `src/`-only", so a fixture that silently returned a ``str``
again would pass every static gate. These tests are that gate.

The fixture is reached by executing ``conftest.py`` out of band and unwrapping the
fixture marker. Nothing in that module opens a connection at import time — it only reads
env — and the import runs inside ``patch.dict(..., clear=True)`` so the module's
``_load_dotenv()`` side effect (it copies `helm-charts/.env.dev` into ``os.environ``)
cannot leak into the rest of the unit run, and the import does not require that file to
exist. No cluster and no exported environment are needed.

spec: TESTING.md §Unit Testing → Scope — "Unit tests verify business logic in isolation.
      They **must never** require a running dev environment."
spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried as
      `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
      `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
      from this connection layer whatever characters they contain, and the URL's string
      form masks the password rather than carrying it into a log line or traceback".
spec: TESTING.md §Integration Lifecycle & Isolation — "Reset helpers ... read all
      credentials from the environment (the `DATASPOKE_DEV_*` block in
      `helm-charts/.env.dev`); no credential is hardcoded in a helper".
"""

import importlib.util
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from sqlalchemy import URL

_CONFTEST_PATH = Path(__file__).resolve().parents[2] / "integration" / "conftest.py"

# The module-level env reads ``tests/integration/conftest.py`` performs at import time
# (``os.environ[...]``, lines 94-102). Supplied so the import succeeds on a machine with
# no `helm-charts/.env.dev` and no exported environment; none of these keys is read by
# the fixture under test.
_IMPORT_ENV = {
    "DATASPOKE_DEV_DATAHUB_GMS_URL": "http://datahub-gms.import-only.invalid",
    "DATASPOKE_DEV_REDIS_HOST": "redis.import-only.invalid",
    "DATASPOKE_DEV_REDIS_PORT": "9202",
    "DATASPOKE_DEV_DUMMY_DATA_KAFKA_BROKERS": "example-kafka.import-only.invalid:9104",
    "DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS": "datahub-kafka.import-only.invalid:9005",
    # Inherited so the interpreter keeps working (subprocess lookups, cert paths) under
    # ``clear=True``; not read by the module under test.
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
}


def _load_integration_conftest() -> ModuleType:
    """Execute ``tests/integration/conftest.py`` as a standalone module.

    Loaded under a distinct module name so this never collides with the copy pytest
    itself imports when the integration groups run.
    """
    spec = importlib.util.spec_from_file_location(
        "_integration_conftest_under_test", _CONFTEST_PATH
    )
    assert spec is not None and spec.loader is not None, f"cannot load {_CONFTEST_PATH}"
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, _IMPORT_ENV, clear=True):
        spec.loader.exec_module(module)
    return module


_CONFTEST = _load_integration_conftest()

# ``pytest.fixture`` wraps the function in a ``FixtureFunctionDefinition`` that keeps the
# undecorated function on ``__wrapped__``. Asserted rather than assumed: if a pytest
# upgrade drops the attribute these tests must fail loudly at collection, not silently
# stop exercising the fixture.
assert hasattr(_CONFTEST.integration_db_url, "__wrapped__"), (
    "integration_db_url is no longer unwrappable — re-point these tests at the "
    "fixture's underlying function before trusting this file again."
)
_integration_db_url: Callable[[], URL] = _CONFTEST.integration_db_url.__wrapped__


def _url_with_env(**env: str) -> URL:
    """Call the shipped fixture body with exactly *env* in the environment.

    ``clear=True``: a developer runs the integration groups with `helm-charts/.env.dev`
    exported, so the ambient shell carries real ``DATASPOKE_DEV_POSTGRES_*`` values that
    would otherwise leak into the assertions below — including the cleared-env one.
    """
    with patch.dict(os.environ, env, clear=True):
        return _integration_db_url()


def test_the_env_block_populates_the_urls_fields() -> None:
    """Each ``DATASPOKE_DEV_POSTGRES_*`` value lands on its own ``URL`` field.

    The ``isinstance`` assertion is the direct pin: the fixture's consumers pass its
    result straight to ``create_async_engine``, which accepts a DSN string just as
    happily, so an interpolated string regresses silently everywhere except here. The
    ``host`` assertion pins the consequence — with ``p@ss`` interpolated into a DSN the
    netloc splits and the host reads ``ss@db.example.com``, so every integration run
    fails DNS resolution instead of reporting a bad credential.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — "Credentials are carried
    as `sqlalchemy.URL` fields rather than interpolated into a DSN string, so
    `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_PASSWORD` reach the driver verbatim
    from this connection layer whatever characters they contain".
    """
    url = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="db.example.com",
        DATASPOKE_DEV_POSTGRES_PORT="9999",
        DATASPOKE_DEV_POSTGRES_USER="myuser",
        DATASPOKE_DEV_POSTGRES_PASSWORD="p@ss",  # noqa: S106 - test fixture value
        DATASPOKE_DEV_POSTGRES_DB="mydb",
    )

    assert isinstance(url, URL), (
        "the fixture must carry credentials as sqlalchemy.URL fields, not as an "
        f"interpolated DSN string; got {type(url).__name__}"
    )
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "myuser"
    assert url.password == "p@ss"
    assert url.host == "db.example.com"
    assert url.port == 9999
    assert url.database == "mydb"


def test_the_fixture_reads_the_test_env_block_not_the_app_runtime_one() -> None:
    """The fixture reads ``DATASPOKE_DEV_POSTGRES_*``, never the app-runtime block.

    A fixture that fell back to ``DATASPOKE_POSTGRES_*`` would silently target whatever
    the app runtime is configured for — in-cluster coordinates unreachable from a
    developer machine. Both blocks are populated here with *different* values, so this
    discriminates; it is the same shape as ``tests/unit/integration_util/
    test_main_db_url.py::test_every_env_key_the_helper_reads_is_the_test_block``, applied
    to the other call site.

    spec: TESTING.md §Running — "Export `helm-charts/.env.dev` into the shell before
    invoking pytest — `conftest.py` and `util/*.py` consume the `DATASPOKE_DEV_*` block
    it contains". §Integration Lifecycle & Isolation carries the same rule but scopes it
    to reset helpers; this fixture is neither, so §Running is the anchor that governs it.
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
        f"the fixture must read the DATASPOKE_DEV_* block, not the app-runtime "
        f"DATASPOKE_POSTGRES_* one; got {url.render_as_string(hide_password=False)!r}."
    )


@pytest.mark.parametrize(
    "missing",
    [
        "DATASPOKE_DEV_POSTGRES_HOST",
        "DATASPOKE_DEV_POSTGRES_PORT",
        "DATASPOKE_DEV_POSTGRES_USER",
        "DATASPOKE_DEV_POSTGRES_PASSWORD",
    ],
)
def test_each_required_key_is_individually_required(missing: str) -> None:
    """Dropping any one of the four required keys raises ``KeyError`` naming *that* key.

    This is the whole claim the fixture's docstring makes — "raising ``KeyError`` here
    names the cause instead of letting every DB-touching test fail against a fallback
    host". A fallback host would turn "you forgot to export `helm-charts/.env.dev`" into
    a connection error at each of the dozens of tests that depend on ``async_engine``.

    Parametrized one key at a time rather than clearing the environment wholesale: with
    everything unset the first lookup short-circuits, so a single cleared-env case proves
    only that *some* key is required. A fallback added to ``DATASPOKE_DEV_POSTGRES_PASSWORD``
    alone — the most consequential one, since it would let a run proceed against an empty
    credential instead of failing — would survive that weaker test. Each case here omits
    exactly one key from an otherwise-complete block, so each key is pinned on its own.

    NOT spec-derived: no spec document states this fixture's failure mode. Pinned because
    the fail-fast policy is the fixture's stated contract and the only thing that
    distinguishes it from the reset utility's defaulted sibling.
    """
    env = {
        "DATASPOKE_DEV_POSTGRES_HOST": "db.example.com",
        "DATASPOKE_DEV_POSTGRES_PORT": "9999",
        "DATASPOKE_DEV_POSTGRES_USER": "myuser",
        "DATASPOKE_DEV_POSTGRES_PASSWORD": "secret",
        "DATASPOKE_DEV_POSTGRES_DB": "mydb",
    }
    del env[missing]

    with pytest.raises(KeyError) as excinfo, patch.dict(os.environ, env, clear=True):
        _integration_db_url()

    assert missing in str(excinfo.value), (
        f"the failure must name the one missing key {missing!r}; got {excinfo.value!r}"
    )


def test_only_the_database_name_is_defaulted() -> None:
    """``DATASPOKE_DEV_POSTGRES_DB`` is the single key with a fallback.

    Its absence is not a signal that the env file went unexported — the cluster's
    database name is fixed — so it defaults while the four coordinates that vary per
    developer stay required. The spot pgvector fixtures that used to read this key
    themselves (one of them requiring it) now inherit this policy.

    NOT spec-derived: the default value ``dataspoke`` appears in no spec document; it is
    the DataSpoke database name the install scripts auto-populate into
    `helm-charts/.env.dev` (``DATASPOKE_DEV_POSTGRES_DB``, from the app ConfigMap).
    Pinned because a silent change would point every integration engine at a different
    database.
    """
    url = _url_with_env(
        DATASPOKE_DEV_POSTGRES_HOST="db.example.com",
        DATASPOKE_DEV_POSTGRES_PORT="9999",
        DATASPOKE_DEV_POSTGRES_USER="myuser",
        DATASPOKE_DEV_POSTGRES_PASSWORD="mypass",  # noqa: S106 - test fixture value
    )

    assert url.database == "dataspoke"
    # Backstop: the default is reached because the key was absent, not because the
    # fixture ignores it.
    assert (
        _url_with_env(
            DATASPOKE_DEV_POSTGRES_HOST="db.example.com",
            DATASPOKE_DEV_POSTGRES_PORT="9999",
            DATASPOKE_DEV_POSTGRES_USER="myuser",
            DATASPOKE_DEV_POSTGRES_PASSWORD="mypass",  # noqa: S106 - test fixture value
            DATASPOKE_DEV_POSTGRES_DB="explicitdb",
        ).database
        == "explicitdb"
    )
