"""Overview config and dashboard response models (DG)."""

from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import SingleResponse


class PatchOverviewRequest(BaseModel):
    layout: str | None = Field(default=None, description="Graph layout algorithm, e.g. 'force', 'hierarchical', or 'radial'")
    color_by: str | None = Field(default=None, description="Node coloring strategy, e.g. 'quality_score' or 'medallion'")
    filters: dict[str, Any] | None = Field(default=None, description="Filter configuration for the overview graph. Example: {\"platform\": \"postgres\", \"min_quality_score\": 0.7}")


class DatasetSummaryStats(BaseModel):
    total_datasets: int = Field(default=0, description="Total number of datasets tracked in the system")
    monitored_datasets: int = Field(default=0, description="Number of datasets with at least one active validation or metric config")
    avg_quality_score: float = Field(default=0.0, description="Average quality score across all monitored datasets (0.0–1.0)")
    issues_count: int = Field(default=0, description="Total number of open metric issues across all datasets")


class GraphNodeResponse(BaseModel):
    id: str = Field(description="Unique node identifier (usually a dataset URN or concept ID)")
    type: str = Field(description="Node type, e.g. 'dataset' or 'concept'")
    label: str = Field(description="Human-readable label displayed on the graph node")
    metadata: dict[str, Any] = Field(default={}, description="Additional node metadata, e.g. {\"platform\": \"postgres\", \"quality_score\": 0.85, \"medallion\": \"gold\"}")


class GraphEdgeResponse(BaseModel):
    source: str = Field(description="Identifier of the source node")
    target: str = Field(description="Identifier of the target node")
    type: str = Field(description="Edge type, e.g. 'lineage', 'related_to', or 'part_of'")
    metadata: dict[str, Any] = Field(default={}, description="Additional edge metadata, e.g. {\"confidence\": 0.9}")


class MedallionSummaryResponse(BaseModel):
    bronze: int = Field(default=0, description="Number of datasets at the bronze quality tier")
    silver: int = Field(default=0, description="Number of datasets at the silver quality tier")
    gold: int = Field(default=0, description="Number of datasets at the gold quality tier")


class OverviewSnapshotResponse(SingleResponse):
    nodes: list[GraphNodeResponse] = Field(default=[], description="Graph nodes representing datasets and concepts")
    edges: list[GraphEdgeResponse] = Field(default=[], description="Graph edges representing relationships and lineage")
    medallion: MedallionSummaryResponse = Field(default_factory=MedallionSummaryResponse, description="Breakdown of datasets by medallion quality tier")
    blind_spots: list[str] = Field(default=[], description="Dataset URNs identified as blind spots (no owner, no description, or no quality monitoring)")
    stats: DatasetSummaryStats = Field(default_factory=DatasetSummaryStats, description="Aggregate summary statistics for the overview dashboard")


class OverviewResponse(SingleResponse):
    layout: str = Field(default="force", description="Current graph layout setting, e.g. 'force', 'hierarchical', or 'radial'")
    color_by: str = Field(default="quality_score", description="Current node coloring strategy, e.g. 'quality_score' or 'medallion'")
    filters: dict[str, Any] = Field(default={}, description="Active filter configuration for the overview graph")
    stats: DatasetSummaryStats = Field(default_factory=DatasetSummaryStats, description="Aggregate summary statistics for the overview dashboard")
