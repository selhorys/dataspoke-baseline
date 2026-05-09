"""Shared string enumerations used across API schemas and backend services."""

from enum import StrEnum


class IngestionConfigStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"


class MetricTheme(StrEnum):
    QUALITY = "quality"
    GOVERNANCE = "governance"
    FRESHNESS = "freshness"


class EventStatus(StrEnum):
    SUCCESS = "success"
    OK = "ok"
    FAILURE = "failure"
    ERROR = "error"
    RUNNING = "running"
    WARNING = "warning"
    INFO = "info"
