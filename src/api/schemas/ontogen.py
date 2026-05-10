"""Ontology Generation request/response schemas — UC3."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse

_VALID_TIERS = frozenset({"hourly", "daily", "weekly"})

_DATASET_FILTER_LIST_CAP = 1000


def _check_dataset_filter_bounds(dataset_filter: dict[str, Any]) -> None:
    """Raise ValueError if any list field in *dataset_filter* exceeds the cap."""
    for key in ("dataset_urns", "tags", "glossary_terms"):
        val = dataset_filter.get(key)
        if val is not None and len(val) > _DATASET_FILTER_LIST_CAP:
            raise ValueError(
                f"dataset_filter.{key} may not exceed "
                f"{_DATASET_FILTER_LIST_CAP} entries"
            )


# ── Conf ──────────────────────────────────────────────────────────────────────


class OntogenConfResponse(SingleResponse):
    is_enabled: bool = Field(description="Master switch for the inference DAG")
    schedule_tier: str | None = Field(
        default=None, description="Periodic re-inference cadence: 'hourly', 'daily', or 'weekly'"
    )
    dataset_filter: dict[str, Any] = Field(
        default={},
        description=(
            "Scope filter: tags (DataHub tag URNs), glossary_terms, and/or dataset_urns. "
            "OR-ed across dimensions; {} means all datasets."
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
    schedule_tier: str | None = Field(
        default=None,
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'.",
    )
    dataset_filter: dict[str, Any] = Field(default={})
    default_run_prompt: str | None = Field(
        default=None,
        max_length=16_000,
        description="Default one-shot prompt for periodic runs (max 16 KB).",
    )

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(
                f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_dataset_filter_bounds(self) -> "OntogenConfPutRequest":
        _check_dataset_filter_bounds(self.dataset_filter)
        return self


class OntogenConfPatchRequest(BaseModel):
    is_enabled: bool | None = Field(default=None)
    schedule_tier: str | None = Field(
        default=None,
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'.",
    )
    dataset_filter: dict[str, Any] | None = Field(default=None)
    default_run_prompt: str | None = Field(
        default=None,
        max_length=16_000,
        description="Default one-shot prompt for periodic runs (max 16 KB).",
    )

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(
                f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_dataset_filter_bounds(self) -> "OntogenConfPatchRequest":
        if self.dataset_filter is not None:
            _check_dataset_filter_bounds(self.dataset_filter)
        return self


# ── Seeds ─────────────────────────────────────────────────────────────────────


class SeedListItem(BaseModel):
    seed_id: str = Field(description="Unique identifier of the seed")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")
    preview: str = Field(
        description="Short Markdown snippet (first ~200 chars, newlines normalised)",
    )


class SeedListResponse(PaginatedResponse):
    seeds: list[SeedListItem] = Field(default=[], description="Page of seed preview records")


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
    status: str = Field(description="Lifecycle status: pending_review, approved, or rejected")
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
    status: str = Field(description="Lifecycle status: pending_review, approved, or rejected")
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
    status: str = Field(description="Lifecycle status: pending_review, approved, or rejected")
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
