"""Metadata Generation request/response schemas — UC4."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.api.schemas._dataset_filter import validate_dataset_filter
from src.api.schemas.common import PaginatedResponse

# ── Conf collection ───────────────────────────────────────────────────────────

_FILTER_DESC = (
    "Optional scope filter. Keys: origin (DataHub FabricType, AND-ed with the OR-group), "
    "tags (list[str], OR), glossary_terms (list[str], OR), "
    "dataset_urns (list[str], OR). Each list dimension capped at 1,000 entries."
)


class MetagenConfResponse(BaseModel):
    id: str
    name: str
    is_enabled: bool
    schedule_tier: Literal["hourly", "daily", "weekly"] | None
    dataset_filter: dict[str, Any] = Field(default_factory=dict)
    result_limit: int
    overwrite_pending: bool
    dataset_affected_count: int = 0
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MetagenConfListResponse(PaginatedResponse):
    confs: list[MetagenConfResponse] = Field(default_factory=list)


class MetagenConfCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    is_enabled: bool = False
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = None
    dataset_filter: dict[str, Any] = Field(default_factory=dict, description=_FILTER_DESC)
    result_limit: int = Field(default=3, ge=1, le=20)
    overwrite_pending: bool = True

    @model_validator(mode="after")
    def validate_dataset_filter_fields(self) -> "MetagenConfCreateRequest":
        validate_dataset_filter(self.dataset_filter)
        return self


class MetagenConfPutRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    is_enabled: bool
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = None
    dataset_filter: dict[str, Any] = Field(default_factory=dict, description=_FILTER_DESC)
    result_limit: int = Field(default=3, ge=1, le=20)
    overwrite_pending: bool = True

    @model_validator(mode="after")
    def validate_dataset_filter_fields(self) -> "MetagenConfPutRequest":
        validate_dataset_filter(self.dataset_filter)
        return self


class MetagenConfPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_enabled: bool | None = None
    schedule_tier: Literal["hourly", "daily", "weekly"] | None = None
    dataset_filter: dict[str, Any] | None = None
    result_limit: int | None = Field(default=None, ge=1, le=20)
    overwrite_pending: bool | None = None

    @model_validator(mode="after")
    def validate_dataset_filter_fields(self) -> "MetagenConfPatchRequest":
        if self.dataset_filter is not None:
            validate_dataset_filter(self.dataset_filter)
        return self


# ── Uncovered ─────────────────────────────────────────────────────────────────


class MetagenUncoveredRow(BaseModel):
    dataset_urn: str
    reason: Literal["no_conf_match", "boundary_blocked"]


class MetagenUncoveredResponse(PaginatedResponse):
    datasets: list[MetagenUncoveredRow] = Field(default_factory=list)


# ── Covered datasets (per-conf) ───────────────────────────────────────────────


class MetagenCoveredDatasetSummary(BaseModel):
    dataset_urn: str
    is_enabled: bool
    allowed: list[Literal["dataset.description", "column.description"]] = Field(
        default_factory=list
    )
    blocked: bool
    reason: Literal["boundary_blocked"] | None


class MetagenCoveredDatasetListResponse(PaginatedResponse):
    datasets: list[MetagenCoveredDatasetSummary] = Field(default_factory=list)


# ── Per-dataset boundary ──────────────────────────────────────────────────────


class MetagenBoundaryResponse(BaseModel):
    dataset_urn: str
    is_enabled: bool
    allowed: list[Literal["dataset.description", "column.description"]]
    created_at: datetime
    updated_at: datetime


class MetagenBoundaryPutRequest(BaseModel):
    is_enabled: bool
    allowed: list[Literal["dataset.description", "column.description"]] = Field(
        default_factory=list
    )


class MetagenBoundaryPatchRequest(BaseModel):
    is_enabled: bool | None = None
    allowed: list[Literal["dataset.description", "column.description"]] | None = None


# ── Item & candidate ──────────────────────────────────────────────────────────


class MetagenItemSummary(BaseModel):
    dataset_urn: str
    item_id: str
    kind: Literal["dataset.description", "column.description"]
    field_path: str | None
    status: Literal["pending", "llm_approved", "approved"]
    candidate_count: int
    created_at: datetime
    composite_id: str


class MetagenItemListResponse(PaginatedResponse):
    """Per-dataset item list (`/spoke/common/data/{urn}/attr/metagen/item`)."""

    items: list[MetagenItemSummary] = Field(default_factory=list)
    candidate_count: int = 0


class MetagenItemIndexResponse(PaginatedResponse):
    """Cross-dataset item index (`/spoke/metagen/item`)."""

    items: list[MetagenItemSummary] = Field(default_factory=list)


class MetagenCandidate(BaseModel):
    candidate_id: str
    conf_id: str | None
    conf_name: str | None
    item_id: str
    dataset_urn: str
    run_id: str
    value: str
    confidence_score: float
    status: Literal["llm_approved", "approved", "rejected"]
    evidence: dict[str, Any]
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_id: str | None


class MetagenItemDetailResponse(MetagenItemSummary):
    candidates: list[MetagenCandidate]


# ── Per-dataset rollup ────────────────────────────────────────────────────────


class MetagenDatasetSummary(BaseModel):
    dataset_urn: str
    is_enabled: bool
    allowed: list[Literal["dataset.description", "column.description"]] = Field(
        default_factory=list
    )
    item_count: int
    approved_count: int
    rejected_count: int
    candidate_count: int
    last_modified_at: datetime | None


class MetagenDatasetListResponse(PaginatedResponse):
    datasets: list[MetagenDatasetSummary] = Field(default_factory=list)


# ── Run ───────────────────────────────────────────────────────────────────────


class MetagenRunRequest(BaseModel):
    dataset_urns: list[str] | None = None


class MetagenRunResponse(BaseModel):
    run_id: str
    conf_id: str
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
