"""Shared string enumerations used across API schemas and backend services."""

from enum import StrEnum


class IngestionConfigStatus(StrEnum):
    OK = "OK"
    DRAFT = "draft"


class GenerationConfigStatus(StrEnum):
    DRAFT = "draft"


class MetricIssueStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ConceptStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"


class AssertionResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"


class MetricTheme(StrEnum):
    QUALITY = "quality"
    GOVERNANCE = "governance"
    FRESHNESS = "freshness"


class MetricIssueType(StrEnum):
    NO_DESCRIPTION = "no_description"
    STALE = "stale"
    LOW_QUALITY = "low_quality"
    MISSING_OWNER = "missing_owner"
    NO_TAGS = "no_tags"
    FRESHNESS = "freshness"
    COMPLETENESS = "completeness"
    SCHEMA_DRIFT = "schema_drift"


class IssuePriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RelationshipType(StrEnum):
    RELATED_TO = "related_to"
    PART_OF = "part_of"
    DEPENDS_ON = "depends_on"
    OVERLAPS_WITH = "overlaps_with"


class OverviewColorBy(StrEnum):
    QUALITY_SCORE = "quality_score"
    MEDALLION = "medallion"


class OverviewLayout(StrEnum):
    FORCE = "force"
    HIERARCHICAL = "hierarchical"


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
