"""Async Redis client wrapper for DataSpoke caching and pub/sub."""

from collections.abc import AsyncIterator

import redis.asyncio as aioredis

# Cache key conventions
VALIDATION_CACHE_KEY = "validation:{dataset_urn}:result"
QUALITY_CACHE_KEY = "quality:{dataset_urn}:score"
SEARCH_CACHE_KEY = "search:{query_hash}"
RATE_LIMIT_KEY = "rate_limit:{user_id}"


class RedisClient:
    """Async Redis wrapper with connection pooling and pub/sub."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._redis = aioredis.Redis(
            host=host,
            port=port,
            password=password,
            decode_responses=True,
        )

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int = 300) -> None:
        await self._redis.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def publish(self, channel: str, message: str) -> None:
        await self._redis.publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw_message in pubsub.listen():
                if raw_message["type"] == "message":
                    yield raw_message["data"]
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()
