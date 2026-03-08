"""Notification domain models."""

from datetime import datetime

from pydantic import BaseModel


class ActionItem(BaseModel):
    dataset_urn: str
    issue_type: str  # "missing_owner", "no_description", "stale", etc.
    priority: str  # "critical", "high", "medium"
    description: str
    estimated_fix_minutes: int
    projected_score_impact: float
    due_date: datetime | None = None


class SLAAlert(BaseModel):
    dataset_urn: str
    sla_name: str
    predicted_breach_at: datetime
    root_cause: str
    recommended_actions: list[str]
