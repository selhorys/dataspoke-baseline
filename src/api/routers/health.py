from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_datahub, get_db, get_redis
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, bool] = {}


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check — always returns 200 when the process is alive."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready(
    datahub: DataHubClient = Depends(get_datahub),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
) -> ReadyResponse:
    """Readiness check — returns 200 with per-dependency status."""
    checks: dict[str, bool] = {}

    checks["datahub"] = await datahub.check_connectivity()

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
