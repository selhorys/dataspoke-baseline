"""Integration tests for the Kafka MCL consumer against dev-env Kafka.

Prerequisites:
- Kafka port-forwarded on localhost:9005 (datahub-port-forward.sh)
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from confluent_kafka import Consumer, KafkaError, Producer

from src.shared.datahub.events import EventRouter, deserialize_mcl

_VERSIONED_TOPIC = "MetadataChangeLog_Versioned_v1"
_TIMESERIES_TOPIC = "MetadataChangeLog_Timeseries_v1"


def _make_mcl_payload(
    *,
    aspect_name: str = "datasetProperties",
    entity_type: str = "dataset",
) -> bytes:
    return json.dumps(
        {
            "entityType": entity_type,
            "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.public.users,PROD)",
            "aspectName": aspect_name,
            "changeType": "UPSERT",
            "aspect": {"value": "integration-test"},
            "created": {"time": 1700000000000},
        }
    ).encode()


def _unique_group_id() -> str:
    return f"dataspoke-consumers-test-{uuid.uuid4().hex[:8]}"


def _make_consumer(kafka_brokers: str, group_id: str | None = None) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": kafka_brokers,
            "group.id": group_id or _unique_group_id(),
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "session.timeout.ms": 10000,
        }
    )


def _make_producer(kafka_brokers: str) -> Producer:
    return Producer({"bootstrap.servers": kafka_brokers})


# ── Tests ────────────────────────────────────────────────────────────────────


class TestKafkaConsumerIntegration:
    def test_consumer_connects_to_kafka(self, kafka_brokers: str) -> None:
        """Verify we can create a consumer and subscribe without connection errors."""
        consumer = _make_consumer(kafka_brokers)
        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            msg = consumer.poll(timeout=5.0)
            # msg is None (no messages) or a valid message — either is fine
            if msg is not None and msg.error():
                assert msg.error().code() == KafkaError._PARTITION_EOF
        finally:
            consumer.close()

    def test_consume_versioned_mcl_event(self, kafka_brokers: str) -> None:
        """Produce a test MCL to the versioned topic and consume it."""
        group_id = _unique_group_id()
        producer = _make_producer(kafka_brokers)
        consumer = _make_consumer(kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            # Prime consumer assignment
            consumer.poll(timeout=3.0)

            # Produce test message
            payload = _make_mcl_payload(aspect_name="datasetProperties")
            producer.produce(_VERSIONED_TOPIC, value=payload)
            undelivered = producer.flush(timeout=10.0)
            if undelivered > 0:
                pytest.skip(
                    "Kafka produce failed — broker likely advertises an internal "
                    "K8s address not reachable from the test host"
                )

            # Consume and verify
            received = None
            for _ in range(20):
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    continue
                received = msg
                break

            assert received is not None, "did not receive produced message within timeout"
            event = deserialize_mcl(received.value())
            assert event.entity_type == "dataset"
            assert event.aspect_name == "datasetProperties"
            consumer.commit(message=received)
        finally:
            consumer.close()

    def test_consume_timeseries_mcl_event(self, kafka_brokers: str) -> None:
        """Produce a test MCL to the timeseries topic and consume it."""
        group_id = _unique_group_id()
        producer = _make_producer(kafka_brokers)
        consumer = _make_consumer(kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_TIMESERIES_TOPIC])
            consumer.poll(timeout=3.0)

            payload = _make_mcl_payload(aspect_name="datasetProfile")
            producer.produce(_TIMESERIES_TOPIC, value=payload)
            undelivered = producer.flush(timeout=10.0)
            if undelivered > 0:
                pytest.skip(
                    "Kafka produce failed — broker likely advertises an internal "
                    "K8s address not reachable from the test host"
                )

            received = None
            for _ in range(20):
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    continue
                received = msg
                break

            assert received is not None, "did not receive produced message within timeout"
            event = deserialize_mcl(received.value())
            assert event.aspect_name == "datasetProfile"
            consumer.commit(message=received)
        finally:
            consumer.close()

    @pytest.mark.asyncio
    async def test_handler_failure_skips_commit(self, kafka_brokers: str) -> None:
        """When a handler fails, offset should NOT be committed."""
        group_id = _unique_group_id()
        producer = _make_producer(kafka_brokers)
        consumer = _make_consumer(kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            consumer.poll(timeout=3.0)

            payload = _make_mcl_payload(aspect_name="ownership")
            producer.produce(_VERSIONED_TOPIC, value=payload)
            undelivered = producer.flush(timeout=10.0)
            if undelivered > 0:
                pytest.skip(
                    "Kafka produce failed — broker likely advertises an internal "
                    "K8s address not reachable from the test host"
                )

            # Consume first time
            received = None
            for _ in range(20):
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    continue
                received = msg
                break

            assert received is not None, "did not receive produced message"

            # Simulate handler failure — do NOT commit
            failing_handler = AsyncMock(side_effect=RuntimeError("handler failed"))
            router = EventRouter()
            router.register("ownership", failing_handler)

            event = deserialize_mcl(received.value())
            try:
                await router.dispatch(event)
            except RuntimeError:
                pass
            # Intentionally NOT committing offset

            # Re-create consumer with same group — message should be redelivered
            consumer.close()
            consumer = _make_consumer(kafka_brokers, group_id=group_id)
            consumer.subscribe([_VERSIONED_TOPIC])

            redelivered = None
            for _ in range(20):
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    continue
                redelivered = msg
                break

            assert redelivered is not None, "message was not redelivered after skipped commit"
            consumer.commit(message=redelivered)
        finally:
            consumer.close()
