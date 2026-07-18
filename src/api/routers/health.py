from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_datahub, get_db, get_redis
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str = Field(description="Liveness status, always 'ok' when reachable")


class ReadyResponse(BaseModel):
    status: str = Field(description="Overall readiness: 'ok' or 'degraded'")
    checks: dict[str, bool] = Field(
        default={}, description="Per-dependency reachability: datahub, postgres, redis"
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check — always returns 200 when the process is alive."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
) -> ReadyResponse:
    """Readiness check — returns 200 with per-dependency status.

    Reports state; never returns 503.  When DataHub is unconfigured or
    unreachable, ``checks.datahub`` is False and ``status`` is 'degraded'.

    ``get_datahub`` is resolved manually (honouring ``dependency_overrides`` for
    test isolation) instead of via ``Depends()`` so that ``StorageUnavailableError``
    is caught here rather than converted to 503 by the global exception handler.
    """
    from src.shared.exceptions import StorageUnavailableError

    checks: dict[str, bool] = {}

    # Resolve get_datahub manually so dependency_overrides (set in tests) are honoured
    # and StorageUnavailableError from an unconfigured peripheral is caught locally.
    datahub_resolver = request.app.dependency_overrides.get(get_datahub, None)
    if datahub_resolver is not None:
        # Test override: the override is a no-arg callable returning the mock directly.
        try:
            raw = datahub_resolver()
            datahub: DataHubClient | None = raw if isinstance(raw, DataHubClient) else raw
        except Exception:
            datahub = None
    else:
        # Production path: read from peripheral_config + secret.
        try:
            datahub = await get_datahub(db)
        except (StorageUnavailableError, Exception):
            datahub = None

    if datahub is not None:
        try:
            checks["datahub"] = await datahub.check_connectivity()
        except Exception:
            checks["datahub"] = False
    else:
        checks["datahub"] = False

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:
        checks["postgres"] = False

    try:
        await redis.get("__ready_probe__")
        checks["redis"] = True
    except Exception:
        checks["redis"] = False

    all_ok = all(checks.values())
    return ReadyResponse(
        status="ok" if all_ok else "degraded",
        checks=checks,
    )
