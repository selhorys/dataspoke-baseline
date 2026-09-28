from datetime import datetime
from typing import Any

from pydantic import BaseModel


class QualityScore(BaseModel):
    overall_score: float  # 0–100
    dimensions: dict[str, float]  # e.g. {"completeness": 85, "freshness": 70}
    dimension_details: dict[str, dict[str, Any]] | None = None
    dataset_urn: str | None = None
    computed_at: datetime | None = None
