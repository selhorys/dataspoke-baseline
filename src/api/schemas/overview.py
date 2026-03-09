"""Overview config and dashboard response models (DG)."""

from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import SingleResponse


class PatchOverviewRequest(BaseModel):
    layout: str | None = None
    color_by: str | None = None
    filters: dict[str, Any] | None = None


class DatasetSummaryStats(BaseModel):
    total_datasets: int = 0
    monitored_datasets: int = 0
    avg_quality_score: float = 0.0
    issues_count: int = 0


class OverviewResponse(SingleResponse):
    layout: str = "grid"
    color_by: str = "quality"
    filters: dict[str, Any] = {}
    stats: DatasetSummaryStats = DatasetSummaryStats()
