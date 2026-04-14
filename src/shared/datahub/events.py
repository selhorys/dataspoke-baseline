"""MCL event deserialization, aspect-based event router, and handler implementations.

DataHub publishes MetadataChangeLog events to Kafka topics. This module
deserializes those events, routes them by aspect name to registered handlers,
and delegates to downstream services and Airflow workflows.
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
from src.shared.settings import settings

logger = structlog.get_logger(__name__)

# Type alias for async handler functions
Handler = Callable[["MetadataChangeLogEvent"], Coroutine[Any, Any, None]]

# Module-level Airflow client, set by build_router()
_airflow_client: Any = None


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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _urn_to_workflow_id(urn: str) -> str:
    """Create a short, stable identifier from a URN for workflow IDs."""
    from src.workflows._common import urn_to_workflow_id

    return urn_to_workflow_id(urn)


# ── Handler Implementations ─────────────────────────────────────────────────


async def sync_vector_index(event: MetadataChangeLogEvent) -> None:
    """Re-generate vector embedding for the changed dataset via embedding-sync DAG."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "sync_vector_index",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    if _airflow_client is None:
        logger.warning("airflow_unavailable_skipping", handler="sync_vector_index", entity_urn=event.entity_urn)
        return
    try:
        await _airflow_client.trigger_dag_run(
            "embedding-sync",
            conf={
                "callback_base_url": settings.airflow_callback_base_url,
                "mode": "single",
                "dataset_urn": event.entity_urn,
                "workflow_id": f"embedding-sync-{_urn_to_workflow_id(event.entity_urn)}",
            },
        )
    except Exception:
        logger.exception(
            "workflow_start_failed",
            handler="sync_vector_index",
            entity_urn=event.entity_urn,
        )


async def detect_new_clusters(event: MetadataChangeLogEvent) -> None:
    """Detect new ontology clusters when schema changes via ontology-rebuild DAG."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "detect_new_clusters",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    if _airflow_client is None:
        logger.warning("airflow_unavailable_skipping", handler="detect_new_clusters", entity_urn=event.entity_urn)
        return
    try:
        await _airflow_client.trigger_dag_run(
            "ontology-rebuild",
            conf={
                "callback_base_url": settings.airflow_callback_base_url,
                "force": "false",
                "workflow_id": "ontology-rebuild",
            },
        )
    except Exception:
        logger.exception(
            "workflow_start_failed",
            handler="detect_new_clusters",
            entity_urn=event.entity_urn,
        )


async def update_health_score(event: MetadataChangeLogEvent) -> None:
    """Re-compute health scores when ownership or tags change.

    Calls aggregate_health_scores directly (no workflow required) because
    the aggregation needs the current event context and there is no
    single-dataset workflow variant for health scoring.
    """
    if event.entity_type != "dataset":
        return
    logger.info(
        "update_health_score",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    from src.backend.metrics.aggregator import aggregate_health_scores
    from src.shared.db.session import SessionLocal
    from src.workflows._common import make_cache, make_datahub

    datahub = make_datahub()
    cache = make_cache()
    async with SessionLocal() as db:
        await aggregate_health_scores(datahub=datahub, db=db, cache=cache)


# ── Router Factory ───────────────────────────────────────────────────────────


def build_router(*, airflow_client: Any = None) -> EventRouter:
    """Wire the routing table per spec (BACKEND.md:930-941).

    Args:
        airflow_client: Optional Airflow client for triggering DAG runs.
            When None, handlers that require Airflow log the event but
            do not trigger workflows.
    """
    global _airflow_client  # noqa: PLW0603
    _airflow_client = airflow_client

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
    router.register("datasetProfile", update_health_score)
    return router
