"""Metric definition, result, and attribute models (Governance).

Spec: spec/API.md §Metric (/spoke/governance/metric), spec/USE_CASE_en.md §UC5.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.api.schemas._dataset_filter import (
    DATASET_FILTER_FIELD_DESCRIPTION as _FILTER_DESC,
)
from src.api.schemas._dataset_filter import validate_dataset_filter
from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.metric_conf import (
    MAX_TIME_WINDOW_SEC,
    is_valid_time_window_sec,
    time_window_sec_error,
)

_ScheduleTier = Literal["hourly", "daily", "weekly"]

# Keys emitted by each built-in metric type
_EMITTED_KEYS: dict[str, set[str]] = {
    "ingestion-freshness": {"total", "ingested_in_time"},
    "validation-score": {"total", "validation_score_sum"},
    "doc-health": {"total", "doc_health"},
}


def _check_metric_conf_for_type(metric_type: str, metric_conf: dict[str, Any]) -> None:
    """Raise ValueError when metric_conf is missing or invalid for the given type."""
    if metric_type in ("ingestion-freshness", "validation-score"):
        if not is_valid_time_window_sec(metric_conf.get("time_window_sec")):
            raise ValueError(time_window_sec_error(metric_type))
    elif metric_type == "doc-health":
        if metric_conf != {}:
            raise ValueError("metric_conf must be {} for metric_type 'doc-health'")


class MetricSeries(BaseModel):
    """One chart series of a metric definition.

    The dashboard draws one line per descriptor, in ``idx`` order, stroked with
    ``color``. ``name`` selects which of the type's emitted keys the metric
    persists.
    """

    name: str = Field(description="One of the metric type's emitted value keys")
    color: str = Field(
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Line color as a #RRGGBB hex string",
    )
    idx: int = Field(ge=1, description="Display order, 1-based; unique within the metric")


def _check_metrics_series(metric_type: str, metrics: list[MetricSeries]) -> None:
    """Raise ValueError when the series descriptors are invalid for *metric_type*.

    Three rules: every ``name`` is one of the type's emitted keys, ``name`` is
    unique, and ``idx`` is unique (spec/API.md §Metric — Definition body).
    """
    allowed = _EMITTED_KEYS.get(metric_type, set())
    names = [series.name for series in metrics]
    idxs = [series.idx for series in metrics]

    unknown = set(names) - allowed
    if unknown:
        raise ValueError(
            f"metrics[] contains keys not emitted by '{metric_type}': {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    if len(set(names)) != len(names):
        raise ValueError("metrics[].name must be unique within the metric")
    if len(set(idxs)) != len(idxs):
        raise ValueError("metrics[].idx must be unique within the metric")


class ReplaceMetricConfigRequest(BaseModel):
    """Request body for PUT (replace) of an existing metric definition."""

    mode: Literal["active", "passive"] = Field(
        description="Measurement mode: 'active' (built-in measurer) or 'passive' (reserved)"
    )
    is_enabled: bool = Field(
        description="Whether scheduled measurement is enabled"
    )
    metric_type: Literal["ingestion-freshness", "validation-score", "doc-health"] = Field(
        description="Built-in metric type"
    )
    title: str = Field(description="Short human-readable title for the metric")
    description: str = Field(description="What this metric measures")
    metrics: list[MetricSeries] = Field(
        description=(
            "Series descriptors {name, color, idx}. 'name' is one of the type's emitted "
            "keys; 'name' and 'idx' are each unique within the metric"
        )
    )
    metric_conf: dict[str, Any] = Field(
        description=(
            "Type-specific config. 'ingestion-freshness' and 'validation-score' require "
            f"time_window_sec — the measurement window in seconds, an integer in "
            f"[1, {MAX_TIME_WINDOW_SEC}] (ten years); a boolean is not accepted. "
            "Evidence exactly at the window boundary counts as in-window. "
            "'doc-health' takes {}"
        )
    )
    schedule_tier: _ScheduleTier | None = Field(
        default=None,
        description="Schedule tier: 'hourly', 'daily', or 'weekly' (null = on-demand only)",
    )
    dataset_filter: str = Field(
        default="",
        description=_FILTER_DESC,
    )

    @model_validator(mode="after")
    def validate_fields(self) -> "ReplaceMetricConfigRequest":
        validate_dataset_filter(self.dataset_filter)
        _check_metric_conf_for_type(self.metric_type, self.metric_conf)
        _check_metrics_series(self.metric_type, self.metrics)
        return self


class CreateMetricConfigRequest(ReplaceMetricConfigRequest):
    """Request body for POST (create) of a new metric definition.

    Extends ``ReplaceMetricConfigRequest`` with a client-supplied ``metric_id``
    that must be unique.  Bad format yields 422; collision yields 409 METRIC_EXISTS.

    Spec: spec/API.md §Metric — metric_id note.
    """

    metric_id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$",
        description=(
            "Client-supplied kebab-case identifier, e.g. 'ingestion-freshness'. "
            "Must be unique — 409 METRIC_EXISTS on collision."
        ),
    )


class PatchMetricConfigRequest(BaseModel):
    mode: Literal["active", "passive"] | None = Field(default=None)
    is_enabled: bool | None = Field(default=None)
    metric_type: Literal["ingestion-freshness", "validation-score", "doc-health"] | None = Field(
        default=None
    )
    title: str | None = Field(default=None)
    description: str | None = Field(default=None)
    metrics: list[MetricSeries] | None = Field(default=None)
    metric_conf: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Type-specific config, replacing the stored dict wholesale. The *merged* "
            "definition is what is validated: with metric_type 'ingestion-freshness' or "
            f"'validation-score' the merged conf needs time_window_sec, an integer in "
            f"[1, {MAX_TIME_WINDOW_SEC}] (ten years) — a boolean is not accepted, and "
            "evidence exactly at the window boundary counts as in-window. 'doc-health' "
            "takes {}"
        ),
    )
    schedule_tier: _ScheduleTier | None = Field(default=None)
    dataset_filter: str | None = Field(default=None, description=_FILTER_DESC)

    @model_validator(mode="after")
    def validate_fields(self) -> "PatchMetricConfigRequest":
        if self.dataset_filter is not None:
            validate_dataset_filter(self.dataset_filter)
        # metrics[] is re-validated against the *merged* metric_type in
        # MetricsService.patch_metric_config — a patch may carry either field
        # alone, so the pair is only knowable there.
        return self


class MetricDefinitionResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric definition")
    mode: str = Field(description="Measurement mode: 'active' or 'passive'")
    is_enabled: bool = Field(description="Whether scheduled measurement is enabled")
    metric_type: str = Field(description="Built-in metric type")
    title: str = Field(description="Human-readable metric title")
    description: str = Field(description="What this metric measures")
    metrics: list[MetricSeries] = Field(
        description="Series descriptors {name, color, idx} the dashboard chart draws"
    )
    metric_conf: dict[str, Any] = Field(description="Type-specific configuration")
    schedule_tier: _ScheduleTier | None = Field(
        description="Schedule tier: 'hourly', 'daily', or 'weekly'"
    )
    dataset_filter: str = Field(description=_FILTER_DESC)
    created_at: datetime = Field(description="UTC timestamp when the metric was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class MetricDefinitionListItem(MetricDefinitionResponse):
    """List-row variant of the metric definition.

    Adds ``last_run_at`` — a list-only field derived from the latest
    METRIC.RUN_COMPLETE event. Single-GET / conf / create / update / patch use
    the bare ``MetricDefinitionResponse`` and do NOT expose this field.
    """

    last_run_at: datetime | None = Field(
        default=None,
        description=(
            "UTC timestamp of the latest METRIC.RUN_COMPLETE event for this metric, "
            "or null when it has never completed a run. Present on list rows only."
        ),
    )


class MetricDefinitionListResponse(PaginatedResponse):
    metrics: list[MetricDefinitionListItem] = Field(
        default=[], description="Page of metric definition records"
    )


class MetricAttrResponse(SingleResponse):
    """Lightweight attributes view."""

    id: str = Field(description="Unique identifier of the metric")
    mode: str = Field(description="Measurement mode: 'active' or 'passive'")
    metric_type: str = Field(description="Built-in metric type")
    title: str = Field(description="Human-readable metric title")
    is_enabled: bool = Field(description="Whether scheduled measurement is enabled")
    schedule_tier: _ScheduleTier | None = Field(
        description="Schedule tier: 'hourly', 'daily', or 'weekly'"
    )
    latest_values: dict[str, float] | None = Field(
        default=None, description="Most recent measured values for this metric"
    )
    latest_measured_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent measurement"
    )


class MetricResultResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric result")
    metric_id: str = Field(
        description="Identifier of the metric definition this result belongs to"
    )
    values: dict[str, float] = Field(description="Named float measurements")
    breakdown: dict[str, Any] | None = Field(
        default=None,
        description="Per-dataset breakdown (dataset_count + failed datasets list)",
    )
    measured_at: datetime = Field(description="UTC timestamp when the measurement was taken")


class MetricResultListResponse(PaginatedResponse):
    results: list[MetricResultResponse] = Field(
        default=[], description="Page of metric result records"
    )


class MetricDatasetRow(BaseModel):
    """One dataset the metric covers, with its latest verdict."""

    dataset_urn: str = Field(description="URN of the covered dataset")
    met: Literal["true", "false", "unknown"] = Field(
        description=(
            "Whether the dataset met the metric's criterion on the latest non-dry run. "
            "'unknown' means in scope but never evaluated — the metric has never run, "
            "or the dataset entered scope after the last run"
        )
    )
    last_check_at: datetime | None = Field(
        default=None,
        description=(
            "Per-dataset evidence timestamp, falling back to the run's measured_at "
            "(doc-health has no per-dataset timestamp, so it reports the run time). "
            "Null when the dataset has no verdict"
        ),
    )
    detail: dict[str, Any] | None = Field(
        default=None, description="Type-specific per-dataset metadata; null without a verdict"
    )


class MetricDatasetListResponse(PaginatedResponse):
    datasets: list[MetricDatasetRow] = Field(
        default=[], description="Page of covered datasets with their latest verdict"
    )
    attrs_synced_at: datetime | None = Field(
        default=None,
        description=(
            "Newest dataset_registry.attrs_synced_at over the datasets in scope — how "
            "fresh the attributes this scope was filtered against are. Scope-relative "
            "and unaffected by met filtering or paging; null when the scope is empty "
            "or no covered dataset has ever synced"
        ),
    )


class MetricRunResultResponse(SingleResponse):
    run_id: str = Field(
        description=(
            "Run identifier — the metric measurer's run_id when emitted via "
            "the DAG run conf, otherwise the Airflow DAG run identifier"
        )
    )
    status: str = Field(
        description=(
            "Terminal execution status: 'success' or 'failed' from Airflow, "
            "or 'success' / 'error' when overridden by the metric measurer"
        )
    )
    detail: dict[str, Any] = Field(
        default={},
        description="Metric measurer execution metadata (empty when the measurer emitted none)",
    )
