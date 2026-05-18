"""Metric definition, result, and attribute models (DG)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import MetricTheme

_ScheduleTier = Literal["hourly", "daily", "weekly"]


class UpsertMetricConfigRequest(BaseModel):
    title: str = Field(
        description="Short human-readable title for the metric, e.g. 'Poorly documented datasets'"
    )
    description: str = Field(
        description="Detailed description of what the metric measures and why it matters"
    )
    theme: MetricTheme = Field(
        description="Metric theme grouping: 'quality', 'governance', or 'freshness'"
    )
    measurement_query: dict[str, Any] = Field(
        description=(
            "Query configuration for metric measurement. "
            "Required key: 'aggregation' — registered measurer key, e.g. 'pct_fresh' or 'pct_rules_passing'. "
            "Optional key: 'dataset_filter' with 'tags', 'glossary_terms', and/or 'dataset_urns' lists for OR-filtering. "
            "Unsupported aggregation values return 422 INVALID_PARAMETER."
        )
    )
    schedule_tier: _ScheduleTier | None = Field(
        default=None,
        description="Schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'",
    )
    is_enabled: bool = Field(
        default=True, description="Whether the metric is active and scheduled for measurement"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Ingestion freshness coverage",
                "description": "Measures the percentage of datasets with a recent successful ingestion run",
                "theme": "freshness",
                "measurement_query": {
                    "aggregation": "pct_fresh",
                    "dataset_filter": {
                        "tags": ["urn:li:tag:PII"],
                    },
                },
                "schedule_tier": "daily",
                "is_enabled": True,
            }
        }
    }


class PatchMetricConfigRequest(BaseModel):
    title: str | None = Field(default=None, description="Updated metric title")
    description: str | None = Field(default=None, description="Updated metric description")
    theme: MetricTheme | None = Field(
        default=None, description="Updated metric theme: 'quality', 'governance', or 'freshness'"
    )
    measurement_query: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Updated query configuration for metric measurement. "
            "Required key: 'aggregation' — registered measurer key, e.g. 'pct_fresh' or 'pct_rules_passing'."
        ),
    )
    schedule_tier: _ScheduleTier | None = Field(
        default=None, description="Updated schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'."
    )
    is_enabled: bool | None = Field(
        default=None, description="Set to true to enable the metric, false to pause"
    )


class RunMetricRequest(BaseModel):
    dry_run: bool = Field(
        default=False,
        description="When true, simulate the measurement without persisting results",
    )


class MetricDefinitionResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric definition")
    title: str = Field(description="Human-readable metric title")
    description: str = Field(description="Detailed metric description")
    theme: MetricTheme = Field(description="Metric theme: 'quality', 'governance', or 'freshness'")
    measurement_query: dict[str, Any] = Field(
        description="Query configuration used to measure this metric"
    )
    schedule_tier: _ScheduleTier | None = Field(description="Schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'")
    is_enabled: bool = Field(description="Whether the metric is actively being measured")
    created_at: datetime = Field(description="UTC timestamp when the metric was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class MetricDefinitionListResponse(PaginatedResponse):
    metrics: list[MetricDefinitionResponse] = Field(
        default=[], description="Page of metric definition records"
    )


class MetricAttrResponse(SingleResponse):
    """Lightweight attributes view."""

    id: str = Field(description="Unique identifier of the metric")
    title: str = Field(description="Human-readable metric title")
    theme: MetricTheme = Field(description="Metric theme: 'quality', 'governance', or 'freshness'")
    is_enabled: bool = Field(description="Whether the metric is actively being measured")
    schedule_tier: _ScheduleTier | None = Field(description="Schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'")
    latest_value: float | None = Field(
        default=None, description="Most recent measured value for this metric"
    )
    latest_measured_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent measurement"
    )


class MetricResultResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric result")
    metric_id: str = Field(
        description="Identifier of the metric definition this result belongs to"
    )
    value: float = Field(description="Measured metric value")
    breakdown: dict[str, Any] | None = Field(
        default=None,
        description="Optional breakdown of the metric value by dimension or sub-category",
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
