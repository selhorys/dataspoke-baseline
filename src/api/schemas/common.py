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


class PaginationParams(BaseModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    sort: str | None = None


class TimeRangeParams(BaseModel):
    from_time: datetime | None = Field(default=None, alias="from")
    to_time: datetime | None = Field(default=None, alias="to")

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
