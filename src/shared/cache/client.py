"""Async Redis client wrapper for DataSpoke caching and pub/sub."""

from collections.abc import AsyncIterator

import redis.asyncio as aioredis

# Cache key conventions — see spec/feature/BACKEND.md §Cache Key Conventions.
VALIDATION_CACHE_KEY = "validation:{dataset_urn}:result"
QUALITY_CACHE_KEY = "quality:{dataset_urn}:score"
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

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 300) -> bool:
        """Set key only if it does not exist. Returns True if set, False if already exists."""
        result = await self._redis.set(key, value, ex=ttl_seconds, nx=True)
        return result is not None

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def delete_if_value(self, key: str, expected: str) -> bool:
        """Delete *key* only if its current value equals *expected* (CAS).

        Implements the canonical Lua compare-and-swap to prevent a worker whose
        TTL expired from deleting a lock token acquired by a later worker.

        Returns True if the key was deleted, False if the value did not match
        (or the key was already absent).
        """
        _LUA_CAS = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) "
            "else return 0 end"
        )
        result = await self._redis.eval(_LUA_CAS, 1, key, expected)
        return bool(result)

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
