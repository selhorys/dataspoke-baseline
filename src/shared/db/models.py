"""SQLAlchemy 2.0 ORM models for all DataSpoke PostgreSQL tables.

All tables live in the ``dataspoke`` schema. See spec/feature/BACKEND_SCHEMA.md
for the authoritative column/index definitions.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CHAR,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.shared.config import EMBEDDING_DIMENSION as _EMBEDDING_DIM

TIMESTAMPTZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


SCHEMA = "dataspoke"


# ── users ────────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("google_sub", name="uq_users_google_sub"),
        CheckConstraint(
            "password_hash IS NOT NULL OR google_sub IS NOT NULL",
            name="ck_users_auth_method",
        ),
        CheckConstraint(
            "role IN ('Admin', 'Editor', 'Reader')",
            name="ck_users_role",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="Reader")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    api_tokens: Mapped[list["ApiToken"]] = relationship(
        "ApiToken", back_populates="user", cascade="all, delete-orphan"
    )
    password_reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan"
    )


# ── api_tokens ────────────────────────────────────────────────────────────────


class ApiToken(Base):
    __tablename__ = "api_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        CheckConstraint(
            "role_snapshot IN ('Admin', 'Editor', 'Reader')",
            name="ck_api_tokens_role_snapshot",
        ),
        Index(
            "ix_api_tokens_user_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    role_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="api_tokens")


# ── password_reset_tokens ─────────────────────────────────────────────────────


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index(
            "ix_password_reset_tokens_user_expires",
            "user_id",
            desc("expires_at"),
        ),
        {"schema": SCHEMA},
    )

    token_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship("User", back_populates="password_reset_tokens")


# ── ingestion_source ─────────────────────────────────────────────────────────


class IngestionSource(Base):
    __tablename__ = "ingestion_source"
    __table_args__ = (
        Index("ix_ingestion_source_parent", "parent_source_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    datahub_source_urn: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Self-referential link: a row is a CLI wrapper iff parent_source_id IS NOT
    # NULL, pointing at its regular DATAHUB_MANAGED parent. NULL ⇒ regular source.
    parent_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.ingestion_source.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="OK")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    datasets: Mapped[list["IngestionSourceDataset"]] = relationship(
        "IngestionSourceDataset", back_populates="source", cascade="all, delete-orphan"
    )


# ── ingestion_source_dataset ─────────────────────────────────────────────────


class IngestionSourceDataset(Base):
    __tablename__ = "ingestion_source_dataset"
    __table_args__ = {"schema": SCHEMA}

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.ingestion_source.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    derivation: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    source: Mapped["IngestionSource"] = relationship("IngestionSource", back_populates="datasets")


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
        CheckConstraint(
            "jsonb_array_length(variables) BETWEEN 1 AND 200",
            name="ck_validation_configs_variables_length",
        ),
        {"schema": SCHEMA},
    )

    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
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
        CheckConstraint(
            "score BETWEEN 0.0 AND 1.0",
            name="ck_validation_results_score_range",
        ),
        Index("ix_validation_results_urn_data_time", "dataset_urn", desc("data_time")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    data_time: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ingestion_time: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ── metagen_config (collection) ───────────────────────────────────────────────


class MetagenConfig(Base):
    __tablename__ = "metagen_config"
    __table_args__ = (
        UniqueConstraint("name", name="uq_metagen_config_name"),
        CheckConstraint("result_limit BETWEEN 1 AND 20", name="ck_metagen_config_result_limit"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    overwrite_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── metagen_boundary ──────────────────────────────────────────────────────────


class MetagenBoundary(Base):
    __tablename__ = "metagen_boundary"
    __table_args__ = {"schema": SCHEMA}

    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── metagen_items ─────────────────────────────────────────────────────────────


class MetagenItem(Base):
    __tablename__ = "metagen_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('dataset.description', 'column.description')",
            name="ck_metagen_items_kind",
        ),
        {"schema": SCHEMA},
    )

    dataset_urn: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    field_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── metagen_candidates ────────────────────────────────────────────────────────


class MetagenCandidate(Base):
    __tablename__ = "metagen_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_urn", "item_id"],
            [f"{SCHEMA}.metagen_items.dataset_urn", f"{SCHEMA}.metagen_items.item_id"],
        ),
        Index(
            "ix_metagen_candidates_item_status_created",
            "conf_id",
            "dataset_urn",
            "item_id",
            "status",
            "created_at",
        ),
        Index("ix_metagen_candidates_run_id", "run_id"),
        Index(
            "ix_metagen_candidates_one_approved",
            "dataset_urn",
            "item_id",
            unique=True,
            postgresql_where=text("status = 'approved'"),
        ),
        CheckConstraint(
            "status IN ('llm_approved', 'approved', 'rejected')",
            name="ck_metagen_candidates_status",
        ),
        {"schema": SCHEMA},
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conf_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.metagen_config.id", ondelete="SET NULL"),
        nullable=True,
    )
    dataset_urn: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    embedding: Mapped["MetagenCandidateEmbedding | None"] = relationship(
        "MetagenCandidateEmbedding", back_populates="candidate", uselist=False
    )


# ── metagen_candidate_embeddings (pgvector) ───────────────────────────────────


class MetagenCandidateEmbedding(Base):
    __tablename__ = "metagen_candidate_embeddings"
    __table_args__ = {"schema": SCHEMA}

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.metagen_candidates.candidate_id"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)

    candidate: Mapped["MetagenCandidate"] = relationship(
        "MetagenCandidate", back_populates="embedding"
    )


# ── metric_definitions ───────────────────────────────────────────────────────


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    metric_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    metric_conf: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    dataset_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    values: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False, default=dict)
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


# ── ontogen_config (singleton) ────────────────────────────────────────────────


class OntogenConfig(Base):
    __tablename__ = "ontogen_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_ontogen_config_singleton"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    default_run_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── runtime_config (singleton) ────────────────────────────────────────────────


class RuntimeConfig(Base):
    __tablename__ = "runtime_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_runtime_config_singleton"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    llm_provider: Mapped[str] = mapped_column(Text, nullable=False, default="gemini")
    llm_model: Mapped[str] = mapped_column(Text, nullable=False, default="gemini-3.5-flash")
    ontogen_llm_max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    ontogen_debate_max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    ontogen_debate_rag_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    ontogen_debate_reviewer_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    metagen_llm_max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    metagen_debate_max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    metagen_debate_rag_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    metagen_debate_reviewer_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    metagen_confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    metagen_ontology_rag_node_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    metagen_ontology_rag_edge_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    metagen_ontology_rag_triple_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    validation_score_n_intervals: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    stub_redis_client: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stub_llm_client: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stub_pgvector_manager: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stub_notification_service: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auth_datahub_corp_group: Mapped[str] = mapped_column(
        Text, nullable=False, default="dataspoke-users"
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── peripheral_config ─────────────────────────────────────────────────────────


class PeripheralConfig(Base):
    __tablename__ = "peripheral_config"
    __table_args__ = (
        CheckConstraint(
            "name IN ('datahub', 'langfuse', 'smtp')", name="ck_peripheral_config_name"
        ),
        {"schema": SCHEMA},
    )

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── ontogen_seeds ─────────────────────────────────────────────────────────────


class OntogenSeed(Base):
    __tablename__ = "ontogen_seeds"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    embedding: Mapped["EdgeEmbedding | None"] = relationship(
        "EdgeEmbedding", back_populates="edge", uselist=False
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
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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
    embedding: Mapped["TripleEmbedding | None"] = relationship(
        "TripleEmbedding", back_populates="triple", uselist=False
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
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
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
    # The HNSW index (node_embeddings_embedding_hnsw_idx) is created by the
    # alembic migration via raw DDL.
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    node: Mapped["OntogenNode"] = relationship("OntogenNode", back_populates="embedding")


# ── edge_embeddings (pgvector) ────────────────────────────────────────────────


class EdgeEmbedding(Base):
    __tablename__ = "edge_embeddings"
    __table_args__ = {"schema": SCHEMA}

    edge_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_edges.id"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    edge: Mapped["OntogenEdge"] = relationship("OntogenEdge", back_populates="embedding")


# ── triple_embeddings (pgvector) ──────────────────────────────────────────────


class TripleEmbedding(Base):
    __tablename__ = "triple_embeddings"
    __table_args__ = {"schema": SCHEMA}

    triple_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ontogen_triples.id"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="llm_pending")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    triple: Mapped["OntogenTriple"] = relationship("OntogenTriple", back_populates="embedding")
