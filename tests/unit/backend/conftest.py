"""Shared factories and helpers for backend unit tests.

Consolidates mock object builders and DB query mock patterns that are
reused across multiple backend service test modules.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from tests.unit.conftest import route_db_execute

# ── DB query mock helpers ────────────────────────────────────────────────────


def mock_paginated_query(db: AsyncMock, rows: list, total_count: int) -> None:
    """Route db.execute so the COUNT query returns total_count and the page-rows
    query returns rows — dispatched by SQL, not call order."""
    count_result = MagicMock()
    count_result.scalar.return_value = total_count
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    route_db_execute(db, [("count(", count_result)], default=rows_result)


def mock_scalar_query(db: AsyncMock, row: object | None) -> None:
    """Set up db.execute to return a scalar_one_or_none result."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)


def mock_scalars_query(db: AsyncMock, rows: list) -> None:
    """Set up db.execute to return a `.scalars().all()` list (one query)."""
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = rows
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
    event_type: str = "INGESTION.COMPLETE",
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


# ── OntogenNode / OntogenEdge / OntogenTriple factories ──────────────────────


def make_ontogen_node_row(
    *,
    id: str = "book",
    name: str = "Book",
    description: str = "A book entity",
    confidence_score: float = 0.9,
    status: str = "llm_pending",
) -> MagicMock:
    """Create a mock OntogenNode row."""
    row = MagicMock()
    row.id = id
    row.name = name
    row.description = description
    row.confidence_score = confidence_score
    row.status = status
    row.evidence = {"datasets": [], "run_at": "2025-01-01T00:00:00+00:00"}
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def make_ontogen_edge_row(
    *,
    id: str = "has_edition",
    label: str = "has edition",
    semantics: str | None = "One book has many editions",
    confidence_score: float = 0.85,
    status: str = "llm_pending",
) -> MagicMock:
    """Create a mock OntogenEdge row."""
    row = MagicMock()
    row.id = id
    row.label = label
    row.semantics = semantics
    row.confidence_score = confidence_score
    row.status = status
    row.evidence = {"run_at": "2025-01-01T00:00:00+00:00"}
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def make_ontogen_triple_row(
    *,
    subject_node_id: str = "book",
    edge_id: str = "has_edition",
    object_node_id: str = "edition",
    confidence_score: float = 0.8,
    status: str = "llm_pending",
) -> MagicMock:
    """Create a mock OntogenTriple row.

    ID format: {subject_node_id}__{edge_id}__{object_node_id}
    """
    row = MagicMock()
    row.id = f"{subject_node_id}__{edge_id}__{object_node_id}"
    row.subject_node_id = subject_node_id
    row.edge_id = edge_id
    row.object_node_id = object_node_id
    row.confidence_score = confidence_score
    row.status = status
    row.evidence = {"datasets": [], "run_at": "2025-01-01T00:00:00+00:00"}
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def make_dataset_node_map_row(
    *,
    dataset_urn: str = "urn:li:dataset:(urn:li:dataPlatform:postgres,test.table,PROD)",
    node_id: str = "book",
    confidence_score: float = 0.9,
    status: str = "llm_pending",
    is_primary: bool = True,
) -> MagicMock:
    """Create a mock DatasetNodeMap row."""
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.node_id = node_id
    row.confidence_score = confidence_score
    row.status = status
    row.is_primary = is_primary
    row.created_at = datetime.now(tz=UTC)
    return row


# ── MetagenResult factory ────────────────────────────────────────────────────


def make_metagen_result_row(
    *,
    dataset_urn: str = "urn:li:dataset:(urn:li:dataPlatform:postgres,test.table,PROD)",
    proposals: dict | None = None,
    field_status: dict | None = None,
    generated_at: datetime | None = None,
) -> MagicMock:
    """Create a mock MetagenResult row.

    proposals and field_status are JSONB dicts per BACKEND_SCHEMA.
    """
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.proposals = proposals or {
        "dataset.description": "Generated description for the dataset.",
        "column.description.id": "Primary key column.",
    }
    row.field_status = field_status or {
        "dataset.description": "pending",
        "column.description.id": "pending",
    }
    row.run_id = uuid.uuid4()
    row.generated_at = generated_at or datetime.now(tz=UTC)
    row.last_reviewed_at = None
    return row


# ── Metric breakdown factory ─────────────────────────────────────────────────


def make_metric_breakdown_row(
    breakdown: dict | None = None,
) -> MagicMock:
    """Create a mock MetricResult row with unified breakdown shape.

    breakdown shape:
    {"dataset_count": <int>, "datasets": [{"urn": ..., "category": ..., "detail": ...}]}
    """
    row = MagicMock()
    row.id = uuid.uuid4()
    row.metric_id = "ingestion-freshness"
    row.value = 0.5
    row.breakdown = breakdown or {
        "dataset_count": 2,
        "datasets": [
            {
                "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t1,PROD)",
                "category": "fresh",
                "detail": {"last_event_at": "2025-01-01T00:00:00+00:00"},
            },
            {
                "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t2,PROD)",
                "category": "stale",
                "detail": {"last_event_at": "2024-01-01T00:00:00+00:00"},
            },
        ],
    }
    row.measured_at = datetime.now(tz=UTC)
    return row


