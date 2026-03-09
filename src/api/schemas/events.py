"""Shared event list response models used by all feature domains."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class EventResponse(SingleResponse):
    id: str
    entity_type: str
    entity_id: str
    event_type: str
    status: str
    detail: dict[str, Any] = {}
    occurred_at: datetime


class EventListResponse(PaginatedResponse):
    events: list[EventResponse] = []


class EventFilterParams(BaseModel):
    entity_type: str | None = None
    event_type: str | None = None
    status: str | None = None
