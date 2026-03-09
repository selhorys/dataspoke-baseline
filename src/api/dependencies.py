"""Dependency injection provider functions for infrastructure clients.

Each provider creates a fresh client per request. The DataHub SDK manages
its own connection pooling internally; Redis and Qdrant clients are
lightweight wrappers around pooled connections.

Service providers (get_ingestion_service, get_validation_service, etc.)
will be added as backend services are implemented in src/backend/.
"""

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.config import settings
from src.backend.dataset.service import DatasetService
from src.backend.ingestion.service import IngestionService
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import SessionLocal
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager

# ── Infrastructure client providers ──────────────────────────────


def get_datahub() -> DataHubClient:
    return DataHubClient(settings.datahub_gms_url, settings.datahub_token)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def get_redis() -> RedisClient:
    return RedisClient(settings.redis_host, settings.redis_port, settings.redis_password)


def get_qdrant() -> QdrantManager:
    return QdrantManager(
        host=settings.qdrant_host,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        grpc_port=settings.qdrant_grpc_port,
    )


def get_llm() -> LLMClient:
    return LLMClient(settings.llm_provider, settings.llm_api_key, settings.llm_model)


# ── Service providers (added as backend services are implemented) ──


async def get_dataset_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
) -> DatasetService:
    return DatasetService(datahub=datahub, db=db, cache=cache)


async def get_ingestion_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm),
) -> IngestionService:
    return IngestionService(datahub=datahub, db=db, llm=llm)


# async def get_validation_service(...) -> ValidationService: ...
# async def get_generation_service(...) -> GenerationService: ...
# async def get_search_service(...) -> SearchService: ...
# async def get_ontology_service(...) -> OntologyService: ...
# async def get_metrics_service(...) -> MetricsService: ...
