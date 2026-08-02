"""SQLAlchemy 2.0 async session factory for DataSpoke PostgreSQL."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_host = os.environ.get("DATASPOKE_POSTGRES_HOST", "localhost")
_port = os.environ.get("DATASPOKE_POSTGRES_PORT", "5432")
_user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
_password = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke")
_db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")


def _build_url(host: str, port: str, user: str, password: str, db: str) -> URL:
    """Carry the credentials as ``URL`` fields instead of interpolating a DSN string.

    spec/feature/BACKEND.md §Shared Services (PostgreSQL row): the credentials
    reach the driver verbatim whatever characters they contain. A DSN string
    would have to be escaped on write and unescaped on read by two different
    code paths, and any asymmetry between them silently rewrites the
    credential — an ``@`` in the password turns the tail of it into the host,
    and a ``%`` decodes into a different password entirely. Held as fields,
    there is no round-trip: the dialect hands ``password`` to asyncpg as-is.

    ``str()``/``repr()`` of the result mask the password as ``***``, so the URL
    cannot carry a live credential into a log line or traceback. Use
    ``url.render_as_string(hide_password=False)`` where the real DSN is needed.
    """
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=db,
    )


DATABASE_URL = _build_url(_host, _port, _user, _password, _db)

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    # Survive a Postgres pod reschedule: pre_ping validates (and transparently
    # replaces) a connection invalidated by the move before checkout, and
    # recycle drops connections older than 30 minutes.
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def independent_sessionmaker(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Return a session factory over the engine *db* is bound to.

    For a write that has to commit on its own terms while the caller holds a
    session of its own — a health row written as an exception unwinds
    (spec/feature/BACKEND.md §Health reporting), a throttled bookkeeping stamp on
    a request that may never commit. Two properties, and they are separate:

    *Distinct session* — the write is independent of the caller's transaction,
    so it survives a rollback the caller is about to take and lands even if the
    caller never commits. Opening a session of its own makes that independence
    structural rather than contingent on what the caller happens to do next.

    *Same engine* — the factory is built on the bind of the injected session, so
    the write reaches the database the caller is actually using. The module-level
    ``SessionLocal`` is bound at import time to the app-runtime
    ``DATASPOKE_POSTGRES_*`` values, which an in-process caller carrying a session
    on some other engine — a test utility, a reset helper, an Airflow task, a
    sweep driven from a developer machine through a forwarded port — does not
    have; the write would then be aimed at a different address than every other
    statement in the same call, and land somewhere nobody is reading.

    A session with no usable bind falls back to ``SessionLocal``, which is the
    only address available in that case.
    """
    bind = getattr(db, "bind", None)
    if isinstance(bind, AsyncEngine):
        return async_sessionmaker(bind, class_=AsyncSession, expire_on_commit=False)
    return SessionLocal
