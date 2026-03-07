"""Kafka MCL consumer base with aspect-based routing."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class EventRouter:
    """Routes DataHub MetadataChangeLog events to registered handlers by aspect name."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def register(self, aspect_name: str, handler: EventHandler) -> None:
        """Register a handler for a specific aspect name."""
        self._handlers.setdefault(aspect_name, []).append(handler)

    async def dispatch(self, event: dict[str, Any]) -> None:
        """Route an MCL event to the appropriate handlers based on aspect name."""
        aspect_name = event.get("aspectName")
        if not aspect_name:
            logger.debug("Ignoring event without aspectName: %s", event)
            return

        handlers = self._handlers.get(aspect_name, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "Handler %s failed for aspect %s",
                    handler.__name__,
                    aspect_name,
                )


def create_kafka_consumer(
    brokers: str, topic: str = "MetadataChangeLog_Versioned_v1", group_id: str = "dataspoke"
) -> Any:
    """Create a confluent-kafka Consumer for MCL events.

    Returns a confluent_kafka.Consumer instance configured for the given brokers.
    The caller is responsible for polling and closing the consumer.
    """
    from confluent_kafka import Consumer

    return Consumer(
        {
            "bootstrap.servers": brokers,
            "group.id": group_id,
            "auto.offset.reset": "latest",
            "enable.auto.commit": True,
        },
    )
