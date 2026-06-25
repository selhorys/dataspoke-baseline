"""Tests for src/shared/cache/client.py — Redis client wrapper.

Verifies contracts in spec/feature/BACKEND.md §Shared Services (Redis row) and
§Cache Key Conventions: async get/set/delete/publish/subscribe operations and
cache key format strings.

Spec-anchored TTLs (BACKEND.md §Cache Key Conventions):
  - validation:{dataset_urn}:result  → 60s
  - quality:{dataset_urn}:score      → 300s
  - rate_limit:{user_id}             → 60s
  - ontogen:node/edge/triple:{id}    → 300s
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.shared.cache.client import (
    QUALITY_CACHE_KEY,
    RATE_LIMIT_KEY,
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


@pytest.fixture
def real_client_kwargs():
    """Connection kwargs from a *real* (unmocked) RedisClient.

    The redis-py async ctor only records connection parameters into the pool;
    it opens no socket until first command, so constructing it here is safe and
    lets us assert the resilience knobs that were actually passed through.
    """
    client = RedisClient(host="localhost", port=6379, password="test")
    return client._redis.connection_pool.connection_kwargs


async def test_get_returns_value(client, mock_redis) -> None:
    mock_redis.get.return_value = "cached_value"
    result = await client.get("test_key")
    assert result == "cached_value"
    mock_redis.get.assert_awaited_once_with("test_key")


async def test_get_returns_none_on_miss(client, mock_redis) -> None:
    mock_redis.get.return_value = None
    result = await client.get("missing_key")
    assert result is None


async def test_set_requires_explicit_ttl(client, mock_redis) -> None:
    """ttl_seconds is keyword-only and required — there is no default.

    spec/feature/BACKEND.md §Cache Key Conventions defines per-key TTLs
    (60s/300s); passing the wrong default would silently mis-cache.
    """
    with pytest.raises(TypeError):
        await client.set("key", "value")  # type: ignore[call-arg]
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
    """Verify cache key format strings against spec/feature/BACKEND.md §Cache Key Conventions."""
    assert (
        VALIDATION_CACHE_KEY.format(dataset_urn="urn:li:dataset:x")
        == "validation:urn:li:dataset:x:result"
    )
    assert (
        QUALITY_CACHE_KEY.format(dataset_urn="urn:li:dataset:x") == "quality:urn:li:dataset:x:score"
    )
    assert RATE_LIMIT_KEY.format(user_id="user-1") == "rate_limit:user-1"


# ── Connection resilience knobs ───────────────────────────────────────────────
#
# These pin the resilience contract from the backend hardening change: a Redis
# blip or pod move must be recovered in-place instead of hanging or surfacing as
# an unretried error. They introspect the pool's recorded connection_kwargs, which
# is where redis-py stores the parameters handed to the async Redis ctor.


def test_socket_timeouts_configured(real_client_kwargs) -> None:
    """Bounded socket timeouts so a vanished Redis (pod reschedule, network
    partition) fails fast at 5s instead of hanging a request indefinitely on a
    dead connection."""
    assert real_client_kwargs["socket_connect_timeout"] == 5
    assert real_client_kwargs["socket_timeout"] == 5


def test_socket_keepalive_enabled(real_client_kwargs) -> None:
    """TCP keepalive so a connection silently dropped by an intermediary (NAT/LB
    idle reap after a Redis move) is detected rather than reused as a black hole."""
    assert real_client_kwargs["socket_keepalive"] is True


def test_health_check_interval(real_client_kwargs) -> None:
    """Periodic health pings (every 30s) so an idle pooled connection invalidated
    by a backend move is proactively validated before a caller checks it out."""
    assert real_client_kwargs["health_check_interval"] == 30


def test_retry_configured_with_exponential_backoff(real_client_kwargs) -> None:
    """Transparent retry with exponential backoff so a transient connection error
    during a Redis blip is recovered in-place instead of bubbling to the caller.

    The backoff-class check is the revert sentinel: redis 7.4.0's DEFAULT retry
    backoff is ExponentialWithJitterBackoff, which is NOT a subclass of
    ExponentialBackoff, so this assertion fails if the explicit Retry config is
    dropped. The retries==3 check alone cannot detect a revert because 3 is also
    the redis default; the backoff-class check is what catches it.
    """
    retry = real_client_kwargs["retry"]
    assert isinstance(retry, Retry)
    assert isinstance(retry._backoff, ExponentialBackoff)
    assert retry._retries == 3


def test_retry_on_connection_and_timeout_errors(real_client_kwargs) -> None:
    """Retry is armed for the two transient failures a Redis move produces —
    ConnectionError and TimeoutError — so they are absorbed by the retry loop
    rather than propagated as request failures."""
    retry_on_error = real_client_kwargs["retry_on_error"]
    assert RedisConnectionError in retry_on_error
    assert RedisTimeoutError in retry_on_error


def test_decode_responses_preserved(real_client_kwargs) -> None:
    """decode_responses stays True so the resilience change did not silently flip
    return types from str to bytes and break every caller that reads cache values."""
    assert real_client_kwargs["decode_responses"] is True
