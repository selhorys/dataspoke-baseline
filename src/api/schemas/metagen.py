"""Metadata Generation request/response schemas — UC4."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.api.schemas.common import PaginatedResponse

_DATASET_FILTER_LIST_CAP = 1000


def _check_dataset_filter_bounds(dataset_filter: dict[str, Any]) -> None:
    """Raise ValueError if any list field in *dataset_filter* exceeds the cap.

    Spec: API.md §Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns}
    ≤ 1,000 entries per dimension.
    """
    for key in ("dataset_urns", "tags", "glossary_terms"):
        val = dataset_filter.get(key)
        if val is not None and len(val) > _DATASET_FILTER_LIST_CAP:
            raise ValueError(
                f"dataset_filter.{key} may not exceed "
                f"{_DATASET_FILTER_LIST_CAP} entries"
            )


# ── Global conf ───────────────────────────────────────────────────────────────


class MetagenGlobalConfResponse(BaseModel):
    is_enabled: bool
    schedule_tier: Literal["hourly", "daily", "weekly"] | None
    dataset_filter: dict[str, Any] = Field(default_factory=dict)
    result_limit: int
    overwrite_pending: bool
    updated_at: datetime


class MetagenGlobalConfPutRequest(BaseModel):
    is_enabled: bool
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = None
    dataset_filter: dict[str, Any] = Field(default_factory=dict)
    result_limit: int = Field(default=3, ge=1, le=20)
    overwrite_pending: bool = True

    @model_validator(mode="after")
    def validate_dataset_filter_bounds(self) -> "MetagenGlobalConfPutRequest":
        _check_dataset_filter_bounds(self.dataset_filter)
        return self


class MetagenGlobalConfPatchRequest(BaseModel):
    is_enabled: bool | None = None
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = None
    dataset_filter: dict[str, Any] | None = None
    result_limit: int | None = Field(default=None, ge=1, le=20)
    overwrite_pending: bool | None = None

    @model_validator(mode="after")
    def validate_dataset_filter_bounds(self) -> "MetagenGlobalConfPatchRequest":
        if self.dataset_filter is not None:
            _check_dataset_filter_bounds(self.dataset_filter)
        return self


# ── Per-dataset boundary ──────────────────────────────────────────────────────


class MetagenBoundaryResponse(BaseModel):
    dataset_urn: str
    is_enabled: bool
    allowed: list[Literal["dataset.description", "column.description"]]
    owner: str | None
    created_at: datetime
    updated_at: datetime


class MetagenBoundaryPutRequest(BaseModel):
    is_enabled: bool
    allowed: list[Literal["dataset.description", "column.description"]] = Field(
        default_factory=list
    )
    owner: str | None = None


class MetagenBoundaryPatchRequest(BaseModel):
    is_enabled: bool | None = None
    allowed: list[Literal["dataset.description", "column.description"]] | None = None
    owner: str | None = None


# ── Item & candidate ──────────────────────────────────────────────────────────


class MetagenItemSummary(BaseModel):
    dataset_urn: str
    item_id: str
    kind: Literal["dataset.description", "column.description"]
    field_path: str | None
    status: Literal["pending", "llm_approved", "approved"]
    candidate_count: int
    composite_id: str


class MetagenItemListResponse(PaginatedResponse):
    items: list[MetagenItemSummary] = Field(default_factory=list)


class MetagenCandidate(BaseModel):
    candidate_id: str
    item_id: str
    dataset_urn: str
    value: str
    confidence_score: float
    status: Literal["llm_approved", "approved", "rejected"]
    evidence: dict[str, Any]
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_id: str | None


class MetagenItemDetailResponse(MetagenItemSummary):
    candidates: list[MetagenCandidate]


# ── Run ───────────────────────────────────────────────────────────────────────


class MetagenRunRequest(BaseModel):
    dataset_urns: list[str] | None = None
    dry_run: bool = False


class MetagenRunResponse(BaseModel):
    run_id: str
    status: Literal["success", "failure"]
    dry_run: bool
    unresolved_urns: list[str]
    counts: dict[str, int]
    producer_iterations: int | None
    debate_outcome: Literal["accept", "turns_exhausted", "cycle_detected"] | None


# ── Review ────────────────────────────────────────────────────────────────────


class MetagenReviewRequest(BaseModel):
    verdict: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=2000)
