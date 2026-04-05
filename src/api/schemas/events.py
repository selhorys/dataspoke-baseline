"""Shared event list response models used by all feature domains."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import EventStatus


class EventResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the event")
    entity_type: str = Field(description="Type of the entity that triggered the event, e.g. 'ingestion_config', 'validation_config'")
    entity_id: str = Field(description="Identifier of the entity that triggered the event")
    event_type: str = Field(description="Type of the event, e.g. 'run_started', 'run_completed', 'config_updated'")
    status: EventStatus = Field(description="Outcome status of the event: 'success', 'ok', 'failure', 'running', or 'warning'")
    detail: dict[str, Any] = Field(default={}, description="Additional event details, e.g. error messages, run statistics, or changed fields")
    occurred_at: datetime = Field(description="UTC timestamp when the event occurred")


class EventListResponse(PaginatedResponse):
    events: list[EventResponse] = Field(default=[], description="Page of event records")


class EventFilterParams(BaseModel):
    entity_type: str | None = Field(default=None, description="Filter events by entity type, e.g. 'ingestion_config'")
    event_type: str | None = Field(default=None, description="Filter events by event type, e.g. 'run_completed'")
    status: EventStatus | None = Field(default=None, description="Filter events by outcome status")
