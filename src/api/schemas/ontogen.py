"""Ontology Generation request/response schemas — UC3."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.api.schemas._dataset_filter import validate_dataset_filter
from src.api.schemas.common import PaginatedResponse, SingleResponse

# ── Conf ──────────────────────────────────────────────────────────────────────


class OntogenConfResponse(SingleResponse):
    is_enabled: bool = Field(description="Master switch for the inference DAG")
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = Field(
        default=None, description="Periodic re-inference cadence: 'hourly', 'daily', or 'weekly'"
    )
    dataset_filter: dict[str, Any] = Field(
        default={},
        description=(
            "Optional scope filter. Keys: origin (DataHub FabricType, AND-ed with the OR-group), "
            "tags (list[str], OR), glossary_terms (list[str], OR), "
            "dataset_urns (list[str], OR). Each list dimension capped at 1,000 entries."
        ),
    )
    default_run_prompt: str | None = Field(
        default=None,
        description="Default one-shot prompt for periodic runs and bodyless manual calls",
    )
    updated_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent conf update"
    )


class OntogenConfPutRequest(BaseModel):
    is_enabled: bool = Field(default=False)
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = Field(
        default=None,
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'.",
    )
    dataset_filter: dict[str, Any] = Field(default={})
    default_run_prompt: str | None = Field(
        default=None,
        max_length=16_000,
        description="Default one-shot prompt for periodic runs (max 16 KB).",
    )

    @model_validator(mode="after")
    def validate_dataset_filter_fields(self) -> "OntogenConfPutRequest":
        validate_dataset_filter(self.dataset_filter)
        return self


class OntogenConfPatchRequest(BaseModel):
    is_enabled: bool | None = Field(default=None)
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = Field(
        default=None,
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'.",
    )
    dataset_filter: dict[str, Any] | None = Field(default=None)
    default_run_prompt: str | None = Field(
        default=None,
        max_length=16_000,
        description="Default one-shot prompt for periodic runs (max 16 KB).",
    )

    @model_validator(mode="after")
    def validate_dataset_filter_fields(self) -> "OntogenConfPatchRequest":
        if self.dataset_filter is not None:
            validate_dataset_filter(self.dataset_filter)
        return self


# ── Seeds ─────────────────────────────────────────────────────────────────────


class SeedListItem(BaseModel):
    seed_id: str = Field(description="Unique identifier of the seed")
    is_enabled: bool = Field(
        description="Whether the seed participates in the inference pipeline"
    )
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")
    preview: str = Field(
        description="Short Markdown snippet (first ~200 chars, newlines normalised)",
    )


class SeedListResponse(PaginatedResponse):
    seeds: list[SeedListItem] = Field(default=[], description="Page of seed preview records")


class SeedEnabledRequest(BaseModel):
    is_enabled: bool = Field(
        description="Enable (true) or disable (false) the seed for the inference pipeline"
    )


# ── Run ───────────────────────────────────────────────────────────────────────


class OntogenRunResponse(SingleResponse):
    status: str = Field(description="Outcome: 'success' or 'failure'")
    dry_run: bool = Field(description="Whether this was a dry run (no persistence)")
    unresolved_urns: list[str] = Field(
        default=[], description="URNs in dataset_filter that did not resolve in DataHub"
    )
    counts: dict[str, int] = Field(
        default={}, description="Proposed nodes/edges/triples counts"
    )


# ── Node ─────────────────────────────────────────────────────────────────────


class NodeResponse(SingleResponse):
    id: str = Field(description="Slug ID of the ontology node")
    name: str = Field(description="Human-readable node name")
    description: str = Field(default="", description="Node description")
    confidence_score: float = Field(description="LLM confidence score (0–1)")
    status: str = Field(
        description=(
            "Lifecycle status: llm_pending (LLM-created, awaiting review), "
            "llm_approved (LLM-reviewer accepted + high confidence), "
            "approved (human-approved), or rejected (human-rejected)"
        )
    )
    created_at: datetime = Field(description="UTC timestamp when the node was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class NodeListResponse(PaginatedResponse):
    nodes: list[NodeResponse] = Field(default=[], description="Page of node records")


class NodeAttrResponse(SingleResponse):
    node_id: str = Field(description="Slug ID of the ontology node")
    confidence_score: float = Field(description="LLM confidence score")
    evidence: dict[str, Any] = Field(
        default={}, description="Source evidence collected for this node"
    )


# ── Edge ─────────────────────────────────────────────────────────────────────


class EdgeResponse(SingleResponse):
    id: str = Field(description="Slug ID of the ontology edge (predicate)")
    label: str = Field(description="Human-readable predicate label")
    semantics: str | None = Field(default=None, description="Semantic description of the predicate")
    confidence_score: float = Field(description="LLM confidence score (0–1)")
    status: str = Field(
        description=(
            "Lifecycle status: llm_pending (LLM-created, awaiting review), "
            "llm_approved (LLM-reviewer accepted + high confidence), "
            "approved (human-approved), or rejected (human-rejected)"
        )
    )
    created_at: datetime = Field(description="UTC timestamp when the edge was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class EdgeListResponse(PaginatedResponse):
    edges: list[EdgeResponse] = Field(default=[], description="Page of edge records")


class EdgeAttrResponse(SingleResponse):
    edge_id: str = Field(description="Slug ID of the ontology edge")
    confidence_score: float = Field(description="LLM confidence score")
    evidence: dict[str, Any] = Field(
        default={}, description="Source evidence collected for this edge"
    )


# ── Triple ────────────────────────────────────────────────────────────────────


class TripleResponse(SingleResponse):
    id: str = Field(
        description="Composite ID: '{subject_node_id}__{edge_id}__{object_node_id}'"
    )
    subject_node_id: str = Field(description="ID of the subject node")
    edge_id: str = Field(description="ID of the predicate edge")
    object_node_id: str = Field(description="ID of the object node")
    confidence_score: float = Field(description="LLM confidence score (0–1)")
    status: str = Field(
        description=(
            "Lifecycle status: llm_pending (LLM-created, awaiting review), "
            "llm_approved (LLM-reviewer accepted + high confidence), "
            "approved (human-approved), or rejected (human-rejected)"
        )
    )
    created_at: datetime = Field(description="UTC timestamp when the triple was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class TripleListResponse(PaginatedResponse):
    triples: list[TripleResponse] = Field(default=[], description="Page of triple records")


class TripleAttrResponse(SingleResponse):
    triple_id: str = Field(description="Composite triple ID")
    subject_node_id: str = Field(description="ID of the subject node")
    edge_id: str = Field(description="ID of the predicate edge")
    object_node_id: str = Field(description="ID of the object node")
    confidence_score: float = Field(description="LLM confidence score")
    evidence: dict[str, Any] = Field(
        default={}, description="Source evidence collected for this triple"
    )


# ── Review ────────────────────────────────────────────────────────────────────


class ReviewRequest(BaseModel):
    verdict: Literal["approve", "reject"] = Field(
        description="Review decision: 'approve' to accept the result, 'reject' to dismiss it"
    )
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional human-readable explanation for the verdict",
    )
