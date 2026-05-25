"""Dependency injection provider functions for infrastructure clients.

Long-lived clients (Redis, pgvector, Airflow) are constructed once during
application startup (via the lifespan in src/api/main.py) and stored on
app.state.  Per-request providers below retrieve the shared instance.

DataHub clients are NOT long-lived on app.state — they are constructed
per-request from the DB-backed peripheral_config so that connection changes
via /admin/peripherals/datahub are honoured immediately.

LLM clients are NOT long-lived on app.state — they are constructed per-request
from the DB-backed RuntimeConfig so that provider/model changes via
/admin/conf are honoured immediately and stub toggling via make_llm_client()
applies on all paths (including manual metagen/ontogen API runs).
"""

from collections.abc import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dataset.service import DatasetService
from src.backend.ingestion.service import IngestionService
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import SessionLocal
from src.shared.vector.client import PgVectorManager
from src.workflows.airflow.client import AirflowClient

# NOTE: LLMClient is NOT imported here. LLM instances are built per-request
# inside get_metagen_service / get_ontogen_service via make_llm_client() so that
# RuntimeConfig changes and stub toggling are always honoured.

# ── Infrastructure client providers ──────────────────────────────


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def get_datahub(db: AsyncSession = Depends(get_db)) -> DataHubClient:
    """Construct a per-request DataHubClient from the peripheral_config DB row.

    Raises StorageUnavailableError (→ 503) when the DataHub peripheral is not
    configured or the token is absent.
    """
    from src.backend.admin.datahub_secret import get_datahub_token
    from src.backend.admin.peripheral_service import get_peripheral_config
    from src.shared.exceptions import StorageUnavailableError

    dto = await get_peripheral_config(db, "datahub")
    token = get_datahub_token()
    if dto is None or not token:
        raise StorageUnavailableError("datahub peripheral not configured")
    return DataHubClient(dto.gms_url, token)


async def get_redis(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedisClient:
    from src.backend.admin.config_service import get_runtime_config

    rc = await get_runtime_config(db)
    if rc.stub_redis_client:
        from src.workflows._stubs import StubRedisClient

        return StubRedisClient()  # type: ignore[return-value]
    return request.app.state.redis


async def get_vector(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PgVectorManager:
    from src.backend.admin.config_service import get_runtime_config

    rc = await get_runtime_config(db)
    if rc.stub_pgvector_manager:
        from src.workflows._stubs import StubPgVectorManager

        return StubPgVectorManager()  # type: ignore[return-value]
    return request.app.state.vector


async def get_notification(
    db: AsyncSession = Depends(get_db),
) -> "NotificationService":
    from src.backend.admin.config_service import get_runtime_config
    from src.shared.notifications.service import NotificationService

    rc = await get_runtime_config(db)
    if rc.stub_notification_service:
        from src.workflows._stubs import StubNotificationService

        return StubNotificationService()  # type: ignore[return-value]
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
    vector: PgVectorManager = Depends(get_vector),
) -> "MetagenService":
    from src.backend.admin.config_service import get_runtime_config
    from src.backend.metagen.service import MetagenService
    from src.workflows._common import make_llm_client, read_langfuse_config

    rc = await get_runtime_config(db)
    lf_host, lf_pk = await read_langfuse_config(db)
    llm = make_llm_client(stub=rc.stub_llm_client, provider=rc.llm_provider, model=rc.llm_model, langfuse_host=lf_host, langfuse_public_key=lf_pk)
    return MetagenService(datahub=datahub, db=db, cache=cache, llm=llm, vector=vector)


async def get_ontogen_service(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    cache: RedisClient = Depends(get_redis),
    vector: PgVectorManager = Depends(get_vector),
) -> "OntogenService":
    from src.backend.admin.config_service import get_runtime_config
    from src.backend.ontogen.service import OntogenService
    from src.workflows._common import make_llm_client, read_langfuse_config

    rc = await get_runtime_config(db)
    lf_host, lf_pk = await read_langfuse_config(db)
    llm = make_llm_client(stub=rc.stub_llm_client, provider=rc.llm_provider, model=rc.llm_model, langfuse_host=lf_host, langfuse_public_key=lf_pk)
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


