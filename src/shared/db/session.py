"""SQLAlchemy 2.0 async session factory for DataSpoke PostgreSQL."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_host = os.environ.get("DATASPOKE_POSTGRES_HOST", "localhost")
_port = os.environ.get("DATASPOKE_POSTGRES_PORT", "5432")
_user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
_password = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke")
_db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")

DATABASE_URL = f"postgresql+asyncpg://{_user}:{_password}@{_host}:{_port}/{_db}"

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
