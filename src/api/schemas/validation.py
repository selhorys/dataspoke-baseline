"""Validation API schemas — passive result-store model."""

import re
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.db.models import DEFAULT_VALIDATION_ATTRIBUTE
from src.shared.metric_conf import MAX_TIME_WINDOW_SEC

_VARIABLE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,99}\Z")

# Reject ASCII control characters except \t (0x09) and \n (0x0a), plus DEL (0x7f).
_DESC_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ValidationVariable(BaseModel):
    """One declared variable: a name plus a human-readable description."""

    #: Singular noun this entry kind is called by in its rejection messages, so a
    #: ``parameter`` entry is not reported as a bad ``variable``. A ``ClassVar``
    #: rather than a field: Pydantic keeps it off the model's schema.
    _entry_label: ClassVar[str] = "variable"

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
            raise ValueError(f"{cls._entry_label} description must not exceed 200 characters")
        if _DESC_CTRL_RE.search(v):
            raise ValueError(f"{cls._entry_label} description contains control characters")
        return v


class ValidationParameter(ValidationVariable):
    """One declared pipeline hyperparameter: a name plus a description.

    Same shape and same per-item rules as :class:`ValidationVariable`, in its own
    namespace — a name may appear in both lists, since uniqueness is per list.
    DataSpoke never interprets a parameter: it is opaque storage so a pipeline's
    tunables travel with the rule that uses them.

    Spec: spec/feature/VALIDATION.md §Rule Configuration.
    """

    _entry_label: ClassVar[str] = "parameter"


class ValidationAttribute(BaseModel):
    """Declared data-arrival cadence of the dataset.

    A **closed, typed** object rather than an open bag: only the named fields
    below are stored, and each carries its own default, so a conf written without
    the section still holds a complete object. The governance ``validation-score``
    measurer reads it to anchor its per-dataset criterion window
    (spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window).

    That downstream read is what bounds the fields. The measurer shifts its window
    back by ``cadence_offset * cadence_unit`` seconds, so it is the **product**
    that has to stay representable — bounding either factor alone does not bound
    it. This model is the sole gate on that product: the column is plain JSONB
    with no ``CHECK``, exactly as ``metric_conf`` is.

    Spec: spec/feature/VALIDATION.md §Rule Configuration.
    """

    cadence_unit: int = Field(
        default=DEFAULT_VALIDATION_ATTRIBUTE["cadence_unit"],
        gt=0,
        le=MAX_TIME_WINDOW_SEC,
        description=(
            "Period in seconds at which the dataset's data is expected to arrive. "
            f"An integer in [1, {MAX_TIME_WINDOW_SEC}] (ten years — the same ceiling "
            "as metric_conf.time_window_sec); a boolean is not accepted"
        ),
    )
    cadence_offset: int = Field(
        default=DEFAULT_VALIDATION_ATTRIBUTE["cadence_offset"],
        ge=0,
        description=(
            "How many cadence_unit periods the arriving data lags the arrival "
            "instant. Daily D-1 data is unit=86400, offset=0; daily D-8 data is "
            "unit=86400, offset=7. A non-negative integer whose product with "
            f"cadence_unit must not exceed {MAX_TIME_WINDOW_SEC} seconds; a boolean "
            "is not accepted"
        ),
    )

    @field_validator("cadence_unit", "cadence_offset", mode="before")
    @classmethod
    def _reject_bool(cls, v: Any) -> Any:
        """Reject a JSON boolean before Pydantic coerces it to 1/0.

        ``bool`` subclasses ``int``, so in Pydantic's lax mode ``true`` would
        otherwise be admitted as a one-second cadence. ``metric_conf``'s
        ``time_window_sec`` refuses a boolean for the same reason
        (:func:`src.shared.metric_conf.is_valid_time_window_sec`); the two
        window-shaping integers answer the same way.
        """
        if isinstance(v, bool):
            raise ValueError("must be an integer, not a boolean")
        return v

    @model_validator(mode="after")
    def _check_window_shift(self) -> "ValidationAttribute":
        """Bound ``cadence_offset * cadence_unit`` — the measurer's window shift.

        The per-field bounds cannot express this: ``cadence_offset`` carries no
        ceiling of its own, so only the product check keeps the measurer's
        ``timedelta(seconds=offset * unit)`` from overflowing. The rejected value
        is deliberately left out of the message — it is caller-controlled and
        arbitrarily long, and the bound is what the caller needs to be told.
        """
        if self.cadence_offset * self.cadence_unit > MAX_TIME_WINDOW_SEC:
            raise ValueError(
                "cadence_offset * cadence_unit must not exceed "
                f"{MAX_TIME_WINDOW_SEC} seconds (ten years)"
            )
        return self


def _validate_named_entries[T: ValidationVariable](
    entries: list[T], field_label: str, entry_label: str
) -> list[T]:
    """Shared 1–200-entries + unique-name check for ``variables`` and ``parameter``.

    The two lists share the per-item rules but not the namespace: uniqueness is
    checked per list, so the same name may appear in both.

    Two labels because the two messages name two different things: the cardinality
    checks are about the *field* (``field_label`` — "variables", "parameter"),
    while the uniqueness check is about the *entries* in it (``entry_label``,
    singular — "variable name", not "variables name").
    """
    if len(entries) < 1:
        raise ValueError(f"{field_label} must have at least 1 entry")
    if len(entries) > 200:
        raise ValueError(f"{field_label} must not exceed 200 entries")
    names = [entry.name for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError(f"{entry_label} names must be unique")
    return entries


def _validate_variables(
    variables: list[ValidationVariable],
) -> list[ValidationVariable]:
    return _validate_named_entries(
        variables, "variables", ValidationVariable._entry_label
    )


def _validate_parameter(
    parameter: list[ValidationParameter] | None,
) -> list[ValidationParameter] | None:
    """Validate the optional ``parameter`` list.

    ``None`` is "the section is absent" and passes through. An explicit ``[]`` is
    rejected exactly as an empty ``variables`` is, so ``null`` stays the single
    spelling of "clear" (spec/feature/VALIDATION.md §Rule Configuration).
    """
    if parameter is None:
        return None
    return _validate_named_entries(
        parameter, "parameter", ValidationParameter._entry_label
    )


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
    attribute: ValidationAttribute = Field(
        default_factory=ValidationAttribute,
        description=(
            "Declared data-arrival cadence {cadence_unit, cadence_offset}. Written "
            "wholesale: the complete per-field-defaulted object replaces the "
            "previous value rather than deep-merging into it. Omitting the key "
            "stores the all-defaults object"
        ),
    )
    parameter: list[ValidationParameter] | None = Field(
        default=None,
        description=(
            "Optional pipeline hyperparameters, each a {name, description} object "
            "under the same rules as variables in its own namespace. Omitting the "
            "key on PUT stores the section as absent, clearing any previous value; "
            "an explicit empty list is rejected"
        ),
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

    @field_validator("parameter", mode="after")
    @classmethod
    def _check_parameter(
        cls, v: list[ValidationParameter] | None
    ) -> list[ValidationParameter] | None:
        return _validate_parameter(v)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "description": "Daily row count plus key column means and null counts",
                "variables": [
                    {"name": "row_cnt", "description": "Daily row count"},
                    {"name": "col1_mean", "description": "Mean of col1"},
                    {"name": "col2_null_cnt", "description": "Null count of col2"},
                ],
                "attribute": {"cadence_unit": 86400, "cadence_offset": 0},
                "parameter": [
                    {"name": "z_threshold", "description": "Std-dev cutoff for outliers"}
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
    attribute: ValidationAttribute | None = Field(
        default=None,
        description=(
            "Replacement data-arrival cadence. Supplied, it is written wholesale — "
            "the complete per-field-defaulted object replaces the previous value, "
            "so {\"cadence_offset\": 7} also resets cadence_unit to its default. "
            "Omitted, the stored value is unchanged"
        ),
    )
    parameter: list[ValidationParameter] | None = Field(
        default=None,
        description=(
            "Replacement hyperparameter list. Omitting the key leaves the stored "
            "value unchanged; an explicit null clears the section; a non-empty list "
            "(1–200 entries, validated as variables are) replaces it wholesale. An "
            "empty list is rejected, so null is the single spelling of 'clear'"
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

    @field_validator("parameter", mode="after")
    @classmethod
    def _check_parameter(
        cls, v: list[ValidationParameter] | None
    ) -> list[ValidationParameter] | None:
        return _validate_parameter(v)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "description": "Updated daily row count check",
                "attribute": {"cadence_unit": 86400, "cadence_offset": 7},
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
            "Keys must match [a-z][a-z0-9_]{0,99}; ≤ 200 entries "
            "(a result may report partial coverage, including none)."
        )
    )

    @field_validator("variables", mode="after")
    @classmethod
    def _check_variables(
        cls, v: dict[str, float]
    ) -> dict[str, float]:
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
    attribute: ValidationAttribute = Field(
        description=(
            "Declared data-arrival cadence {cadence_unit, cadence_offset}. Always "
            "present — a conf written without the section carries the all-defaults object"
        )
    )
    parameter: list[ValidationParameter] | None = Field(
        default=None,
        description=(
            "Declared pipeline hyperparameters. The key is omitted entirely from "
            "the body when the section is absent; it is never serialized as null. "
            "The conf routes carry response_model_exclude_none for exactly that"
        ),
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
    """One row in the cross-dataset list response.

    ``description``/``variable_count``/``latest_*`` are null for uncovered rows
    (registered datasets with no validation conf) under ``coverage=uncovered|both``.
    """

    dataset_urn: str
    description: str | None = None
    variable_count: int | None = None
    latest_data_time: datetime | None = None
    latest_score: float | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ValidationListResponse(PaginatedResponse):
    """Response for GET /spoke/validation."""

    validations: list[ValidationListItem] = Field(
        default=[],
        description="Cross-dataset validation list, each row aggregates conf + latest result",
    )
