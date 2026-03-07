"""Async Redis wrapper for caching, rate limiting, and pub/sub."""

from collections.abc import AsyncIterator

import redis.asyncio as aioredis

from src.shared.exceptions import StorageUnavailableError


class RedisClient:
    """Async Redis wrapper."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._redis = aioredis.Redis(
            host=host,
            port=port,
            password=password or None,
            decode_responses=True,
        )

    async def get(self, key: str) -> str | None:
        try:
            return await self._redis.get(key)
        except aioredis.RedisError as exc:
            raise StorageUnavailableError(f"Redis GET failed: {exc}") from exc

    async def set(self, key: str, value: str, ttl_seconds: int = 300) -> None:
        try:
            await self._redis.set(key, value, ex=ttl_seconds)
        except aioredis.RedisError as exc:
            raise StorageUnavailableError(f"Redis SET failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except aioredis.RedisError as exc:
            raise StorageUnavailableError(f"Redis DELETE failed: {exc}") from exc

    async def publish(self, channel: str, message: str) -> None:
        try:
            await self._redis.publish(channel, message)
        except aioredis.RedisError as exc:
            raise StorageUnavailableError(f"Redis PUBLISH failed: {exc}") from exc

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        """Subscribe to a Redis pub/sub channel. Yields messages as they arrive."""
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(channel)
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        yield message["data"]
            finally:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
        except aioredis.RedisError as exc:
            raise StorageUnavailableError(f"Redis SUBSCRIBE failed: {exc}") from exc

    async def close(self) -> None:
        await self._redis.aclose()
