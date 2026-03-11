"""Shared constants, retry policy, and service factories for Temporal workflows."""

from datetime import timedelta

from temporalio.common import RetryPolicy

from src.api.config import settings
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager

TASK_QUEUE = "dataspoke-main"
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
