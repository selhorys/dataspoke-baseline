"""MCL event deserialization, aspect-based event router, and handler stubs.

DataHub publishes MetadataChangeLog events to Kafka topics. This module
deserializes those events, routes them by aspect name to registered handlers,
and provides thin handler stubs that delegate to existing services.
"""

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from pydantic import BaseModel

from src.shared.config import HANDLER_TIMEOUT_S
from src.shared.exceptions import (
    DataHubUnavailableError,
    EventProcessingError,
    StorageUnavailableError,
)

logger = structlog.get_logger(__name__)

# Type alias for async handler functions
Handler = Callable[["MetadataChangeLogEvent"], Coroutine[Any, Any, None]]


# ── MCL Pydantic Model ──────────────────────────────────────────────────────


class MetadataChangeLogEvent(BaseModel):
    """Deserialized MetadataChangeLog event from Kafka."""

    entity_type: str
    entity_urn: str
    aspect_name: str
    change_type: str
    aspect: dict[str, Any] | None = None
    created: dict[str, Any] | None = None


def deserialize_mcl(raw: bytes) -> MetadataChangeLogEvent:
    """Parse raw Kafka message value into a MetadataChangeLogEvent."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise EventProcessingError(f"invalid MCL JSON: {exc}") from exc

    # DataHub MCL fields use camelCase; map to snake_case
    try:
        return MetadataChangeLogEvent(
            entity_type=data.get("entityType", ""),
            entity_urn=data.get("entityUrn", ""),
            aspect_name=data.get("aspectName", ""),
            change_type=data.get("changeType", ""),
            aspect=data.get("aspect"),
            created=data.get("created"),
        )
    except Exception as exc:
        raise EventProcessingError(f"MCL deserialization failed: {exc}") from exc


# ── EventRouter ──────────────────────────────────────────────────────────────


class EventRouter:
    """Routes MCL events to registered handlers by aspect name."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def register(self, aspect_name: str, handler: Handler) -> None:
        self._handlers.setdefault(aspect_name, []).append(handler)

    @property
    def registered_aspects(self) -> dict[str, list[Handler]]:
        return self._handlers

    async def dispatch(self, event: MetadataChangeLogEvent) -> None:
        """Dispatch event to all handlers registered for its aspect name.

        Handlers run sequentially. If any handler raises a retryable error
        (DataHubUnavailableError, StorageUnavailableError), it propagates to
        the caller so the consumer can skip the offset commit.
        """
        handlers = self._handlers.get(event.aspect_name, [])
        for handler in handlers:
            try:
                await asyncio.wait_for(handler(event), timeout=HANDLER_TIMEOUT_S)
            except TimeoutError:
                logger.error(
                    "handler_timeout",
                    handler=handler.__name__,
                    aspect_name=event.aspect_name,
                    entity_urn=event.entity_urn,
                    timeout_s=HANDLER_TIMEOUT_S,
                )
            except (DataHubUnavailableError, StorageUnavailableError):
                raise
            except Exception:
                logger.exception(
                    "handler_failed",
                    handler=handler.__name__,
                    aspect_name=event.aspect_name,
                    entity_urn=event.entity_urn,
                )


# ── Handler Stubs ────────────────────────────────────────────────────────────


async def sync_vector_index(event: MetadataChangeLogEvent) -> None:
    """Re-generate vector embedding for the changed dataset."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "sync_vector_index",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    # TODO: delegate to SearchService.reindex() or start EmbeddingSyncWorkflow


async def detect_new_clusters(event: MetadataChangeLogEvent) -> None:
    """Detect new ontology clusters when schema changes."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "detect_new_clusters",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    # TODO: delegate to OntologyService or start OntologyRebuildWorkflow


async def update_health_score(event: MetadataChangeLogEvent) -> None:
    """Re-compute health score when ownership or tags change."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "update_health_score",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    # TODO: delegate to MetricsService.aggregate_health()


async def trigger_quality_check(event: MetadataChangeLogEvent) -> None:
    """Run anomaly detection on new dataset profile data."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "trigger_quality_check",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    # TODO: delegate to ValidationService.run() or start ValidationWorkflow


async def check_freshness_sla(event: MetadataChangeLogEvent) -> None:
    """Check freshness SLA when a new operation event arrives."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "check_freshness_sla",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    # TODO: delegate to SLAMonitorWorkflow or ValidationService


# ── Router Factory ───────────────────────────────────────────────────────────


def build_router() -> EventRouter:
    """Wire the routing table per spec (BACKEND.md:930-941)."""
    router = EventRouter()
    # Search (UC5)
    router.register("datasetProperties", sync_vector_index)
    router.register("schemaMetadata", sync_vector_index)
    router.register("globalTags", sync_vector_index)
    # Generation (UC4)
    router.register("schemaMetadata", detect_new_clusters)
    # Metrics (UC6)
    router.register("ownership", update_health_score)
    router.register("globalTags", update_health_score)
    # Validation (UC2, UC3)
    router.register("datasetProfile", trigger_quality_check)
    # Validation SLA (UC3)
    router.register("operation", check_freshness_sla)
    return router
