from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EventRecord(BaseModel):
    id: str
    entity_type: str  # "dataset", "metric", "concept"
    entity_id: str
    event_type: str  # "ingestion_run", "validation_run", "generation_run", etc.
    status: str  # "success", "failure", "warning"
    detail: dict[str, Any] = {}
    occurred_at: datetime
