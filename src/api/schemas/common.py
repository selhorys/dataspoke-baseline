from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


class ErrorResponse(BaseModel):
    error_code: str = Field(description="Machine-readable error code, e.g. 'NOT_FOUND' or 'VALIDATION_ERROR'")
    message: str = Field(description="Human-readable description of the error")
    trace_id: str = Field(description="Unique request trace identifier for log correlation")
    resp_time: datetime = Field(default_factory=_now_utc, description="UTC timestamp when this response was generated")


class PaginatedResponse(BaseModel):
    offset: int = Field(default=0, description="Number of items skipped before this page")
    limit: int = Field(default=20, description="Maximum number of items returned in this page")
    total_count: int = Field(default=0, description="Total number of items matching the query across all pages")
    resp_time: datetime = Field(default_factory=_now_utc, description="UTC timestamp when this response was generated")
    # Subclasses add a typed list field named after the resource.
    # This base class carries only the pagination envelope.


class SingleResponse(BaseModel):
    resp_time: datetime = Field(default_factory=_now_utc, description="UTC timestamp when this response was generated")


class PaginationParams(BaseModel):
    offset: int = Field(default=0, ge=0, description="Number of items to skip (0-based)")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum number of items to return (1–100)")
    sort: str | None = Field(default=None, description="Sort expression in the form '<field>_asc' or '<field>_desc', e.g. 'created_at_desc'")


class TimeRangeParams(BaseModel):
    from_time: datetime | None = Field(default=None, alias="from", description="Inclusive start of the time window (ISO-8601 UTC)")
    to_time: datetime | None = Field(default=None, alias="to", description="Inclusive end of the time window (ISO-8601 UTC)")

    model_config = {"populate_by_name": True}


def parse_sort(sort_param: str | None, allowed: dict[str, Any], default: Any) -> Any:
    """Parse 'field_asc'/'field_desc' into SQLAlchemy order clause.

    Args:
        sort_param: User-supplied sort string, e.g. ``"created_at_desc"``
        allowed: Mapping of field name -> SQLAlchemy column
        default: Fallback order clause when sort_param is None or invalid
    """
    if sort_param is None:
        return default
    for suffix, method in (("_desc", "desc"), ("_asc", "asc")):
        if sort_param.endswith(suffix):
            field_name = sort_param[: -len(suffix)]
            col = allowed.get(field_name)
            if col is not None:
                return getattr(col, method)()
            break
    return default
