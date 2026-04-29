"""Shared string enumerations used across API schemas and backend services."""

from enum import StrEnum


class IngestionConfigStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"


class AssertionResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"


class MetricTheme(StrEnum):
    QUALITY = "quality"
    GOVERNANCE = "governance"
    FRESHNESS = "freshness"


class ValidationRuleType(StrEnum):
    FRESHNESS = "freshness"
    VOLUME = "volume"
    FIELD = "field"
    SCHEMA = "schema"
    SQL = "sql"
    CUSTOM = "custom"


class EventStatus(StrEnum):
    SUCCESS = "success"
    OK = "ok"
    FAILURE = "failure"
    ERROR = "error"
    RUNNING = "running"
    WARNING = "warning"
    INFO = "info"
