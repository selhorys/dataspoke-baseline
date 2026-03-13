"""Integration tests for WebSocket feed mechanism via Redis pub/sub.

Prerequisites:
- Redis port-forwarded to localhost:9202 (dataspoke-port-forward.sh)

These tests verify that Redis pub/sub channels used by the WebSocket feed
handlers work correctly end-to-end. No dummy-data dependencies — Redis only.
"""

import asyncio
import json

import pytest
import pytest_asyncio

from src.shared.cache.client import RedisClient


@pytest_asyncio.fixture
async def publisher(redis_client: RedisClient) -> RedisClient:
    """Separate Redis client for publishing (subscriber blocks the other)."""
    # redis_client from conftest.py is the subscriber; we need a separate
    # connection for publishing since subscribe() blocks the connection.
    from tests.integration.conftest import _redis_host, _redis_password, _redis_port

    client = RedisClient(host=_redis_host, port=_redis_port, password=_redis_password)
    yield client
    await client.close()


class TestRedisPubSubValidation:
    """Verify Redis pub/sub for the validation progress channel."""

    @pytest.mark.asyncio
    async def test_subscribe_receives_messages_in_order(
        self, redis_client: RedisClient, publisher: RedisClient
    ) -> None:
        test_urn = "urn:li:dataset:(urn:li:dataPlatform:mysql,test.ws_feed,PROD)"
        channel = f"ws:validation:{test_urn}"

        messages_to_send = [
            {"type": "progress", "step": "fetch_aspects", "pct": 20, "msg": "Fetching"},
            {"type": "progress", "step": "check_freshness", "pct": 60, "msg": "Checking"},
            {"type": "result", "status": "completed", "quality_score": 85, "issues": []},
        ]

        received: list[dict] = []

        async def _subscribe_and_collect() -> None:
            async for raw in redis_client.subscribe(channel):
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "result":
                    break

        # Start subscriber, give it time to register, then publish.
        sub_task = asyncio.create_task(_subscribe_and_collect())
        await asyncio.sleep(0.2)

        for msg in messages_to_send:
            await publisher.publish(channel, json.dumps(msg))

        await asyncio.wait_for(sub_task, timeout=5.0)

        assert len(received) == 3
        assert received[0]["type"] == "progress"
        assert received[0]["step"] == "fetch_aspects"
        assert received[1]["type"] == "progress"
        assert received[2]["type"] == "result"
        assert received[2]["quality_score"] == 85


class TestRedisPubSubMetrics:
    """Verify Redis pub/sub for the metric updates channel."""

    @pytest.mark.asyncio
    async def test_subscribe_receives_multiple_updates(
        self, redis_client: RedisClient, publisher: RedisClient
    ) -> None:
        channel = "ws:metric:updates"

        messages_to_send = [
            {"type": "metric_update", "metric_id": "m1", "value": 42.0},
            {"type": "metric_update", "metric_id": "m2", "value": 99.5},
        ]

        received: list[dict] = []
        expected_count = len(messages_to_send)

        async def _subscribe_and_collect() -> None:
            async for raw in redis_client.subscribe(channel):
                received.append(json.loads(raw))
                if len(received) >= expected_count:
                    break

        sub_task = asyncio.create_task(_subscribe_and_collect())
        await asyncio.sleep(0.2)

        for msg in messages_to_send:
            await publisher.publish(channel, json.dumps(msg))

        await asyncio.wait_for(sub_task, timeout=5.0)

        assert len(received) == 2
        assert received[0]["metric_id"] == "m1"
        assert received[0]["value"] == 42.0
        assert received[1]["metric_id"] == "m2"
        assert received[1]["value"] == 99.5
