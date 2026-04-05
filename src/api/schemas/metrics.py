"""Metric definition, result, and attribute models (DG)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import MetricTheme


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
            "Required key: 'type' ('poorly_documented' or 'stale_datasets'). "
            "Optional key: 'dataset_filter' with 'tags' and/or 'glossary_terms' lists for OR-filtering."
        )
    )
    schedule_cron: str | None = Field(
        default=None,
        description="Cron expression for periodic measurement runs, e.g. '0 6 * * *' for daily at 06:00 UTC",
    )
    is_active: bool = Field(
        default=True, description="Whether the metric is active and scheduled for measurement"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Poorly documented datasets",
                "description": "Measures how many datasets have descriptions shorter than 20 characters",
                "theme": "governance",
                "measurement_query": {
                    "type": "poorly_documented",
                    "dataset_filter": {
                        "tags": ["urn:li:tag:PII"],
                        "glossary_terms": ["urn:li:glossaryTerm:CustomerData"],
                    },
                },
                "schedule_cron": "0 6 * * *",
                "is_active": True,
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
        default=None, description="Updated query configuration for metric measurement."
    )
    schedule_cron: str | None = Field(
        default=None, description="Updated cron expression for periodic measurement runs."
    )
    is_active: bool | None = Field(
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
    schedule_cron: str | None = Field(description="Cron expression for scheduled measurement runs")
    is_active: bool = Field(description="Whether the metric is actively being measured")
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
    is_active: bool = Field(description="Whether the metric is actively being measured")
    schedule_cron: str | None = Field(description="Cron expression for scheduled measurement runs")
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
    run_id: str = Field(description="Kestra execution ID for this metric run")
    status: str = Field(
        description="Execution status returned by Kestra, e.g. 'RUNNING' or 'SUCCESS'"
    )
    detail: dict[str, Any] = Field(
        default={}, description="Additional execution metadata returned by Kestra"
    )
