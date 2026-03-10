"""Metric definition, result, and attribute models (DG)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class UpsertMetricConfigRequest(BaseModel):
    title: str
    description: str
    theme: str
    measurement_query: dict[str, Any]
    schedule: str | None = None
    alarm_enabled: bool = False
    alarm_threshold: dict[str, Any] | None = None
    active: bool = True


class PatchMetricConfigRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    theme: str | None = None
    measurement_query: dict[str, Any] | None = None
    schedule: str | None = None
    alarm_enabled: bool | None = None
    alarm_threshold: dict[str, Any] | None = None
    active: bool | None = None


class RunMetricRequest(BaseModel):
    dry_run: bool = False


class MetricDefinitionResponse(SingleResponse):
    id: str
    title: str
    description: str
    theme: str
    measurement_query: dict[str, Any]
    schedule: str | None
    alarm_enabled: bool
    alarm_threshold: dict[str, Any] | None
    active: bool
    created_at: datetime
    updated_at: datetime


class MetricDefinitionListResponse(PaginatedResponse):
    metrics: list[MetricDefinitionResponse] = []


class MetricAttrResponse(SingleResponse):
    """Lightweight attributes view."""

    id: str
    title: str
    theme: str
    active: bool
    alarm_enabled: bool
    schedule: str | None
    latest_value: float | None = None
    latest_measured_at: datetime | None = None


class MetricResultResponse(SingleResponse):
    id: str
    metric_id: str
    value: float
    breakdown: dict[str, Any] | None = None
    alarm_triggered: bool
    run_id: str
    measured_at: datetime


class MetricResultListResponse(PaginatedResponse):
    results: list[MetricResultResponse] = []


class MetricRunResultResponse(SingleResponse):
    run_id: str
    status: str
    detail: dict[str, Any] = {}
