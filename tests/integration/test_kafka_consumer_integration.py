"""Integration tests for the Kafka MCL consumer against dev-env Kafka.

Prerequisites:
- Kafka port-forwarded on localhost:9005 (datahub-port-forward.sh)

Test-specific data additions:
- Synthetic JSON MCL messages produced to MetadataChangeLog_Versioned_v1 and
  MetadataChangeLog_Timeseries_v1 topics referencing catalog.title_master.
  These are cleaned up by offset advancement; no dummy-data-reset needed.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition

from src.shared.datahub.events import EventRouter, deserialize_mcl

_VERSIONED_TOPIC = "MetadataChangeLog_Versioned_v1"
_TIMESERIES_TOPIC = "MetadataChangeLog_Timeseries_v1"


def _make_mcl_payload(
    *,
    aspect_name: str = "datasetProperties",
    entity_type: str = "dataset",
    test_id: str = "",
) -> bytes:
    return json.dumps(
        {
            "entityType": entity_type,
            "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
            "aspectName": aspect_name,
            "changeType": "UPSERT",
            "aspect": {"value": "integration-test", "testId": test_id},
            "created": {"time": 1700000000000},
        }
    ).encode()


def _unique_group_id() -> str:
    return f"dataspoke-consumers-test-{uuid.uuid4().hex[:8]}"


def _make_consumer(datahub_kafka_brokers: str, group_id: str | None = None) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": datahub_kafka_brokers,
            "group.id": group_id or _unique_group_id(),
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "session.timeout.ms": 10000,
        }
    )


def _make_producer(datahub_kafka_brokers: str) -> Producer:
    return Producer({"bootstrap.servers": datahub_kafka_brokers})


def _wait_for_assignment(consumer: Consumer, *, max_polls: int = 10) -> None:
    """Poll until the consumer has at least one partition assigned."""
    for _ in range(max_polls):
        consumer.poll(timeout=1.0)
        if consumer.assignment():
            return
    pytest.skip("Consumer never received partition assignment")


def _seek_to_end(consumer: Consumer) -> list[TopicPartition]:
    """Seek all assigned partitions to the high watermark.

    Returns the list of TopicPartitions at the end-of-log positions.
    Must be called after _wait_for_assignment().
    """
    partitions = consumer.assignment()
    end_offsets = []
    for tp in partitions:
        _, high = consumer.get_watermark_offsets(tp)
        seek_tp = TopicPartition(tp.topic, tp.partition, high)
        consumer.seek(seek_tp)
        end_offsets.append(seek_tp)
    return end_offsets


def _poll_for_test_message(
    consumer: Consumer,
    test_id: str,
    *,
    max_polls: int = 30,
) -> "tuple[object, dict] | None":
    """Poll until we find the JSON message matching test_id, skipping Avro/binary messages.

    The versioned topic may contain real DataHub MCL events (Avro-encoded).
    This helper safely skips non-JSON messages and looks for our specific test payload.
    Call after _seek_to_end() to avoid scanning the full topic history.
    """
    for _ in range(max_polls):
        msg = consumer.poll(timeout=1.0)
        if msg is None or msg.error():
            continue
        try:
            data = json.loads(msg.value())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue  # skip Avro or other binary messages
        if data.get("aspect", {}).get("testId") == test_id:
            return msg, data
    return None


# ── Tests ────────────────────────────────────────────────────────────────────


class TestKafkaConsumerIntegration:
    def test_consumer_connects_to_kafka(self, datahub_kafka_brokers: str) -> None:
        """Verify we can create a consumer and subscribe without connection errors."""
        consumer = _make_consumer(datahub_kafka_brokers)
        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            msg = consumer.poll(timeout=5.0)
            # msg is None (no messages) or a valid message — either is fine
            if msg is not None and msg.error():
                assert msg.error().code() == KafkaError._PARTITION_EOF
        finally:
            consumer.close()

    def test_consume_versioned_mcl_event(self, datahub_kafka_brokers: str) -> None:
        """Produce a test MCL to the versioned topic and consume it."""
        group_id = _unique_group_id()
        test_id = uuid.uuid4().hex[:8]
        producer = _make_producer(datahub_kafka_brokers)
        consumer = _make_consumer(datahub_kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            _wait_for_assignment(consumer)
            _seek_to_end(consumer)

            payload = _make_mcl_payload(aspect_name="datasetProperties", test_id=test_id)
            producer.produce(_VERSIONED_TOPIC, value=payload)
            undelivered = producer.flush(timeout=10.0)
            if undelivered > 0:
                pytest.skip(
                    "Kafka produce failed — broker likely advertises an internal "
                    "K8s address not reachable from the test host"
                )

            result = _poll_for_test_message(consumer, test_id)
            assert result is not None, "did not receive produced message within timeout"
            received, _ = result
            event = deserialize_mcl(received.value())
            assert event.entity_type == "dataset"
            assert event.aspect_name == "datasetProperties"
            consumer.commit(message=received)
        finally:
            consumer.close()

    def test_consume_timeseries_mcl_event(self, datahub_kafka_brokers: str) -> None:
        """Produce a test MCL to the timeseries topic and consume it."""
        group_id = _unique_group_id()
        test_id = uuid.uuid4().hex[:8]
        producer = _make_producer(datahub_kafka_brokers)
        consumer = _make_consumer(datahub_kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_TIMESERIES_TOPIC])
            _wait_for_assignment(consumer)
            _seek_to_end(consumer)

            payload = _make_mcl_payload(aspect_name="datasetProfile", test_id=test_id)
            producer.produce(_TIMESERIES_TOPIC, value=payload)
            undelivered = producer.flush(timeout=10.0)
            if undelivered > 0:
                pytest.skip(
                    "Kafka produce failed — broker likely advertises an internal "
                    "K8s address not reachable from the test host"
                )

            result = _poll_for_test_message(consumer, test_id)
            assert result is not None, "did not receive produced message within timeout"
            received, _ = result
            event = deserialize_mcl(received.value())
            assert event.aspect_name == "datasetProfile"
            consumer.commit(message=received)
        finally:
            consumer.close()

    @pytest.mark.asyncio
    async def test_handler_failure_skips_commit(self, datahub_kafka_brokers: str) -> None:
        """When a handler fails, offset should NOT be committed."""
        group_id = _unique_group_id()
        test_id = uuid.uuid4().hex[:8]
        producer = _make_producer(datahub_kafka_brokers)
        consumer = _make_consumer(datahub_kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            _wait_for_assignment(consumer)
            # Seek to end and commit those positions so the consumer group has
            # a baseline committed offset just before our test message.
            end_offsets = _seek_to_end(consumer)
            consumer.commit(offsets=end_offsets, asynchronous=False)

            payload = _make_mcl_payload(aspect_name="ownership", test_id=test_id)
            producer.produce(_VERSIONED_TOPIC, value=payload)
            undelivered = producer.flush(timeout=10.0)
            if undelivered > 0:
                pytest.skip(
                    "Kafka produce failed — broker likely advertises an internal "
                    "K8s address not reachable from the test host"
                )

            # Consume first time
            result = _poll_for_test_message(consumer, test_id)
            assert result is not None, "did not receive produced message"
            received, _ = result

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
            # because the committed offset is still at the pre-test-message position
            consumer.close()
            consumer = _make_consumer(datahub_kafka_brokers, group_id=group_id)
            consumer.subscribe([_VERSIONED_TOPIC])

            result = _poll_for_test_message(consumer, test_id)
            assert result is not None, "message was not redelivered after skipped commit"
            redelivered, _ = result
            consumer.commit(message=redelivered)
        finally:
            consumer.close()
