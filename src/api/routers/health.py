from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check — always returns 200 when the process is alive."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """Readiness check — returns 200 when the service is ready to accept traffic.

    TODO: add real connectivity checks for DataHub, PostgreSQL, and Redis
    once those clients are wired in.
    """
    return HealthResponse(status="ok")
