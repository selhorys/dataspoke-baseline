"""Metric definition, result, and issue models (DG)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateMetricRequest(BaseModel):
    name: str
    dataset_urn: str
    expression: str
    threshold: dict[str, Any] = {}
    schedule: str | None = None
    owner: str


class PatchMetricRequest(BaseModel):
    name: str | None = None
    expression: str | None = None
    threshold: dict[str, Any] | None = None
    schedule: str | None = None
    status: str | None = None


class RunMetricRequest(BaseModel):
    dry_run: bool = False


class MetricDefinitionResponse(SingleResponse):
    id: str
    name: str
    dataset_urn: str
    expression: str
    threshold: dict[str, Any]
    schedule: str | None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class MetricDefinitionListResponse(PaginatedResponse):
    metrics: list[MetricDefinitionResponse] = []


class MetricResultResponse(SingleResponse):
    id: str
    metric_id: str
    value: float
    is_anomaly: bool = False
    computed_at: datetime


class MetricResultListResponse(PaginatedResponse):
    results: list[MetricResultResponse] = []


class MetricIssueResponse(SingleResponse):
    id: str
    metric_id: str
    issue_type: str
    severity: str
    detail: str
    field_path: str | None = None
    detected_at: datetime


class MetricIssueListResponse(PaginatedResponse):
    issues: list[MetricIssueResponse] = []
