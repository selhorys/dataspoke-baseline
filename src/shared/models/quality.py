from datetime import datetime
from typing import Any

from pydantic import BaseModel


class QualityScore(BaseModel):
    overall_score: float  # 0–100
    dimensions: dict[str, float]  # e.g. {"completeness": 85, "freshness": 70}
    dimension_details: dict[str, dict[str, Any]] | None = None
    dataset_urn: str | None = None
    computed_at: datetime | None = None


class QualityIssue(BaseModel):
    issue_type: str  # "freshness", "completeness", "schema_drift", etc.
    severity: str  # "critical", "warning", "info"
    detail: str
    field_path: str | None = None


class AnomalyResult(BaseModel):
    metric_name: str
    is_anomaly: bool
    expected_value: float
    actual_value: float
    confidence: float
    detected_at: datetime
