from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EventRecord(BaseModel):
    id: str
    entity_type: str  # "dataset", "metric", "concept"
    entity_id: str
    event_type: str  # e.g. "INGESTION.COMPLETE", "VALIDATION.COMPLETE", "GENERATION.COMPLETE"
    status: str  # "success", "failure", "warning"
    detail: dict[str, Any] = {}
    occurred_at: datetime
