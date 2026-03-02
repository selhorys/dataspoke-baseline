from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    trace_id: str


class PaginatedResponse(BaseModel):
    offset: int = 0
    limit: int = 20
    total_count: int = 0
    resp_time: datetime = Field(default_factory=_now_utc)
    # Subclasses add a typed list field named after the resource.
    # This base class carries only the pagination envelope.


class SingleResponse(BaseModel):
    resp_time: datetime = Field(default_factory=_now_utc)


class NotImplementedResponse(BaseModel):
    error_code: str = "NOT_IMPLEMENTED"
    message: str = "This endpoint is not yet implemented."
    detail: Any = None
