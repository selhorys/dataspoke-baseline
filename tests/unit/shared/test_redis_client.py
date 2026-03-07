"""Unit tests for RedisClient — no real Redis connection needed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.cache.client import RedisClient
from src.shared.exceptions import StorageUnavailableError


@pytest.fixture
def mock_redis():
    with patch("src.shared.cache.client.aioredis.Redis") as cls:
        instance = AsyncMock()
        # pubsub() is a sync method on the redis client that returns a PubSub object
        instance.pubsub = MagicMock()
        cls.return_value = instance
        yield instance


def _make_client(mock_redis) -> RedisClient:
    return RedisClient(host="localhost", port=6379, password="")


@pytest.mark.asyncio
async def test_get_returns_value(mock_redis):
    mock_redis.get.return_value = "cached_value"
    client = _make_client(mock_redis)

    result = await client.get("test:key")

    assert result == "cached_value"
    mock_redis.get.assert_awaited_once_with("test:key")


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_key(mock_redis):
    mock_redis.get.return_value = None
    client = _make_client(mock_redis)

    result = await client.get("missing:key")

    assert result is None


@pytest.mark.asyncio
async def test_set_with_ttl(mock_redis):
    client = _make_client(mock_redis)

    await client.set("test:key", "value", ttl_seconds=60)

    mock_redis.set.assert_awaited_once_with("test:key", "value", ex=60)


@pytest.mark.asyncio
async def test_set_default_ttl(mock_redis):
    client = _make_client(mock_redis)

    await client.set("test:key", "value")

    mock_redis.set.assert_awaited_once_with("test:key", "value", ex=300)


@pytest.mark.asyncio
async def test_delete(mock_redis):
    client = _make_client(mock_redis)

    await client.delete("test:key")

    mock_redis.delete.assert_awaited_once_with("test:key")


@pytest.mark.asyncio
async def test_publish(mock_redis):
    client = _make_client(mock_redis)

    await client.publish("channel", "msg")

    mock_redis.publish.assert_awaited_once_with("channel", "msg")


@pytest.mark.asyncio
async def test_get_raises_storage_unavailable_on_error(mock_redis):
    import redis.asyncio as aioredis

    mock_redis.get.side_effect = aioredis.RedisError("connection lost")
    client = _make_client(mock_redis)

    with pytest.raises(StorageUnavailableError, match="Redis GET failed"):
        await client.get("key")


@pytest.mark.asyncio
async def test_set_raises_storage_unavailable_on_error(mock_redis):
    import redis.asyncio as aioredis

    mock_redis.set.side_effect = aioredis.RedisError("connection lost")
    client = _make_client(mock_redis)

    with pytest.raises(StorageUnavailableError, match="Redis SET failed"):
        await client.set("key", "val")


@pytest.mark.asyncio
async def test_subscribe_yields_messages(mock_redis):
    pubsub = MagicMock()

    async def fake_listen():
        yield {"type": "subscribe", "data": None}
        yield {"type": "message", "data": "hello"}
        yield {"type": "message", "data": "world"}

    pubsub.listen = fake_listen
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    mock_redis.pubsub.return_value = pubsub

    client = _make_client(mock_redis)
    messages = []
    async for msg in client.subscribe("chan"):
        messages.append(msg)
        if len(messages) == 2:
            break

    assert messages == ["hello", "world"]


@pytest.mark.asyncio
async def test_close(mock_redis):
    client = _make_client(mock_redis)
    await client.close()
    mock_redis.aclose.assert_awaited_once()
