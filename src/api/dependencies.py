"""Dependency injection provider functions for infrastructure clients.

Long-lived clients (DataHub, Redis, pgvector, LLM, Airflow) are constructed
once during application startup (via the lifespan in src/api/main.py) and
stored on app.state. Per-request providers below simply retrieve the shared
instance, which keeps constructors out of the hot path.
"""

from collections.abc import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dataset.service import DatasetService
from src.backend.ingestion.service import IngestionService
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import SessionLocal
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager
from src.workflows.airflow.client import AirflowClient

# ── Infrastructure client providers ──────────────────────────────


def get_datahub(request: Request) -> DataHubClient:
    return request.app.state.datahub


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


def get_redis(request: Request) -> RedisClient:
    return request.app.state.redis


def get_vector(request: Request) -> PgVectorManager:
    return request.app.state.vector


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_notification():
    from src.shared.notifications.service import NotificationService

    return NotificationService()


def get_airflow_client(request: Request) -> AirflowClient:
    """Return the shared Airflow client from app state."""
    return request.app.state.airflow


# ── Service providers ──────────────────────────────────────────────


async def get_dataset_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
) -> DatasetService:
    return DatasetService(datahub=datahub, db=db, cache=cache)


async def get_ingestion_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
) -> IngestionService:
    return IngestionService(datahub=datahub, db=db, cache=cache)


async def get_validation_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
) -> "ValidationService":
    from src.backend.validation.service import ValidationService

    return ValidationService(datahub=datahub, db=db)


async def get_metagen_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
    llm: LLMClient = Depends(get_llm),
    vector: PgVectorManager = Depends(get_vector),
) -> "MetagenService":
    from src.backend.metagen.service import MetagenService

    return MetagenService(datahub=datahub, db=db, cache=cache, llm=llm, vector=vector)


async def get_ontogen_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
    llm: LLMClient = Depends(get_llm),
    vector: PgVectorManager = Depends(get_vector),
) -> "OntogenService":
    from src.backend.ontogen.service import OntogenService

    return OntogenService(
        datahub=datahub,
        db=db,
        cache=cache,
        llm=llm,
        vector=vector,
    )


async def get_metrics_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
) -> "MetricsService":
    from src.backend.metrics.service import MetricsService

    return MetricsService(datahub=datahub, db=db, cache=cache)


