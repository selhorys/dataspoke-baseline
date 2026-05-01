"""Metric definition, result, and attribute models (DG)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import MetricTheme

_VALID_TIERS = frozenset({"hourly", "daily", "weekly"})


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
    schedule_tier: str | None = Field(
        default=None,
        description="Schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'",
    )
    is_enabled: bool = Field(
        default=True, description="Whether the metric is active and scheduled for measurement"
    )

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v

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
    schedule_tier: str | None = Field(
        default=None, description="Updated schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'."
    )
    is_enabled: bool | None = Field(
        default=None, description="Set to true to enable the metric, false to pause"
    )

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v


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
    schedule_tier: str | None = Field(description="Schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'")
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
    schedule_tier: str | None = Field(description="Schedule tier for periodic measurement runs: 'hourly', 'daily', or 'weekly'")
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
    run_id: str = Field(description="Airflow DAG run ID for this metric run")
    status: str = Field(
        description="Execution status returned by Airflow, e.g. 'running' or 'success'"
    )
    detail: dict[str, Any] = Field(
        default={}, description="Additional execution metadata returned by Airflow"
    )
