"""Shared string enumerations used across API schemas and backend services."""

from enum import StrEnum


class IngestionConfigStatus(StrEnum):
    OK = "OK"
    DRAFT = "draft"


class GenerationConfigStatus(StrEnum):
    DRAFT = "draft"


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
