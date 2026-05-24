"""Alembic environment configuration for async PostgreSQL migrations."""

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.shared.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _load_dotenv() -> None:
    """Load helm-charts/.env into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parent.parent
    env_path = start / "helm-charts" / ".env"
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


def _resolve_url() -> str | None:
    """Build DB URL from DATASPOKE_ALEMBIC_URL or DATASPOKE_POSTGRES_* env vars."""
    explicit = os.environ.get("DATASPOKE_ALEMBIC_URL")
    if explicit:
        return explicit
    host = os.environ.get("DATASPOKE_POSTGRES_HOST")
    if not host:
        return None
    port = os.environ.get("DATASPOKE_POSTGRES_PORT", "5432")
    user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
    password = quote_plus(os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke"))
    db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


_load_dotenv()
_url = _resolve_url()
if _url:
    # Escape '%' for configparser interpolation (% → %%)
    config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))


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
        context.run_migrations()


async def run_async_migrations() -> None:
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
