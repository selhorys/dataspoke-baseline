"""Metric definition, result, and attribute models (DG)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import MetricIssueStatus, MetricIssueType, MetricTheme, IssuePriority


class UpsertMetricConfigRequest(BaseModel):
    title: str = Field(description="Short human-readable title for the metric, e.g. 'Orders table freshness'")
    description: str = Field(description="Detailed description of what the metric measures and why it matters")
    theme: MetricTheme = Field(description="Metric theme grouping: 'quality', 'governance', or 'freshness'")
    measurement_query: dict[str, Any] = Field(
        description="Query configuration for metric measurement. Structure depends on metric type. Example: {\"type\": \"sql\", \"query\": \"SELECT COUNT(*) FROM orders WHERE created_at > NOW() - INTERVAL '1 day'\"}"
    )
    schedule_cron: str | None = Field(default=None, description="Cron expression for periodic measurement runs, e.g. '0 6 * * *' for daily at 06:00 UTC")
    alarm_enabled: bool = Field(default=False, description="Whether to trigger alarms when the measurement breaches the alarm_threshold")
    alarm_threshold: dict[str, Any] | None = Field(
        default=None,
        description="Threshold configuration for triggering alarms. Example: {\"operator\": \"lt\", \"value\": 0.8}"
    )
    alarm_recipients: list[str] | None = Field(default=None, description="List of email addresses or user URNs to notify when an alarm is triggered")
    is_active: bool = Field(default=True, description="Whether the metric is active and scheduled for measurement")

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Orders table freshness",
                "description": "Measures how recently the orders table was updated to ensure data is not stale",
                "theme": "freshness",
                "measurement_query": {
                    "type": "sql",
                    "query": "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(updated_at)))/3600 AS hours_since_update FROM orders",
                },
                "schedule_cron": "0 * * * *",
                "alarm_enabled": True,
                "alarm_threshold": {"operator": "gt", "value": 24},
                "alarm_recipients": ["de-team@example.com"],
                "is_active": True,
            }
        }
    }

class PatchMetricConfigRequest(BaseModel):
    title: str | None = Field(default=None, description="Updated metric title")
    description: str | None = Field(default=None, description="Updated metric description")
    theme: MetricTheme | None = Field(default=None, description="Updated metric theme: 'quality', 'governance', or 'freshness'")
    measurement_query: dict[str, Any] | None = Field(default=None, description="Updated query configuration for metric measurement.")
    schedule_cron: str | None = Field(default=None, description="Updated cron expression for periodic measurement runs.")
    alarm_enabled: bool | None = Field(default=None, description="Updated alarm enabled flag.")
    alarm_threshold: dict[str, Any] | None = Field(default=None, description="Updated alarm threshold configuration.")
    alarm_recipients: list[str] | None = Field(default=None, description="Updated list of alarm notification recipients.")
    is_active: bool | None = Field(default=None, description="Set to true to enable the metric, false to pause")


class RunMetricRequest(BaseModel):
    dry_run: bool = Field(default=False, description="When true, simulate the measurement without persisting results")


class MetricDefinitionResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric definition")
    title: str = Field(description="Human-readable metric title")
    description: str = Field(description="Detailed metric description")
    theme: MetricTheme = Field(description="Metric theme: 'quality', 'governance', or 'freshness'")
    measurement_query: dict[str, Any] = Field(description="Query configuration used to measure this metric")
    schedule_cron: str | None = Field(description="Cron expression for scheduled measurement runs")
    alarm_enabled: bool = Field(description="Whether alarms are enabled for this metric")
    alarm_threshold: dict[str, Any] | None = Field(description="Alarm trigger threshold configuration")
    alarm_recipients: list[str] | None = Field(default=None, description="Alarm notification recipients")
    is_active: bool = Field(description="Whether the metric is actively being measured")
    created_at: datetime = Field(description="UTC timestamp when the metric was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class MetricDefinitionListResponse(PaginatedResponse):
    metrics: list[MetricDefinitionResponse] = Field(default=[], description="Page of metric definition records")


class MetricAttrResponse(SingleResponse):
    """Lightweight attributes view."""

    id: str = Field(description="Unique identifier of the metric")
    title: str = Field(description="Human-readable metric title")
    theme: MetricTheme = Field(description="Metric theme: 'quality', 'governance', or 'freshness'")
    is_active: bool = Field(description="Whether the metric is actively being measured")
    alarm_enabled: bool = Field(description="Whether alarms are enabled for this metric")
    schedule_cron: str | None = Field(description="Cron expression for scheduled measurement runs")
    latest_value: float | None = Field(default=None, description="Most recent measured value for this metric")
    latest_measured_at: datetime | None = Field(default=None, description="UTC timestamp of the most recent measurement")


class MetricResultResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the metric result")
    metric_id: str = Field(description="Identifier of the metric definition this result belongs to")
    value: float = Field(description="Measured metric value")
    breakdown: dict[str, Any] | None = Field(default=None, description="Optional breakdown of the metric value by dimension or sub-category")
    alarm_triggered: bool = Field(description="Whether this measurement triggered an alarm")
    run_id: str = Field(description="Kestra execution ID for the run that produced this result")
    measured_at: datetime = Field(description="UTC timestamp when the measurement was taken")


class MetricResultListResponse(PaginatedResponse):
    results: list[MetricResultResponse] = Field(default=[], description="Page of metric result records")


class MetricRunResultResponse(SingleResponse):
    run_id: str = Field(description="Kestra execution ID for this metric run")
    status: str = Field(description="Execution status returned by Kestra, e.g. 'RUNNING' or 'SUCCESS'")
    detail: dict[str, Any] = Field(default={}, description="Additional execution metadata returned by Kestra")


# ── Metric Issues ────────────────────────────────────────────────────────


class PatchMetricIssueRequest(BaseModel):
    status: MetricIssueStatus | None = Field(default=None, description="Updated issue status: 'open', 'in_progress', or 'dismissed'")
    assignee: str | None = Field(default=None, description="User identifier (email or URN) to assign this issue to")
    due_date: datetime | None = Field(default=None, description="Target resolution date (ISO-8601 UTC)")


class DismissMetricIssueRequest(BaseModel):
    reason: str | None = Field(default=None, description="Optional explanation for why the issue is being dismissed")


class MetricIssueResponse(SingleResponse):
    metric_issue_id: str = Field(description="Unique identifier of the metric issue")
    metric_id: str = Field(description="Identifier of the metric definition that raised this issue")
    dataset_urn: str = Field(description="DataHub URN of the dataset affected by this issue")
    issue_type: MetricIssueType = Field(description="Category of the issue, e.g. 'no_description', 'stale', 'low_quality'")
    priority: IssuePriority = Field(description="Severity priority assigned by the system: 'critical', 'high', 'medium', or 'low'")
    status: MetricIssueStatus = Field(description="Current issue status: 'open', 'in_progress', or 'dismissed'")
    assignee: str | None = Field(default=None, description="User identifier currently assigned to resolve this issue")
    description: str = Field(description="Human-readable description of the issue and its impact")
    estimated_fix_minutes: int = Field(description="Estimated time in minutes to resolve this issue")
    projected_score_impact: float = Field(description="Estimated quality score improvement (0.0–1.0) if this issue is resolved")
    due_date: datetime | None = Field(default=None, description="Target resolution date")
    resolved_at: datetime | None = Field(default=None, description="UTC timestamp when the issue was resolved, null if still open")
    created_at: datetime = Field(description="UTC timestamp when the issue was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class MetricIssueListResponse(PaginatedResponse):
    metric_issues: list[MetricIssueResponse] = Field(default=[], description="Page of metric issue records")
