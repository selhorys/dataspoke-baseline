"""SQLAlchemy 2.0 ORM models for all DataSpoke PostgreSQL tables.

All tables live in the ``dataspoke`` schema. See spec/feature/BACKEND_SCHEMA.md
for the authoritative column/index definitions.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    desc,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TIMESTAMPTZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


SCHEMA = "dataspoke"


# ── ingestion_configs ────────────────────────────────────────────────────────


class IngestionConfig(Base):
    __tablename__ = "ingestion_configs"
    __table_args__ = (
        UniqueConstraint("dataset_urn"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[dict] = mapped_column(JSONB, nullable=False)
    deep_spec_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── validation_configs ───────────────────────────────────────────────────────


class ValidationConfig(Base):
    __tablename__ = "validation_configs"
    __table_args__ = (
        UniqueConstraint("dataset_urn"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    sla_target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── validation_results ───────────────────────────────────────────────────────


class ValidationResult(Base):
    __tablename__ = "validation_results"
    __table_args__ = (
        Index("ix_validation_results_urn_measured", "dataset_urn", desc("measured_at")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    dimensions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    issues: Mapped[dict] = mapped_column(JSONB, nullable=False)
    anomalies: Mapped[dict] = mapped_column(JSONB, nullable=False)
    recommendations: Mapped[dict] = mapped_column(JSONB, nullable=False)
    alternatives: Mapped[dict] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── generation_configs ───────────────────────────────────────────────────────


class GenerationConfig(Base):
    __tablename__ = "generation_configs"
    __table_args__ = (
        UniqueConstraint("dataset_urn"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    target_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    code_refs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── generation_results ───────────────────────────────────────────────────────


class GenerationResult(Base):
    __tablename__ = "generation_results"
    __table_args__ = (
        Index("ix_generation_results_urn_generated", "dataset_urn", desc("generated_at")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    proposals: Mapped[dict] = mapped_column(JSONB, nullable=False)
    similar_diffs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    approval_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


# ── concept_categories ───────────────────────────────────────────────────────


class ConceptCategory(Base):
    __tablename__ = "concept_categories"
    __table_args__ = (
        UniqueConstraint("name"),
        Index("ix_concept_categories_parent", "parent_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.concept_categories.id"),
        nullable=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    parent: Mapped["ConceptCategory | None"] = relationship(
        "ConceptCategory", remote_side="ConceptCategory.id"
    )


# ── dataset_concept_map ──────────────────────────────────────────────────────


class DatasetConceptMap(Base):
    __tablename__ = "dataset_concept_map"
    __table_args__ = (
        Index("ix_dataset_concept_map_concept", "concept_id"),
        {"schema": SCHEMA},
    )

    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.concept_categories.id"),
        primary_key=True,
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── concept_relationships ────────────────────────────────────────────────────


class ConceptRelationship(Base):
    __tablename__ = "concept_relationships"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    concept_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.concept_categories.id"),
        nullable=False,
    )
    concept_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.concept_categories.id"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── metric_definitions ───────────────────────────────────────────────────────


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(Text, nullable=False)
    measurement_query: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    alarm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    alarm_threshold: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── metric_results ───────────────────────────────────────────────────────────


class MetricResult(Base):
    __tablename__ = "metric_results"
    __table_args__ = (
        Index("ix_metric_results_metric_measured", "metric_id", desc("measured_at")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.metric_definitions.id"), nullable=False
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    breakdown: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    alarm_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── metric_issues ────────────────────────────────────────────────────────────


class MetricIssue(Base):
    __tablename__ = "metric_issues"
    __table_args__ = (
        Index("ix_metric_issues_status_priority", "status", "priority"),
        Index("ix_metric_issues_urn_status", "dataset_urn", "status"),
        Index("ix_metric_issues_metric_created", "metric_id", desc("created_at")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.metric_definitions.id"), nullable=False
    )
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    issue_type: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    assignee: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_fix_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    projected_score_impact: Mapped[float] = mapped_column(Float, nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── events ───────────────────────────────────────────────────────────────────


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_entity_occurred", "entity_type", "entity_id", desc("occurred_at")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── department_mapping ───────────────────────────────────────────────────────


class DepartmentMapping(Base):
    __tablename__ = "department_mapping"
    __table_args__ = {"schema": SCHEMA}

    owner_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    department: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── overview_config ──────────────────────────────────────────────────────────


class OverviewConfig(Base):
    __tablename__ = "overview_config"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    layout: Mapped[str] = mapped_column(Text, nullable=False, default="force")
    color_by: Mapped[str] = mapped_column(Text, nullable=False, default="quality_score")
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )
