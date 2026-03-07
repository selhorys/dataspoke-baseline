"""FastAPI dependency injection providers for infrastructure clients."""

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.config import settings
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import get_session
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager


@lru_cache(maxsize=1)
def get_datahub() -> DataHubClient:
    """Cached DataHub client (connection reuse across requests)."""
    return DataHubClient(settings.datahub_gms_url, settings.datahub_token)


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Yield a database session per request."""
    async for session in get_session():
        yield session


@lru_cache(maxsize=1)
def get_redis() -> RedisClient:
    """Cached Redis client."""
    return RedisClient(settings.redis_host, settings.redis_port, settings.redis_password)


@lru_cache(maxsize=1)
def get_qdrant() -> QdrantManager:
    """Cached Qdrant client."""
    return QdrantManager(
        host=settings.qdrant_host,
        port=settings.qdrant_http_port,
        grpc_port=settings.qdrant_grpc_port,
        api_key=settings.qdrant_api_key,
    )


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    """Cached LLM client."""
    return LLMClient(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )
