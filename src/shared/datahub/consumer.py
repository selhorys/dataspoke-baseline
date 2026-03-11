"""Kafka consumer entry point for DataHub MetadataChangeLog events.

Subscribes to MCL topics, deserializes events, and routes them through
the EventRouter. Commits offsets only after successful processing.

Usage:
    python -m src.shared.datahub.consumer
"""

import asyncio

import structlog
from confluent_kafka import Consumer, KafkaError

from src.api.config import settings
from src.shared.config import CONSUMER_POLL_TIMEOUT_S
from src.shared.datahub.events import build_router, deserialize_mcl
from src.shared.exceptions import EventProcessingError

logger = structlog.get_logger(__name__)

MCL_TOPICS = [
    "MetadataChangeLog_Versioned_v1",
    "MetadataChangeLog_Timeseries_v1",
]


async def run_consumer() -> None:
    """Main consumer loop — subscribe, poll, route, commit."""
    consumer = Consumer(
        {
            "bootstrap.servers": settings.datahub_kafka_brokers,
            "group.id": "dataspoke-consumers",
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "max.poll.interval.ms": 300000,
        }
    )

    consumer.subscribe(MCL_TOPICS)
    logger.info("consumer_started", topics=MCL_TOPICS)

    router = build_router()

    try:
        while True:
            msg = await asyncio.to_thread(consumer.poll, CONSUMER_POLL_TIMEOUT_S)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.warning("consumer_error", error=str(msg.error()))
                continue

            try:
                event = deserialize_mcl(msg.value())
                await router.dispatch(event)
                consumer.commit(message=msg)
            except EventProcessingError:
                logger.exception(
                    "event_deserialization_failed",
                    topic=msg.topic(),
                    partition=msg.partition(),
                    offset=msg.offset(),
                )
                # Skip malformed messages — commit to avoid redelivery loop
                consumer.commit(message=msg)
            except Exception:
                logger.exception(
                    "event_processing_failed",
                    topic=msg.topic(),
                    partition=msg.partition(),
                    offset=msg.offset(),
                )
                # Do NOT commit — event will be redelivered
    finally:
        consumer.close()
        logger.info("consumer_stopped")


if __name__ == "__main__":
    asyncio.run(run_consumer())
