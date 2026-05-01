"""Initial schema — all DataSpoke tables in the ``dataspoke`` schema.

Creates the full operational schema: 18 ORM-backed tables, the
``dataset_embeddings`` pgvector table used by the search/reindex pipeline,
the pgvector ``vector`` extension, and the Apache AGE ``dataspoke_ontogen``
graph used to materialise ``ontogen_triples`` for graph traversal.

Revision ID: 001
Revises: None
Create Date: 2026-04-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

from src.shared.config import EMBEDDING_DIMENSION

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"
TIMESTAMPTZ = TIMESTAMP(timezone=True)
DATASET_EMBEDDINGS_TABLE = "dataset_embeddings"
DATASET_EMBEDDINGS_HNSW_INDEX = "dataset_embeddings_embedding_hnsw_idx"
NODE_EMBEDDINGS_TABLE = "node_embeddings"
NODE_EMBEDDINGS_HNSW_INDEX = "node_embeddings_embedding_hnsw_idx"


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    # pgvector extension — idempotent. The Bitnami initdb hook also creates
    # this at cluster bootstrap; the guard here keeps the migration safe
    # against a DB provisioned without that hook.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── ingestion_configs ────────────────────────────────────────────────
    op.create_table(
        "ingestion_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False, server_default="active"),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("locator", JSONB, nullable=False),
        sa.Column("identifier", JSONB, nullable=False),
        sa.Column("auth", JSONB, nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("workflow_dag_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="OK"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── dataset_registry ─────────────────────────────────────────────────
    op.create_table(
        "dataset_registry",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("datahub_registered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── validation_configs ───────────────────────────────────────────────
    op.create_table(
        "validation_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("rules", JSONB, nullable=False),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── validation_results ───────────────────────────────────────────────
    op.create_table(
        "validation_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("partition", JSONB, nullable=False),
        sa.Column("values", JSONB, nullable=False),
        sa.Column("validation", JSONB, nullable=True),
        sa.Column("assertion_result", sa.Text(), nullable=False),
        sa.Column("issues", JSONB, nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("measured_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_validation_results_urn_measured",
        "validation_results",
        ["dataset_urn", sa.text("measured_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_validation_results_run_id",
        "validation_results",
        ["run_id"],
        schema=SCHEMA,
    )

    # ── metagen_configs ──────────────────────────────────────────────────
    op.create_table(
        "metagen_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("targets", JSONB, nullable=False),
        sa.Column("code_refs", JSONB, nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── metagen_results ──────────────────────────────────────────────────
    op.create_table(
        "metagen_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("proposals", JSONB, nullable=False),
        sa.Column("field_status", JSONB, nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("generated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_reviewed_at", TIMESTAMPTZ, nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metagen_results_urn_generated",
        "metagen_results",
        ["dataset_urn", sa.text("generated_at DESC")],
        schema=SCHEMA,
    )

    # ── metric_definitions ───────────────────────────────────────────────
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("measurement_query", JSONB, nullable=False),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── metric_results ───────────────────────────────────────────────────
    op.create_table(
        "metric_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "metric_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.metric_definitions.id"),
            nullable=False,
        ),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("breakdown", JSONB, nullable=True),
        sa.Column("measured_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metric_results_metric_measured",
        "metric_results",
        ["metric_id", sa.text("measured_at DESC")],
        schema=SCHEMA,
    )

    # ── events ───────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_events_entity_occurred",
        "events",
        ["entity_type", "entity_id", sa.text("occurred_at DESC")],
        schema=SCHEMA,
    )

    # ── department_mapping ───────────────────────────────────────────────
    op.create_table(
        "department_mapping",
        sa.Column("owner_urn", sa.Text(), primary_key=True),
        sa.Column("department", sa.Text(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── overview_config (singleton) ──────────────────────────────────────
    op.create_table(
        "overview_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("layout", sa.Text(), nullable=False, server_default="force"),
        sa.Column("color_by", sa.Text(), nullable=False, server_default="quality_score"),
        sa.Column("filters", JSONB, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_overview_config_singleton"),
        schema=SCHEMA,
    )

    # ── ontogen_config (singleton) ───────────────────────────────────────
    op.create_table(
        "ontogen_config",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            server_default="1",
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("dataset_filter", JSONB, nullable=False, server_default="'{}'::jsonb"),
        sa.Column(
            "max_manual_queries_per_dataset",
            sa.Integer(),
            nullable=False,
            server_default="20",
        ),
        sa.Column(
            "max_system_queries_per_dataset",
            sa.Integer(),
            nullable=False,
            server_default="10",
        ),
        sa.Column("default_run_prompt", sa.Text(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_ontogen_config_singleton"),
        sa.CheckConstraint(
            "max_manual_queries_per_dataset >= 0",
            name="ck_ontogen_config_max_manual_queries_gte0",
        ),
        sa.CheckConstraint(
            "max_system_queries_per_dataset >= 0",
            name="ck_ontogen_config_max_system_queries_gte0",
        ),
        schema=SCHEMA,
    )

    # ── ontogen_seeds ────────────────────────────────────────────────────
    op.create_table(
        "ontogen_seeds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── ontogen_nodes ────────────────────────────────────────────────────
    op.create_table(
        "ontogen_nodes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("glossary_term_urn", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name"),
        # __ is reserved as the triple-ID separator
        sa.CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_nodes_id_no_double_underscore",
        ),
        schema=SCHEMA,
    )

    # ── ontogen_edges ────────────────────────────────────────────────────
    op.create_table(
        "ontogen_edges",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("semantics", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("evidence", JSONB, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("label"),
        sa.CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_edges_id_no_double_underscore",
        ),
        schema=SCHEMA,
    )

    # ── ontogen_triples ──────────────────────────────────────────────────
    op.create_table(
        "ontogen_triples",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "subject_node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            nullable=False,
        ),
        sa.Column(
            "edge_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_edges.id"),
            nullable=False,
        ),
        sa.Column(
            "object_node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            nullable=False,
        ),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("evidence", JSONB, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "id = subject_node_id || '__' || edge_id || '__' || object_node_id",
            name="ck_ontogen_triples_id_composite",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ontogen_triples_subject",
        "ontogen_triples",
        ["subject_node_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ontogen_triples_object",
        "ontogen_triples",
        ["object_node_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ontogen_triples_edge",
        "ontogen_triples",
        ["edge_id"],
        schema=SCHEMA,
    )

    # ── dataset_node_map ─────────────────────────────────────────────────
    op.create_table(
        "dataset_node_map",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column(
            "node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            primary_key=True,
        ),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_node_map_node_id",
        "dataset_node_map",
        ["node_id"],
        schema=SCHEMA,
    )

    # ── dataset_embeddings (pgvector) ────────────────────────────────────
    # The ``vector(N)`` type is not a SQLAlchemy built-in; the embedding
    # column is added via raw DDL so we don't need a custom type adapter.
    # Application queries bind vectors with an explicit ``::vector`` cast.
    op.create_table(
        DATASET_EMBEDDINGS_TABLE,
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("owners", JSONB, nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("has_pii", sa.Boolean(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{DATASET_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{DATASET_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    # HNSW index with cosine distance ops — matches the ``<=>`` operator
    # used by ``PgVectorManager.search``.
    op.execute(
        f"CREATE INDEX {DATASET_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{DATASET_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── node_embeddings (pgvector) ───────────────────────────────────────
    op.create_table(
        NODE_EMBEDDINGS_TABLE,
        sa.Column(
            "node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{NODE_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{NODE_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    op.execute(
        f"CREATE INDEX {NODE_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{NODE_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── Apache AGE extension + graph ─────────────────────────────────────
    # age may already be installed via the initdb hook; CREATE EXTENSION
    # IF NOT EXISTS is idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS age")
    # LOAD is session-scoped; required before any ag_catalog calls.
    op.execute("LOAD 'age'")
    # Create graph — wrapped in PL/pgSQL exception block to be idempotent.
    op.execute(
        """
        DO $$ BEGIN
            PERFORM ag_catalog.create_graph('dataspoke_ontogen');
        EXCEPTION WHEN others THEN
            -- Ignore "graph already exists" and any duplicate_object error.
            IF SQLERRM LIKE '%already exists%' OR SQLSTATE = '42710' THEN
                NULL;
            ELSE
                RAISE;
            END IF;
        END $$
        """
    )
    # Grant ag_catalog usage to the current application role so service
    # queries work. Wrapped in a DO block so non-owner roles (where the
    # initdb hook already granted access) don't fail on insufficient_privilege.
    op.execute(
        """
        DO $$
        BEGIN
          GRANT USAGE ON SCHEMA ag_catalog TO CURRENT_USER;
        EXCEPTION WHEN insufficient_privilege THEN
          RAISE NOTICE 'ag_catalog GRANT skipped (insufficient privilege; assumed pre-granted by initdb hook)';
        END $$;
        """
    )


def downgrade() -> None:
    # Pre-release schema; no reverse path is provided.
    pass
