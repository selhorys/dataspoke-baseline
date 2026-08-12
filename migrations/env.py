"""Alembic environment configuration for async PostgreSQL migrations."""

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import URL, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine

from src.shared.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _load_dotenv() -> None:
    """Load helm-charts/.env.dev into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parent.parent
    env_path = start / "helm-charts" / ".env.dev"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_url() -> str | URL | None:
    """Build DB URL from DATASPOKE_ALEMBIC_URL or DATASPOKE_POSTGRES_* env vars.

    Returns a ``URL`` object, never a DSN string, when the components come from
    ``DATASPOKE_POSTGRES_*`` — the same shape ``src/shared/db/session.py`` uses,
    for the same reason (spec/feature/BACKEND.md §Shared Services, PostgreSQL
    row): credentials held as fields reach the driver verbatim, with no
    escape/unescape round trip that could rewrite them. It matters beyond the
    credentials here, because ``render_as_string`` quotes only ``username`` and
    ``password``: a DSN string built from it would let a ``?`` in
    ``DATASPOKE_POSTGRES_DB`` or ``_HOST`` re-parse as a query string, which the
    asyncpg dialect forwards as driver connect kwargs. Keeping the object means
    alembic connects to exactly the database ``session.py`` does.

    ``DATASPOKE_ALEMBIC_URL`` is passed through as the operator wrote it.
    """
    explicit = os.environ.get("DATASPOKE_ALEMBIC_URL")
    if explicit:
        return explicit
    host = os.environ.get("DATASPOKE_POSTGRES_HOST")
    if not host:
        return None
    port = os.environ.get("DATASPOKE_POSTGRES_PORT", "5432")
    user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
    password = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke")
    db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=db,
    )


_load_dotenv()
_url = _resolve_url()
# Two readers take the URL back out of the configparser: offline mode, which
# renders SQL instead of connecting, and `async_engine_from_config`, which the
# online path uses only for an operator-supplied `DATASPOKE_ALEMBIC_URL`. A
# `URL` built from `DATASPOKE_POSTGRES_*` is therefore rendered only when offline
# mode will read it, and the deployment path never materialises a cleartext DSN
# at all. `render_as_string` escapes username and password with the exact
# inverse of the unescaping `make_url` applies on read; '%' is then doubled for
# configparser interpolation.
if isinstance(_url, str):
    config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))
elif _url is not None and context.is_offline_mode():
    _rendered = _url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", _rendered.replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Serialize concurrent migration runs. Migrations run as an init container
        # on every API replica, so a fresh install and a scale-from-0 both start
        # several at once. The lock is taken here rather than inside a revision
        # because `run_migrations` creates and reads `alembic_version` before it
        # calls any revision's `upgrade()`: a lock taken there would be reached
        # only after both runs had already decided to migrate, and the loser would
        # then execute the full body against a populated schema and fail on the
        # first `CREATE`. Held for this transaction, which spans the version-table
        # creation, the head read, the migration body and the version write, so a
        # waiting run resumes after the winner commits, reads the head that is now
        # recorded, and has nothing to do.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('dataspoke_migrations'))")
        )
        context.run_migrations()


async def run_async_migrations() -> None:
    # Build from the `URL` object when we have one, so the components never
    # round-trip through a DSN string on the path the deployment actually runs.
    # That branch does not consult the `[alembic]` section, so a `sqlalchemy.*`
    # option added to `alembic.ini` reaches the `async_engine_from_config` branch
    # below and nothing else.
    if isinstance(_url, URL):
        connectable = create_async_engine(_url, poolclass=pool.NullPool)
    else:
        connectable = async_engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
