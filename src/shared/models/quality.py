from datetime import datetime

from pydantic import BaseModel


class QualityScore(BaseModel):
    dataset_urn: str
    overall_score: float  # 0–100
    dimensions: dict[str, float]  # e.g. {"completeness": 85, "freshness": 70}
    computed_at: datetime


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
