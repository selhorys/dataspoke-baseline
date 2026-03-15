"""MCL event deserialization, aspect-based event router, and handler implementations.

DataHub publishes MetadataChangeLog events to Kafka topics. This module
deserializes those events, routes them by aspect name to registered handlers,
and delegates to downstream services and Temporal workflows.
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

# Module-level Temporal client, set by build_router()
_temporal_client: Any = None

TASK_QUEUE = "dataspoke-main"


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
    """Create a short, stable identifier from a URN for Temporal workflow IDs."""
    from src.workflows._common import urn_to_workflow_id

    return urn_to_workflow_id(urn)


# ── Handler Implementations ─────────────────────────────────────────────────


async def sync_vector_index(event: MetadataChangeLogEvent) -> None:
    """Re-generate vector embedding for the changed dataset via EmbeddingSyncWorkflow."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "sync_vector_index",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    if _temporal_client is None:
        return
    from src.workflows.embedding_sync import EmbeddingSyncParams, EmbeddingSyncWorkflow

    try:
        await _temporal_client.start_workflow(
            EmbeddingSyncWorkflow.run,
            EmbeddingSyncParams(mode="single", dataset_urn=event.entity_urn),
            id=f"embedding-sync-{_urn_to_workflow_id(event.entity_urn)}",
            task_queue=TASK_QUEUE,
        )
    except Exception:
        logger.exception(
            "workflow_start_failed",
            handler="sync_vector_index",
            entity_urn=event.entity_urn,
        )


async def detect_new_clusters(event: MetadataChangeLogEvent) -> None:
    """Detect new ontology clusters when schema changes via OntologyRebuildWorkflow."""
    if event.entity_type != "dataset":
        return
    logger.info(
        "detect_new_clusters",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    if _temporal_client is None:
        return
    from src.workflows.ontology import OntologyRebuildParams, OntologyRebuildWorkflow

    try:
        await _temporal_client.start_workflow(
            OntologyRebuildWorkflow.run,
            OntologyRebuildParams(),
            id="ontology-rebuild",
            task_queue=TASK_QUEUE,
        )
    except Exception:
        logger.exception(
            "workflow_start_failed",
            handler="detect_new_clusters",
            entity_urn=event.entity_urn,
        )


async def update_health_score(event: MetadataChangeLogEvent) -> None:
    """Re-compute health scores when ownership or tags change.

    Calls aggregate_health_scores directly (no Temporal required) because
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


async def trigger_quality_check(event: MetadataChangeLogEvent) -> None:
    """Run validation pipeline on new dataset profile data via ValidationWorkflow.

    Only triggers if the dataset has an existing ValidationConfig.
    """
    if event.entity_type != "dataset":
        return
    logger.info(
        "trigger_quality_check",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    if _temporal_client is None:
        return
    from sqlalchemy import select

    from src.shared.db.models import ValidationConfig
    from src.shared.db.session import SessionLocal
    from src.workflows.validation import ValidationParams, ValidationWorkflow

    async with SessionLocal() as db:
        result = await db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == event.entity_urn)
        )
        config = result.scalar_one_or_none()

    if config is None:
        logger.info("no_validation_config", entity_urn=event.entity_urn)
        return

    try:
        await _temporal_client.start_workflow(
            ValidationWorkflow.run,
            ValidationParams(dataset_urn=event.entity_urn),
            id=f"validation-{_urn_to_workflow_id(event.entity_urn)}",
            task_queue=TASK_QUEUE,
        )
    except Exception:
        logger.exception(
            "workflow_start_failed",
            handler="trigger_quality_check",
            entity_urn=event.entity_urn,
        )


async def check_freshness_sla(event: MetadataChangeLogEvent) -> None:
    """Check freshness SLA when a new operation event arrives via SLAMonitorWorkflow.

    Only triggers if the dataset has a ValidationConfig with an sla_target.
    """
    if event.entity_type != "dataset":
        return
    logger.info(
        "check_freshness_sla",
        entity_urn=event.entity_urn,
        aspect_name=event.aspect_name,
    )
    if _temporal_client is None:
        return
    from sqlalchemy import select

    from src.shared.db.models import ValidationConfig
    from src.shared.db.session import SessionLocal
    from src.workflows.sla_monitor import SLAMonitorParams, SLAMonitorWorkflow

    async with SessionLocal() as db:
        result = await db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == event.entity_urn)
        )
        config = result.scalar_one_or_none()

    if config is None or config.sla_target is None:
        logger.info("no_sla_target", entity_urn=event.entity_urn)
        return

    try:
        await _temporal_client.start_workflow(
            SLAMonitorWorkflow.run,
            SLAMonitorParams(
                dataset_urn=event.entity_urn,
                sla_target=config.sla_target,
            ),
            id=f"sla-monitor-{_urn_to_workflow_id(event.entity_urn)}",
            task_queue=TASK_QUEUE,
        )
    except Exception:
        logger.exception(
            "workflow_start_failed",
            handler="check_freshness_sla",
            entity_urn=event.entity_urn,
        )


# ── Router Factory ───────────────────────────────────────────────────────────


def build_router(*, temporal_client: Any = None) -> EventRouter:
    """Wire the routing table per spec (BACKEND.md:930-941).

    Args:
        temporal_client: Optional Temporal client for starting workflows.
            When None, handlers that require Temporal log the event but
            do not start workflows.
    """
    global _temporal_client  # noqa: PLW0603
    _temporal_client = temporal_client

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
