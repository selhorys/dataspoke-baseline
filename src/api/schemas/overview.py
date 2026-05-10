"""Overview config and dashboard response models (DG)."""

from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import SingleResponse


class PatchOverviewRequest(BaseModel):
    layout: str | None = Field(
        default=None,
        description="Graph layout algorithm: 'force', 'hierarchical', or 'radial'",
    )
    color_by: str | None = Field(
        default=None,
        description="Node coloring strategy: 'quality_score', 'freshness', or 'platform'",
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Active filter configuration for the overview graph",
    )


class GraphNodeResponse(BaseModel):
    id: str = Field(description="Unique node identifier (ontology node slug)")
    type: str = Field(description="Node type, e.g. 'ontogen_node'")
    label: str = Field(description="Human-readable label displayed on the graph node")
    metadata: dict[str, Any] = Field(
        default={},
        description="Additional node metadata (description, confidence_score)",
    )


class GraphEdgeResponse(BaseModel):
    source: str = Field(description="Subject node ID")
    target: str = Field(description="Object node ID")
    type: str = Field(description="Edge type, e.g. 'ontogen_triple'")
    metadata: dict[str, Any] = Field(
        default={},
        description="Additional edge metadata (edge_id, edge_label, confidence_score)",
    )


class OntologyGraphResponse(BaseModel):
    nodes: list[GraphNodeResponse] = Field(
        default=[],
        description="Approved ontogen nodes (subjects / objects)",
    )
    edges: list[GraphEdgeResponse] = Field(
        default=[],
        description="Approved ontogen triples rendered as directed edges",
    )


class MedallionSummaryResponse(BaseModel):
    bronze: int = Field(default=0, description="Datasets with 0 upstreams")
    silver: int = Field(default=0, description="Datasets with 1–2 upstreams")
    gold: int = Field(default=0, description="Datasets with 3+ upstreams")


class OverviewSnapshotResponse(SingleResponse):
    metric_values: dict[str, float] = Field(
        default={},
        description="Latest measured value per enabled metric_id",
    )
    per_dataset_breakdown: dict[str, list[dict[str, Any]]] = Field(
        default={},
        description="Latest breakdown datasets list per enabled metric_id",
    )
    blind_spots: list[str] = Field(
        default=[],
        description="Dataset URNs present in DataHub with no approved dataset_node_map row",
    )
    ontology_graph: OntologyGraphResponse = Field(
        default_factory=OntologyGraphResponse,
        description="Approved ontogen nodes and triples (no dataset nodes, no lineage edges)",
    )
    medallion: MedallionSummaryResponse = Field(
        default_factory=MedallionSummaryResponse,
        description="Dataset counts per medallion layer (Bronze / Silver / Gold)",
    )
    ownership_topology: dict[str, list[str]] = Field(
        default={},
        description="Owner URN → list of dataset URNs derived from DataHub OwnershipClass",
    )


class OverviewResponse(SingleResponse):
    layout: str = Field(
        default="force",
        description="Current graph layout: 'force', 'hierarchical', or 'radial'",
    )
    color_by: str = Field(
        default="quality_score",
        description="Current node coloring strategy: 'quality_score', 'freshness', or 'platform'",
    )
    filters: dict[str, Any] = Field(
        default={},
        description="Active filter configuration for the overview graph",
    )
