"""Shared factories and helpers for backend unit tests.

Consolidates mock object builders and DB query mock patterns that are
reused across multiple backend service test modules.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

# ── DB query mock helpers ────────────────────────────────────────────────────


def mock_paginated_query(db: AsyncMock, rows: list, total_count: int) -> None:
    """Set up db.execute to return count then rows for paginated queries."""
    count_result = MagicMock()
    count_result.scalar.return_value = total_count
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(side_effect=[count_result, rows_result])


def mock_scalar_query(db: AsyncMock, row: object | None) -> None:
    """Set up db.execute to return a scalar_one_or_none result."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)


def mock_db_refresh(db: AsyncMock) -> None:
    """Set up db.refresh to populate server-default fields if missing."""

    async def _refresh(obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "status") and getattr(obj, "status") is None:
            obj.status = "active"
        now = datetime.now(tz=UTC)
        if hasattr(obj, "created_at") and getattr(obj, "created_at") is None:
            obj.created_at = now
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at") is None:
            obj.updated_at = now

    db.refresh = AsyncMock(side_effect=_refresh)


# ── Event row factory ────────────────────────────────────────────────────────


def make_event_row(
    *,
    entity_type: str = "dataset",
    entity_id: str = "urn:li:dataset:test",
    event_type: str = "ingestion.completed",
    status: str = "success",
    minutes_ago: int = 5,
) -> MagicMock:
    """Create a mock Event row. Used across dataset, validation, metrics,
    ingestion, generation, and ontology service tests."""
    row = MagicMock()
    row.id = uuid.uuid4()
    row.entity_type = entity_type
    row.entity_id = entity_id
    row.event_type = event_type
    row.status = status
    row.detail = {"source": "test"}
    row.occurred_at = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return row


# ── DataHub aspect mock factories ────────────────────────────────────────────


def make_datahub_props(name: str = "public.users", description: str = "User table") -> MagicMock:
    """Create a mock DatasetPropertiesClass-like object."""
    props = MagicMock()
    props.name = name
    props.description = description
    props.customProperties = {}
    return props


def make_datahub_ownership(owner_urns: list[str]) -> MagicMock:
    """Create a mock OwnershipClass-like object."""
    ownership = MagicMock()
    owners = []
    for urn in owner_urns:
        o = MagicMock()
        o.owner = urn
        owners.append(o)
    ownership.owners = owners
    return ownership


def make_datahub_tags(tag_urns: list[str]) -> MagicMock:
    """Create a mock GlobalTagsClass-like object."""
    tags_obj = MagicMock()
    tags = []
    for urn in tag_urns:
        t = MagicMock()
        t.tag = urn
        tags.append(t)
    tags_obj.tags = tags
    return tags_obj


def make_datahub_schema(field_paths: list[str]) -> MagicMock:
    """Create a mock SchemaMetadataClass-like object."""
    schema = MagicMock()
    fields = []
    for fp in field_paths:
        f = MagicMock()
        f.fieldPath = fp
        fields.append(f)
    schema.fields = fields
    return schema


# ── Concept / relationship factories ─────────────────────────────────────────


def make_concept_row(
    *,
    name: str = "test_concept",
    description: str = "A test concept",
    parent_id: uuid.UUID | None = None,
    status: str = "pending",
    version: int = 1,
) -> MagicMock:
    """Create a mock ConceptCategory row."""
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    row.description = description
    row.parent_id = parent_id
    row.status = status
    row.version = version
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def make_relationship_row(
    *,
    concept_a: uuid.UUID | None = None,
    concept_b: uuid.UUID | None = None,
    relationship_type: str = "related_to",
    confidence_score: float = 0.85,
) -> MagicMock:
    """Create a mock ConceptRelationship row."""
    row = MagicMock()
    row.id = uuid.uuid4()
    row.concept_a = concept_a or uuid.uuid4()
    row.concept_b = concept_b or uuid.uuid4()
    row.relationship_type = relationship_type
    row.confidence_score = confidence_score
    row.created_at = datetime.now(tz=UTC)
    return row


# ── Quality score factory ────────────────────────────────────────────────────


def make_quality_score_mock(overall: float) -> MagicMock:
    """Create a mock quality score object."""
    score = MagicMock()
    score.overall_score = overall
    score.dimensions = {"completeness": overall}
    return score


# ── Validation mock factories ────────────────────────────────────────────────


def make_mock_profile(
    timestamp_ms: int,
    row_count: int = 100,
    null_proportions: list[float] | None = None,
    col_count: int = 10,
) -> MagicMock:
    """Create a mock DatasetProfileClass with configurable timestamp and metrics."""
    profile = MagicMock()
    profile.timestampMillis = timestamp_ms
    profile.rowCount = row_count
    profile.columnCount = col_count
    if null_proportions:
        fps = []
        for np_val in null_proportions:
            fp = MagicMock()
            fp.nullProportion = np_val
            fps.append(fp)
        profile.fieldProfiles = fps
    else:
        profile.fieldProfiles = []
    return profile


def make_mock_operation(timestamp_ms: int) -> MagicMock:
    """Create a mock OperationClass with configurable timestamp."""
    op = MagicMock()
    op.lastUpdatedTimestamp = timestamp_ms
    op.timestampMillis = timestamp_ms
    return op
