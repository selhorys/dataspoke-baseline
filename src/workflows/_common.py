"""Shared constants, retry policy, and service factories for Temporal workflows."""

import hashlib
from datetime import timedelta

from temporalio.common import RetryPolicy

from src.api.config import settings
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import SessionLocal
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager

TASK_QUEUE = "dataspoke-main"


def urn_to_workflow_id(urn: str) -> str:
    """Create a short, stable identifier from a URN for Temporal workflow IDs."""
    return hashlib.md5(urn.encode()).hexdigest()[:12]  # noqa: S324


DEFAULT_ACTIVITY_TIMEOUT = timedelta(minutes=5)
DEFAULT_WORKFLOW_TIMEOUT = timedelta(hours=1)
HEARTBEAT_TIMEOUT = timedelta(seconds=30)


def default_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=10),
        backoff_coefficient=2.0,
        maximum_attempts=3,
    )


def make_datahub() -> DataHubClient:
    return DataHubClient(settings.datahub_gms_url, settings.datahub_token)


def make_cache() -> RedisClient:
    return RedisClient(settings.redis_host, settings.redis_port, settings.redis_password)


def make_llm() -> LLMClient:
    return LLMClient(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )


def make_qdrant() -> QdrantManager:
    return QdrantManager(
        host=settings.qdrant_host,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        grpc_port=settings.qdrant_grpc_port,
    )


def make_db_session():
    """Create a fresh AsyncSession for Temporal activity use.

    Returns an AsyncSession usable as ``async with make_db_session() as db:``.
    Patchable in tests to inject a test-scoped session.
    """
    return SessionLocal()


def make_notification():
    from src.shared.notifications.service import NotificationService

    return NotificationService()


async def await_workflow_result(handle) -> dict:
    """Await a Temporal workflow handle and unwrap activity errors.

    Activities convert DataSpokeError → ApplicationError(type=error_code).
    When the workflow fails, handle.result() raises WorkflowFailureError.
    This helper unwraps the chain and re-raises the matching DataSpokeError
    so that FastAPI exception handlers return the correct HTTP status.
    """
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ActivityError, ApplicationError

    from src.shared.exceptions import ConflictError, DataSpokeError, EntityNotFoundError

    try:
        return await handle.result()
    except WorkflowFailureError as exc:
        cause = exc.cause
        if isinstance(cause, ActivityError):
            cause = cause.cause
        if isinstance(cause, ApplicationError) and cause.type:
            error_code = cause.type
            message = str(cause)
            if error_code.endswith("_NOT_FOUND"):
                entity_type = error_code.removesuffix("_NOT_FOUND").lower()
                raise EntityNotFoundError(entity_type, message) from exc
            if error_code.endswith("_RUNNING"):
                raise ConflictError(error_code, message) from exc
            raise DataSpokeError(message) from exc
        raise
