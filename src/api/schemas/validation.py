"""Validation API schemas — passive result-store model."""

import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse

_VARIABLE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,99}\Z")

# Reject ASCII control characters except \t (0x09) and \n (0x0a), plus DEL (0x7f).
_DESC_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ValidationVariable(BaseModel):
    """One declared variable: a name plus a human-readable description."""

    name: str = Field(description="Variable name, matching [a-z][a-z0-9_]{0,99}")
    description: str = Field(
        description="Per-variable description (≤ 200 chars; empty string allowed)"
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _VARIABLE_RE.match(v):
            raise ValueError(f"{v!r} does not match [a-z][a-z0-9_]{{0,99}}")
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if len(v) > 200:
            raise ValueError("variable description must not exceed 200 characters")
        if _DESC_CTRL_RE.search(v):
            raise ValueError("variable description contains control characters")
        return v


def _validate_variables(
    variables: list[ValidationVariable],
) -> list[ValidationVariable]:
    if len(variables) < 1:
        raise ValueError("variables must have at least 1 entry")
    if len(variables) > 200:
        raise ValueError("variables must not exceed 200 entries")
    names = [v.name for v in variables]
    if len(names) != len(set(names)):
        raise ValueError("variable names must be unique")
    return variables


# ── Request models ────────────────────────────────────────────────────────────


class PutValidationConfRequest(BaseModel):
    """Body for PUT /attr/validation/conf (create or replace)."""

    description: str = Field(description="Free-form description (≤ 2,000 chars)")
    variables: list[ValidationVariable] = Field(
        description=(
            "Declared variables this validation slot will report. Each is a "
            "{name, description} object; names match [a-z][a-z0-9_]{0,99}, "
            "must be unique, 1–200 entries."
        )
    )

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if len(v) > 2000:
            raise ValueError("description must not exceed 2,000 characters")
        if _DESC_CTRL_RE.search(v):
            raise ValueError("description contains control characters")
        return v

    @field_validator("variables", mode="after")
    @classmethod
    def _check_variables(
        cls, v: list[ValidationVariable]
    ) -> list[ValidationVariable]:
        return _validate_variables(v)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "description": "Daily row count plus key column means and null counts",
                "variables": [
                    {"name": "row_cnt", "description": "Daily row count"},
                    {"name": "col1_mean", "description": "Mean of col1"},
                    {"name": "col2_null_cnt", "description": "Null count of col2"},
                ],
            }
        }
    )


class PatchValidationConfRequest(BaseModel):
    """Body for PATCH /attr/validation/conf (partial update)."""

    description: str | None = Field(default=None, description="Updated description (≤ 2,000 chars)")
    variables: list[ValidationVariable] | None = Field(
        default=None,
        description=(
            "Replacement variable list of {name, description} objects. "
            "If supplied, must satisfy 1–200 entries, unique names, each name "
            "matching [a-z][a-z0-9_]{0,99}. Supplying an empty list is rejected."
        ),
    )

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 2000:
            raise ValueError("description must not exceed 2,000 characters")
        if v is not None and _DESC_CTRL_RE.search(v):
            raise ValueError("description contains control characters")
        return v

    @field_validator("variables", mode="after")
    @classmethod
    def _check_variables(
        cls, v: list[ValidationVariable] | None
    ) -> list[ValidationVariable] | None:
        if v is None:
            return v
        return _validate_variables(v)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "description": "Updated daily row count check",
            }
        }
    )


class PostValidationResultRequest(BaseModel):
    """Body for POST /attr/validation/result."""

    data_time: AwareDatetime = Field(
        description="Time the underlying data is for (RFC 3339, timezone-aware)"
    )
    score: float = Field(
        description="Pass/fail score in [0.0, 1.0]"
    )
    variables: dict[str, Annotated[float, Field(allow_inf_nan=False)]] = Field(
        description=(
            "Measured values keyed by variable name (subset of conf.variables). "
            "Keys must match [a-z][a-z0-9_]{0,99}; 1–200 entries."
        )
    )

    @field_validator("variables", mode="after")
    @classmethod
    def _check_variables(
        cls, v: dict[str, float]
    ) -> dict[str, float]:
        if len(v) < 1:
            raise ValueError("variables must have at least 1 entry")
        if len(v) > 200:
            raise ValueError("variables must not exceed 200 entries")
        for key in v:
            if not _VARIABLE_RE.match(key):
                raise ValueError(
                    f"variables key {key!r} does not match [a-z][a-z0-9_]{{0,99}}"
                )
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "data_time": "2026-05-08T00:00:00Z",
                "score": 1.0,
                "variables": {
                    "row_cnt": 50.0,
                    "col1_mean": 31.1,
                    "col2_null_cnt": 15.0,
                },
            }
        }
    )


# ── Response models ───────────────────────────────────────────────────────────


class ValidationConfResponse(SingleResponse):
    """Response for GET/PUT/PATCH /attr/validation/conf."""

    dataset_urn: str = Field(description="DataHub URN of the dataset")
    description: str = Field(description="Free-form description")
    variables: list[ValidationVariable] = Field(
        description="Declared variables, each a {name, description} object"
    )
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")

    model_config = ConfigDict(from_attributes=True)


class ValidationResultRow(BaseModel):
    """A single collapsed result row (last-write-wins per data_time)."""

    data_time: datetime = Field(description="Time the data is for (partition timestamp)")
    score: float = Field(description="Pass/fail score in [0.0, 1.0]")
    variables: dict[str, Any] = Field(description="Measured variable values")

    model_config = ConfigDict(from_attributes=True)


class ValidationResultListResponse(PaginatedResponse):
    """Response for GET /attr/validation/result."""

    results: list[ValidationResultRow] = Field(
        default=[],
        description=(
            "Collapsed timeseries (last-write-wins per distinct data_time), "
            "sorted data_time DESC"
        ),
    )


class ValidationListItem(BaseModel):
    """One row in the cross-dataset list response."""

    dataset_urn: str
    description: str
    variable_count: int
    latest_data_time: datetime | None
    latest_score: float | None
    is_removed: bool
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ValidationListResponse(PaginatedResponse):
    """Response for GET /spoke/validation."""

    validations: list[ValidationListItem] = Field(
        default=[],
        description="Cross-dataset validation list, each row aggregates conf + latest result",
    )
