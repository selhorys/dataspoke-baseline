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


class GraphNodeResponse(BaseModel):
    id: str
    type: str
    label: str
    metadata: dict[str, Any] = {}


class GraphEdgeResponse(BaseModel):
    source: str
    target: str
    type: str
    metadata: dict[str, Any] = {}


class MedallionSummaryResponse(BaseModel):
    bronze: int = 0
    silver: int = 0
    gold: int = 0


class OverviewSnapshotResponse(SingleResponse):
    nodes: list[GraphNodeResponse] = []
    edges: list[GraphEdgeResponse] = []
    medallion: MedallionSummaryResponse = MedallionSummaryResponse()
    blind_spots: list[str] = []
    stats: DatasetSummaryStats = DatasetSummaryStats()


class OverviewResponse(SingleResponse):
    layout: str = "force"
    color_by: str = "quality_score"
    filters: dict[str, Any] = {}
    stats: DatasetSummaryStats = DatasetSummaryStats()
