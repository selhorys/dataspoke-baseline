"""Integration tests for Kafka consumer, handler dispatch, and example-kafka.

Prerequisites:
- DataHub Kafka port-forwarded on localhost:9005 (datahub-port-forward.sh)
- Example Kafka port-forwarded on localhost:9104 (dummy-data-port-forward.sh)

Test-specific data additions:
- Synthetic JSON MCL messages produced to MetadataChangeLog_Versioned_v1 and
  MetadataChangeLog_Timeseries_v1 topics referencing catalog.title_master.
  These are cleaned up by offset advancement; no dummy-data-reset needed.

Dummy-data dependencies:
- Example-kafka topics are reset for TestExampleKafkaIntegration tests.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition

from src.shared.datahub.events import (
    EventRouter,
    MetadataChangeLogEvent,
    build_router,
    deserialize_mcl,
    detect_new_clusters,
    sync_vector_index,
    update_health_score,
)

# Module-level dummy-data declaration: reset example-kafka topics before/after module.
DUMMY_DATA_TOPICS = frozenset(
    {
        "imazon.orders.events",
        "imazon.shipping.updates",
        "imazon.reviews.new",
    }
)

_VERSIONED_TOPIC = "MetadataChangeLog_Versioned_v1"
_TIMESERIES_TOPIC = "MetadataChangeLog_Timeseries_v1"

_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_mcl_payload(
    *,
    aspect_name: str = "datasetProperties",
    entity_type: str = "dataset",
    test_id: str = "",
) -> bytes:
    return json.dumps(
        {
            "entityType": entity_type,
            "entityUrn": _TEST_URN,
            "aspectName": aspect_name,
            "changeType": "UPSERT",
            "aspect": {"value": "integration-test", "testId": test_id},
            "created": {"time": 1700000000000},
        }
    ).encode()


def _make_mcl_event(
    *,
    aspect_name: str = "datasetProperties",
    entity_type: str = "dataset",
    entity_urn: str = _TEST_URN,
) -> MetadataChangeLogEvent:
    return MetadataChangeLogEvent(
        entity_type=entity_type,
        entity_urn=entity_urn,
        aspect_name=aspect_name,
        change_type="UPSERT",
    )


def _unique_group_id() -> str:
    return f"dataspoke-consumers-test-{uuid.uuid4().hex[:8]}"


def _make_consumer(brokers: str, group_id: str | None = None) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": brokers,
            "group.id": group_id or _unique_group_id(),
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "session.timeout.ms": 10000,
        }
    )


def _make_producer(brokers: str) -> Producer:
    return Producer({"bootstrap.servers": brokers})


def _wait_for_assignment(consumer: Consumer, *, max_wait_s: float = 10.0) -> None:
    """Poll until the consumer has at least one partition assigned.

    After assignment is received, performs additional polls so that
    librdkafka's internal partition fetch state initialises — without
    this, an immediate ``consumer.seek()`` can hit ``_STATE`` errors.
    """
    for _ in range(int(max_wait_s / 0.1)):
        consumer.poll(timeout=0.1)
        if consumer.assignment():
            # Extra polls to let fetch state settle before seek
            for _ in range(5):
                consumer.poll(timeout=0.1)
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
    max_wait_s: float = 30.0,
) -> "tuple[object, dict] | None":
    """Poll until we find the JSON message matching test_id, skipping Avro/binary messages.

    The versioned topic may contain real DataHub MCL events (Avro-encoded).
    This helper safely skips non-JSON messages and looks for our specific test payload.
    Call after _seek_to_end() to avoid scanning the full topic history.
    """
    for _ in range(int(max_wait_s / 0.2)):
        msg = consumer.poll(timeout=0.2)
        if msg is None or msg.error():
            continue
        try:
            data = json.loads(msg.value())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue  # skip Avro or other binary messages
        if data.get("aspect", {}).get("testId") == test_id:
            return msg, data
    return None


def _produce_or_skip(producer: Producer, topic: str, payload: bytes) -> None:
    """Produce a message to the given topic; pytest.skip if broker unreachable."""
    producer.produce(topic, value=payload)
    undelivered = producer.flush(timeout=10.0)
    if undelivered > 0:
        pytest.skip(
            "Kafka produce failed — broker likely advertises an internal "
            "K8s address not reachable from the test host"
        )


# ── DataHub Kafka Consumer Tests ──────────────────────────────────────────────


class TestKafkaConsumerIntegration:
    """Test MCL consumer connectivity and message processing against DataHub Kafka."""

    def test_consumer_connects_to_kafka(self, datahub_kafka_brokers: str) -> None:
        """Verify we can create a consumer and subscribe without connection errors."""
        consumer = _make_consumer(datahub_kafka_brokers)
        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            msg = consumer.poll(timeout=2.0)
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
            _produce_or_skip(producer, _VERSIONED_TOPIC, payload)

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
            _produce_or_skip(producer, _TIMESERIES_TOPIC, payload)

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
            _produce_or_skip(producer, _VERSIONED_TOPIC, payload)

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

    @pytest.mark.asyncio
    async def test_all_versioned_aspects_dispatch(self, datahub_kafka_brokers: str) -> None:
        """Produce MCL events for all 4 versioned aspects and verify routing."""
        versioned_aspects = ["datasetProperties", "schemaMetadata", "ownership", "globalTags"]
        group_id = _unique_group_id()
        producer = _make_producer(datahub_kafka_brokers)
        consumer = _make_consumer(datahub_kafka_brokers, group_id=group_id)

        try:
            consumer.subscribe([_VERSIONED_TOPIC])
            _wait_for_assignment(consumer)
            _seek_to_end(consumer)

            # Build router with mocked kestra client
            mock_kestra = AsyncMock()
            router = build_router(kestra_client=mock_kestra)

            for aspect in versioned_aspects:
                test_id = uuid.uuid4().hex[:8]
                payload = _make_mcl_payload(aspect_name=aspect, test_id=test_id)
                _produce_or_skip(producer, _VERSIONED_TOPIC, payload)

                result = _poll_for_test_message(consumer, test_id)
                assert result is not None, f"did not receive {aspect} message"
                received, _ = result
                event = deserialize_mcl(received.value())
                assert event.aspect_name == aspect

                # Dispatch through the full router (handlers call mocked kestra)
                with (
                    patch(
                        "src.backend.metrics.aggregator.aggregate_health_scores",
                        new_callable=AsyncMock,
                    ),
                    patch(
                        "src.shared.db.session.SessionLocal",
                        return_value=AsyncMock(),
                    ),
                    patch(
                        "src.workflows._common.make_datahub",
                    ),
                    patch(
                        "src.workflows._common.make_cache",
                    ),
                ):
                    await router.dispatch(event)

                consumer.commit(message=received)

            # Verify kestra executions were triggered for kestra-backed handlers.
            # ownership → update_health_score (no kestra), so only 3 aspects
            # trigger kestra: datasetProperties (1), schemaMetadata (2),
            # globalTags (1) = 4 execution triggers.
            assert mock_kestra.trigger_execution.await_count == 4
        finally:
            build_router()  # Reset _kestra_client
            consumer.close()


# ── Handler Dispatch Tests ────────────────────────────────────────────────────


class TestEventHandlerDispatch:
    """Test that MCL handlers delegate to the correct services/workflows.

    These tests verify the handler implementations using mocked downstream
    dependencies. They do not require real Kafka — only the handler functions.
    """

    @pytest.fixture(autouse=True)
    def _reset_router(self):
        """Reset the module-level _kestra_client after each test."""
        yield
        build_router()

    @pytest.mark.asyncio
    async def test_sync_vector_index_starts_embedding_workflow(self) -> None:
        """sync_vector_index triggers embedding-sync flow in single mode."""
        mock_kestra = AsyncMock()
        build_router(kestra_client=mock_kestra)

        event = _make_mcl_event(aspect_name="datasetProperties")
        await sync_vector_index(event)

        mock_kestra.trigger_execution.assert_awaited_once()
        call_args = mock_kestra.trigger_execution.call_args
        assert call_args.args[0] == "embedding-sync"
        assert "callback_base_url" in call_args.kwargs["inputs"]
        assert call_args.kwargs["labels"]["workflow_id"].startswith("embedding-sync-")

    @pytest.mark.asyncio
    async def test_detect_new_clusters_starts_ontology_workflow(self) -> None:
        """detect_new_clusters triggers ontology-rebuild flow with fixed ID."""
        mock_kestra = AsyncMock()
        build_router(kestra_client=mock_kestra)

        event = _make_mcl_event(aspect_name="schemaMetadata")
        await detect_new_clusters(event)

        mock_kestra.trigger_execution.assert_awaited_once()
        call_args = mock_kestra.trigger_execution.call_args
        assert call_args.args[0] == "ontology-rebuild"
        assert call_args.kwargs["labels"]["workflow_id"] == "ontology-rebuild"

    @pytest.mark.asyncio
    async def test_update_health_score_calls_aggregator(self) -> None:
        """update_health_score calls aggregate_health_scores directly."""
        mock_agg = AsyncMock()
        with (
            patch(
                "src.backend.metrics.aggregator.aggregate_health_scores",
                mock_agg,
            ),
            patch("src.shared.db.session.SessionLocal", return_value=AsyncMock()),
            patch("src.workflows._common.make_datahub"),
            patch("src.workflows._common.make_cache"),
        ):
            event = _make_mcl_event(aspect_name="ownership")
            await update_health_score(event)

        mock_agg.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_handler_schema_metadata_dispatch(self) -> None:
        """schemaMetadata dispatches to both sync_vector_index and detect_new_clusters."""
        mock_kestra = AsyncMock()
        router = build_router(kestra_client=mock_kestra)

        event = _make_mcl_event(aspect_name="schemaMetadata")
        await router.dispatch(event)

        # Two execution triggers: embedding-sync + ontology-rebuild
        assert mock_kestra.trigger_execution.await_count == 2

        call_flow_ids = [call.args[0] for call in mock_kestra.trigger_execution.call_args_list]
        assert "embedding-sync" in call_flow_ids
        assert "ontology-rebuild" in call_flow_ids

    @pytest.mark.asyncio
    async def test_non_dataset_entity_type_skipped(self) -> None:
        """Handlers skip events with non-dataset entity types."""
        mock_kestra = AsyncMock()
        build_router(kestra_client=mock_kestra)

        for aspect, handler in [
            ("datasetProperties", sync_vector_index),
            ("schemaMetadata", detect_new_clusters),
        ]:
            event = _make_mcl_event(
                aspect_name=aspect,
                entity_type="chart",
            )
            await handler(event)

        mock_kestra.trigger_execution.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handlers_noop_without_kestra_client(self) -> None:
        """Workflow-backed handlers are no-ops when no kestra client is configured."""
        build_router()  # No kestra_client

        event = _make_mcl_event(aspect_name="datasetProperties")
        # Should not raise or attempt any execution trigger
        await sync_vector_index(event)
        await detect_new_clusters(_make_mcl_event(aspect_name="schemaMetadata"))


# ── Example-Kafka Integration Tests ──────────────────────────────────────────


class TestExampleKafkaIntegration:
    """Test example-kafka (Imazon dummy data) connectivity and seed messages.

    These tests use the kafka_brokers fixture (port 9104) and depend on
    DUMMY_DATA_TOPICS being reset by the module_dummy_data autouse fixture.
    """

    def test_consumer_connects_to_example_kafka(self, kafka_brokers: str) -> None:
        """Verify we can connect to the example-kafka broker."""
        consumer = _make_consumer(kafka_brokers)
        try:
            consumer.subscribe(["imazon.orders.events"])
            msg = consumer.poll(timeout=2.0)
            if msg is not None and msg.error():
                assert msg.error().code() == KafkaError._PARTITION_EOF
        finally:
            consumer.close()

    @pytest.mark.parametrize(
        "topic,expected_count",
        [
            ("imazon.orders.events", 20),
            ("imazon.shipping.updates", 15),
            ("imazon.reviews.new", 10),
        ],
    )
    def test_consume_seed_messages(
        self,
        kafka_brokers: str,
        topic: str,
        expected_count: int,
    ) -> None:
        """Consume all seed messages from an Imazon topic and verify count."""
        consumer = _make_consumer(kafka_brokers)
        try:
            consumer.subscribe([topic])
            _wait_for_assignment(consumer)
            # Seek to beginning — _wait_for_assignment may have consumed messages
            for tp in consumer.assignment():
                consumer.seek(TopicPartition(tp.topic, tp.partition, 0))

            messages: list[dict] = []
            empty_polls = 0
            while empty_polls < 10:
                msg = consumer.poll(timeout=0.2)
                if msg is None:
                    empty_polls += 1
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        break
                    continue
                empty_polls = 0
                data = json.loads(msg.value())
                messages.append(data)

            assert len(messages) == expected_count
        finally:
            consumer.close()

    def test_seed_messages_match_fixtures(self, kafka_brokers: str) -> None:
        """Verify consumed messages match the JSONL fixture data."""
        from tests.integration.util.kafka import load_seed_messages

        topic = "imazon.orders.events"
        expected = load_seed_messages(topic)

        consumer = _make_consumer(kafka_brokers)
        try:
            consumer.subscribe([topic])
            _wait_for_assignment(consumer)
            # Seek to beginning — _wait_for_assignment may have consumed messages
            for tp in consumer.assignment():
                consumer.seek(TopicPartition(tp.topic, tp.partition, 0))

            consumed: list[dict] = []
            empty_polls = 0
            while empty_polls < 10 and len(consumed) < len(expected):
                msg = consumer.poll(timeout=0.2)
                if msg is None:
                    empty_polls += 1
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        break
                    continue
                empty_polls = 0
                consumed.append(json.loads(msg.value()))

            assert len(consumed) == len(expected)
            # Compare by order_id to verify content (order-preserving within partition)
            consumed_ids = [m.get("order_id") for m in consumed]
            expected_ids = [m.get("order_id") for m in expected]
            assert consumed_ids == expected_ids
        finally:
            consumer.close()

    def test_orders_contain_expected_fields(self, kafka_brokers: str) -> None:
        """Verify that order events have the expected schema fields."""
        consumer = _make_consumer(kafka_brokers)
        try:
            consumer.subscribe(["imazon.orders.events"])
            _wait_for_assignment(consumer)

            msg = consumer.poll(timeout=2.0)
            if msg is None or msg.error():
                pytest.skip("No messages available in orders topic")

            data = json.loads(msg.value())
            # Orders should have standard e-commerce event fields
            assert "order_id" in data
            assert "event_type" in data
            assert "timestamp" in data
        finally:
            consumer.close()
