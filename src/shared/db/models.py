"""SQLAlchemy 2.0 ORM models for all DataSpoke PostgreSQL tables.

All tables live in the ``dataspoke`` schema. See spec/feature/BACKEND_SCHEMA.md
for the authoritative column/index definitions.

Migration 001 created the initial schema; migration 002 dropped concept-model
tables, renamed generation_* → metagen_*, renamed is_active → is_enabled, and
added ontogen_* tables + node_embeddings.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

from src.shared.config import EMBEDDING_DIMENSION as _EMBEDDING_DIM

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
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    identifier: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    auth: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_dag_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="OK")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── dataset_registry ─────────────────────────────────────────────────────────


class DatasetRegistry(Base):
    __tablename__ = "dataset_registry"
    __table_args__ = {"schema": SCHEMA}

    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    datahub_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
    rules: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
        Index("ix_validation_results_run_id", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    partition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    assertion_result: Mapped[str] = mapped_column(Text, nullable=False)
    issues: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── metagen_configs ───────────────────────────────────────────────────────────


class MetagenConfig(Base):
    __tablename__ = "metagen_configs"
    __table_args__ = (
        UniqueConstraint("dataset_urn"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    targets: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    code_refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── metagen_results ───────────────────────────────────────────────────────────


class MetagenResult(Base):
    __tablename__ = "metagen_results"
    __table_args__ = (
        Index("ix_metagen_results_urn_generated", "dataset_urn", desc("generated_at")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    proposals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    field_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


# ── metric_definitions ───────────────────────────────────────────────────────


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(Text, nullable=False)
    measurement_query: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
    breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    measured_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
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
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
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
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_overview_config_singleton"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    layout: Mapped[str] = mapped_column(Text, nullable=False, default="force")
    color_by: Mapped[str] = mapped_column(Text, nullable=False, default="quality_score")
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── ontogen_config (singleton) ────────────────────────────────────────────────


class OntogenConfig(Base):
    __tablename__ = "ontogen_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_ontogen_config_singleton"),
        CheckConstraint(
            "max_manual_queries_per_dataset >= 0",
            name="ck_ontogen_config_max_manual_queries_gte0",
        ),
        CheckConstraint(
            "max_system_queries_per_dataset >= 0",
            name="ck_ontogen_config_max_system_queries_gte0",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    max_manual_queries_per_dataset: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    max_system_queries_per_dataset: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    default_run_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── ontogen_seeds ─────────────────────────────────────────────────────────────


class OntogenSeed(Base):
    __tablename__ = "ontogen_seeds"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── ontogen_nodes ─────────────────────────────────────────────────────────────


class OntogenNode(Base):
    __tablename__ = "ontogen_nodes"
    __table_args__ = (
        UniqueConstraint("name"),
        CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_nodes_id_no_double_underscore",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_review")
    glossary_term_urn: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships (back-refs filled lazily)
    dataset_maps: Mapped[list["DatasetNodeMap"]] = relationship(
        "DatasetNodeMap", back_populates="node", foreign_keys="[DatasetNodeMap.node_id]"
    )
    embedding: Mapped["NodeEmbedding | None"] = relationship(
        "NodeEmbedding", back_populates="node", uselist=False
    )


# ── ontogen_edges ─────────────────────────────────────────────────────────────


class OntogenEdge(Base):
    __tablename__ = "ontogen_edges"
    __table_args__ = (
        UniqueConstraint("label"),
        CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_edges_id_no_double_underscore",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    semantics: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_review")
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── ontogen_triples ───────────────────────────────────────────────────────────


class OntogenTriple(Base):
    __tablename__ = "ontogen_triples"
    __table_args__ = (
        CheckConstraint(
            "id = subject_node_id || '__' || edge_id || '__' || object_node_id",
            name="ck_ontogen_triples_id_composite",
        ),
        Index("ix_ontogen_triples_subject", "subject_node_id"),
        Index("ix_ontogen_triples_object", "object_node_id"),
        Index("ix_ontogen_triples_edge", "edge_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_node_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_nodes.id"), nullable=False
    )
    edge_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_edges.id"), nullable=False
    )
    object_node_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_nodes.id"), nullable=False
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_review")
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    subject_node: Mapped["OntogenNode"] = relationship(
        "OntogenNode", foreign_keys=[subject_node_id]
    )
    edge: Mapped["OntogenEdge"] = relationship("OntogenEdge", foreign_keys=[edge_id])
    object_node: Mapped["OntogenNode"] = relationship(
        "OntogenNode", foreign_keys=[object_node_id]
    )


# ── dataset_node_map ──────────────────────────────────────────────────────────


class DatasetNodeMap(Base):
    __tablename__ = "dataset_node_map"
    __table_args__ = (
        Index("ix_dataset_node_map_node_id", "node_id"),
        {"schema": SCHEMA},
    )

    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    node_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_nodes.id"), primary_key=True
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    node: Mapped["OntogenNode"] = relationship(
        "OntogenNode", back_populates="dataset_maps", foreign_keys=[node_id]
    )


# ── node_embeddings (pgvector) ────────────────────────────────────────────────
# The embedding column is a pgvector ``vector(N)`` type. We use the pgvector
# SQLAlchemy integration (pgvector>=0.3.0) for the ORM column type.


class NodeEmbedding(Base):
    __tablename__ = "node_embeddings"
    __table_args__ = {"schema": SCHEMA}

    node_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_nodes.id"), primary_key=True
    )
    # Vector dimension is fixed at EMBEDDING_DIMENSION (1536 by default).
    # The HNSW index (node_embeddings_embedding_hnsw_idx) is created by migration 002 via raw DDL.
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_review")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    node: Mapped["OntogenNode"] = relationship("OntogenNode", back_populates="embedding")
