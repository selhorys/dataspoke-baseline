"""Metric definition, result, and attribute models (Governance).

Spec: spec/API.md §Metric (/spoke/governance/metric), spec/USE_CASE_en.md §UC5.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.api.schemas._dataset_filter import validate_dataset_filter
from src.api.schemas.common import PaginatedResponse, SingleResponse

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
        tw = metric_conf.get("time_window_sec")
        if tw is None or not isinstance(tw, int) or tw <= 0:
            raise ValueError(
                "metric_conf.time_window_sec must be a positive int"
                f" for metric_type '{metric_type}'"
            )
    elif metric_type == "doc-health":
        if metric_conf != {}:
            raise ValueError("metric_conf must be {} for metric_type 'doc-health'")


def _check_metrics_subset(metric_type: str, metrics: list[str]) -> None:
    """Raise ValueError when metrics[] contains keys not emitted by metric_type."""
    allowed = _EMITTED_KEYS.get(metric_type, set())
    unknown = set(metrics) - allowed
    if unknown:
        raise ValueError(
            f"metrics[] contains keys not emitted by '{metric_type}': {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )


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
    metrics: list[str] = Field(
        description="Subset of the type's emitted keys to persist in results"
    )
    metric_conf: dict[str, Any] = Field(
        description=(
            "Type-specific config. 'ingestion-freshness' and 'validation-score' require "
            "time_window_sec (positive int seconds); 'doc-health' takes {}"
        )
    )
    schedule_tier: _ScheduleTier | None = Field(
        default=None,
        description="Schedule tier: 'hourly', 'daily', or 'weekly' (null = on-demand only)",
    )
    dataset_filter: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional scope filter. Keys: origin (DataHub FabricType, AND-ed with the OR-group), "
            "tags (list[str], OR), glossary_terms (list[str], OR), "
            "dataset_urns (list[str], OR). Each list dimension capped at 1,000 entries."
        ),
    )

    @model_validator(mode="after")
    def validate_fields(self) -> "ReplaceMetricConfigRequest":
        validate_dataset_filter(self.dataset_filter)
        _check_metric_conf_for_type(self.metric_type, self.metric_conf)
        _check_metrics_subset(self.metric_type, self.metrics)
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
    metrics: list[str] | None = Field(default=None)
    metric_conf: dict[str, Any] | None = Field(default=None)
    schedule_tier: _ScheduleTier | None = Field(default=None)
    dataset_filter: dict[str, Any] | None = Field(default=None)

    @model_validator(mode="after")
    def validate_fields(self) -> "PatchMetricConfigRequest":
        if self.dataset_filter is not None:
            validate_dataset_filter(self.dataset_filter)
        return self


class MetricDefinitionResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric definition")
    mode: str = Field(description="Measurement mode: 'active' or 'passive'")
    is_enabled: bool = Field(description="Whether scheduled measurement is enabled")
    metric_type: str = Field(description="Built-in metric type")
    title: str = Field(description="Human-readable metric title")
    description: str = Field(description="What this metric measures")
    metrics: list[str] = Field(description="Subset of the type's emitted keys to persist")
    metric_conf: dict[str, Any] = Field(description="Type-specific configuration")
    schedule_tier: _ScheduleTier | None = Field(
        description="Schedule tier: 'hourly', 'daily', or 'weekly'"
    )
    dataset_filter: dict[str, Any] = Field(description="Scope filter for dataset resolution")
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
