"""Metadata Generation request/response schemas — UC4."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse

# Max length for individual field-path entries in ReviewResultRequest.fields
_BoundedFieldEntry = Annotated[str, StringConstraints(max_length=512)]

_VALID_TARGETS = frozenset({"dataset.description", "column.description", "cross_data.md"})
_VALID_TIERS = frozenset({"hourly", "daily", "weekly"})

# ── Conf ──────────────────────────────────────────────────────────────────────


class MetagenConfPutRequest(BaseModel):
    targets: list[str] = Field(
        description=(
            "Metadata fields to generate. Allowed values: "
            "'dataset.description', 'column.description', 'cross_data.md'"
        )
    )
    code_refs: dict[str, Any] | None = Field(
        default=None, description="Optional code references used as LLM context"
    )
    is_enabled: bool = Field(
        default=False,
        description="Whether scheduled metagen runs are enabled",
    )
    schedule_tier: str | None = Field(
        default=None,
        description=(
            "Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'. "
            "Required when is_enabled is true."
        ),
    )
    owner: str = Field(
        description="Owner identifier (email or user URN) responsible for this config",
    )

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, v: list[str]) -> list[str]:
        invalid = [t for t in v if t not in _VALID_TARGETS]
        if invalid:
            raise ValueError(
                f"Invalid targets: {invalid}. Allowed: {sorted(_VALID_TARGETS)}"
            )
        return v

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v


class MetagenConfPatchRequest(BaseModel):
    targets: list[str] | None = Field(default=None)
    code_refs: dict[str, Any] | None = Field(default=None)
    is_enabled: bool | None = Field(default=None)
    schedule_tier: str | None = Field(default=None)
    owner: str | None = Field(default=None)

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        invalid = [t for t in v if t not in _VALID_TARGETS]
        if invalid:
            raise ValueError(
                f"Invalid targets: {invalid}. Allowed: {sorted(_VALID_TARGETS)}"
            )
        return v

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v


class MetagenConfResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metagen config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    targets: list[str] = Field(description="Configured generation targets")
    code_refs: dict[str, Any] | None = Field(
        default=None, description="Code references used as LLM context"
    )
    is_enabled: bool = Field(description="Whether scheduled metagen runs are enabled")
    schedule_tier: str | None = Field(
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'"
    )
    status: str = Field(description="Config lifecycle status")
    owner: str = Field(description="Owner identifier responsible for this config")
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


# ── Results ───────────────────────────────────────────────────────────────────


class MetagenResultResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metagen result")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    proposals: dict[str, Any] = Field(
        description="LLM-proposed metadata per target field"
    )
    field_status: dict[str, Any] = Field(
        description="Per-field review status: pending, approved, or rejected"
    )
    run_id: str = Field(description="Run identifier that produced this result")
    generated_at: datetime = Field(description="UTC timestamp when proposals were generated")
    last_reviewed_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent review action"
    )


class MetagenResultListResponse(PaginatedResponse):
    results: list[MetagenResultResponse] = Field(
        default=[], description="Page of metagen result records"
    )


# ── Review ────────────────────────────────────────────────────────────────────


class ReviewResultRequest(BaseModel):
    verdict: Literal["approve", "reject"] = Field(
        description=(
            "Review decision: 'approve' to accept, 'reject' to dismiss. "
            "Combined with 'fields' for partial approval."
        )
    )
    fields: list[_BoundedFieldEntry] | None = Field(
        default=None,
        max_length=200,
        description=(
            "Optional list of field paths / cross-data action IDs to "
            "approve or reject selectively. "
            "Omit for whole-proposal verdict."
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional human-readable explanation for the verdict",
    )


# ── Run ───────────────────────────────────────────────────────────────────────


class RunMetagenRequest(BaseModel):
    dry_run: bool = Field(
        default=False,
        description="When true, generate proposals without persisting results",
    )


class MetagenRunResponse(SingleResponse):
    id: str = Field(description="Result ID (run_id on dry_run)")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    proposals: dict[str, Any] = Field(description="LLM-proposed metadata per target")
    field_status: dict[str, Any] = Field(description="Per-field review status")
    run_id: str = Field(description="Run identifier")
    generated_at: datetime = Field(description="UTC timestamp when proposals were generated")
    last_reviewed_at: datetime | None = Field(default=None)


# ── Cross-dataset list view ───────────────────────────────────────────────────


class MetagenListItem(BaseModel):
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    run_id: str = Field(description="Most recent run identifier")
    proposals: dict[str, Any] = Field(description="Most recent LLM proposals")
    field_status: dict[str, Any] = Field(description="Per-field review status")
    generated_at: datetime = Field(description="UTC timestamp of the most recent run")
    last_reviewed_at: datetime | None = Field(default=None)


class MetagenListResponse(PaginatedResponse):
    results: list[MetagenListItem] = Field(
        default=[], description="Page of cross-dataset metagen records"
    )
