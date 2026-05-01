"""Tests for src/shared/cache/client.py — Redis client wrapper.

Verifies contracts in spec/feature/BACKEND.md §Shared Services (Redis row) and
§Cache Key Conventions (L138-L148): async get/set/delete/publish/subscribe
operations, default TTL, and cache key format strings.

Spec-anchored TTLs (BACKEND.md L142-L147):
  - validation:{dataset_urn}:result  → 60s
  - rate_limit:{user_id}             → 60s
  - ontogen:node/edge/triple:{id}    → 300s

Keys NOT in spec (impl-only, no spec backing):
  - QUALITY_CACHE_KEY  (quality:{dataset_urn}:score)  — no spec row
  - SEARCH_CACHE_KEY   (search:{query_hash})           — no spec row

The RedisClient.set() default TTL is 300s, which is correct for ontogen paths
but does NOT match the 60s TTL required by spec for validation and rate-limit
paths. Callers must pass ttl_seconds=60 explicitly for those paths."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.cache.client import (
    QUALITY_CACHE_KEY,
    RATE_LIMIT_KEY,
    SEARCH_CACHE_KEY,
    VALIDATION_CACHE_KEY,
    RedisClient,
)


@pytest.fixture
def mock_redis():
    with patch("src.shared.cache.client.aioredis.Redis") as mock_cls:
        instance = AsyncMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def client(mock_redis):
    return RedisClient(host="localhost", port=6379, password="test")


async def test_get_returns_value(client, mock_redis) -> None:
    mock_redis.get.return_value = "cached_value"
    result = await client.get("test_key")
    assert result == "cached_value"
    mock_redis.get.assert_awaited_once_with("test_key")


async def test_get_returns_none_on_miss(client, mock_redis) -> None:
    mock_redis.get.return_value = None
    result = await client.get("missing_key")
    assert result is None


async def test_set_with_default_ttl(client, mock_redis) -> None:
    # WARN: 300s default contradicts BACKEND.md L142 (validation cache should be 60s).
    # Surfaces impl gap — default may not be correct for validation paths.
    # Ontogen paths (300s) are spec-correct; callers must pass ttl_seconds=60 for
    # validation:{dataset_urn}:result and rate_limit:{user_id} keys.
    await client.set("key", "value")
    mock_redis.set.assert_awaited_once_with("key", "value", ex=300)


async def test_set_with_custom_ttl(client, mock_redis) -> None:
    await client.set("key", "value", ttl_seconds=60)
    mock_redis.set.assert_awaited_once_with("key", "value", ex=60)


async def test_delete(client, mock_redis) -> None:
    await client.delete("key")
    mock_redis.delete.assert_awaited_once_with("key")


async def test_publish(client, mock_redis) -> None:
    await client.publish("channel", "hello")
    mock_redis.publish.assert_awaited_once_with("channel", "hello")


async def test_subscribe_yields_messages(client, mock_redis) -> None:
    pubsub = AsyncMock()
    # redis.asyncio.Redis.pubsub() is synchronous, returns a PubSub object
    mock_redis.pubsub = MagicMock(return_value=pubsub)

    async def mock_listen():
        yield {"type": "subscribe", "data": 1}
        yield {"type": "message", "data": "msg1"}
        yield {"type": "message", "data": "msg2"}

    pubsub.listen = mock_listen

    messages = []
    async for msg in client.subscribe("test_channel"):
        messages.append(msg)
        if len(messages) == 2:
            break

    assert messages == ["msg1", "msg2"]
    pubsub.subscribe.assert_awaited_once_with("test_channel")


def test_cache_key_formatting() -> None:
    """Verify cache key format strings against spec/feature/BACKEND.md §Cache Key Conventions.

    VALIDATION_CACHE_KEY and RATE_LIMIT_KEY are spec-anchored (BACKEND.md L142, L147).
    QUALITY_CACHE_KEY and SEARCH_CACHE_KEY are impl-pinned with no spec row — they
    exercise what the impl exports but cannot be fully validated against the spec table.
    """
    # Spec-anchored (BACKEND.md L142): validation:{dataset_urn}:result
    assert (
        VALIDATION_CACHE_KEY.format(dataset_urn="urn:li:dataset:x")
        == "validation:urn:li:dataset:x:result"
    )
    # Impl-pinned: quality:{dataset_urn}:score — NOT in BACKEND.md §Cache Key Conventions
    assert (
        QUALITY_CACHE_KEY.format(dataset_urn="urn:li:dataset:x") == "quality:urn:li:dataset:x:score"
    )
    # Impl-pinned: search:{query_hash} — NOT in BACKEND.md §Cache Key Conventions
    assert SEARCH_CACHE_KEY.format(query_hash="abc123") == "search:abc123"
    # Spec-anchored (BACKEND.md L147): rate_limit:{user_id}
    assert RATE_LIMIT_KEY.format(user_id="user-1") == "rate_limit:user-1"
