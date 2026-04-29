"""Spec rewrite — concept→triple model swap, generation→metagen rename,
is_active→is_enabled rename, AGE graph setup, ontogen tables.

Revision ID: 002
Revises: 001
Create Date: 2026-04-29

This migration is destructive by design (see plan file bubbly-stargazing-tome.md).
All concept-model tables are dropped outright; generation_* tables are drop-and-recreated
as metagen_* with updated column shapes. No data migration is attempted for dropped tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from src.shared.config import EMBEDDING_DIMENSION

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"
TIMESTAMPTZ = TIMESTAMP(timezone=True)
NODE_EMBEDDINGS_TABLE = "node_embeddings"
NODE_EMBEDDINGS_HNSW_INDEX = "node_embeddings_embedding_hnsw_idx"


def upgrade() -> None:
    # ── 1. Drop obsolete concept-model tables ────────────────────────────────
    # Drop in dependency order (children before parents).
    op.drop_table("concept_relationships", schema=SCHEMA)
    op.drop_table("dataset_concept_map", schema=SCHEMA)
    op.drop_table("concept_categories", schema=SCHEMA)

    # ── 2. Drop generation_* tables (will be recreated as metagen_*) ─────────
    op.drop_index(
        "ix_generation_results_urn_generated",
        table_name="generation_results",
        schema=SCHEMA,
    )
    op.drop_table("generation_results", schema=SCHEMA)
    op.drop_table("generation_configs", schema=SCHEMA)

    # ── 3. Rename is_active → is_enabled on existing config tables ───────────
    op.alter_column("ingestion_configs", "is_active", new_column_name="is_enabled", schema=SCHEMA)
    op.alter_column("validation_configs", "is_active", new_column_name="is_enabled", schema=SCHEMA)
    op.alter_column("metric_definitions", "is_active", new_column_name="is_enabled", schema=SCHEMA)

    # ── 4. Create metagen_configs ─────────────────────────────────────────────
    op.create_table(
        "metagen_configs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
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

    # ── 5. Create metagen_results ─────────────────────────────────────────────
    op.create_table(
        "metagen_results",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("proposals", JSONB, nullable=False),
        sa.Column("field_status", JSONB, nullable=False),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
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

    # ── 6. Create ontogen_config (singleton) ──────────────────────────────────
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

    # ── 7. Create ontogen_seeds ───────────────────────────────────────────────
    op.create_table(
        "ontogen_seeds",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── 8. Create ontogen_nodes ───────────────────────────────────────────────
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
        # Guard: __ is forbidden in IDs (reserved as triple-ID separator)
        sa.CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_nodes_id_no_double_underscore",
        ),
        schema=SCHEMA,
    )

    # ── 9. Create ontogen_edges ───────────────────────────────────────────────
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

    # ── 10. Create ontogen_triples ────────────────────────────────────────────
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
        # Enforce composite-slug ID convention
        sa.CheckConstraint(
            "id = subject_node_id || '__' || edge_id || '__' || object_node_id",
            name="ck_ontogen_triples_id_composite",
        ),
        schema=SCHEMA,
    )
    # Indexes per BACKEND_SCHEMA §Indexes
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

    # ── 11. Create dataset_node_map ───────────────────────────────────────────
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

    # ── 12. Create node_embeddings (pgvector) ─────────────────────────────────
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
    # Add embedding column via raw DDL — vector type is non-standard SQLAlchemy type.
    # Pattern mirrors dataset_embeddings in migration 001.
    op.execute(
        f"ALTER TABLE {SCHEMA}.{NODE_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{NODE_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    # HNSW index with cosine distance — mirrors dataset_embeddings_embedding_hnsw_idx
    op.execute(
        f"CREATE INDEX {NODE_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{NODE_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── 13. AGE extension + graph ─────────────────────────────────────────────
    # age extension may already be installed via initdb hook; CREATE EXTENSION IF NOT EXISTS
    # is idempotent and safe to run again.
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
    # Grant ag_catalog usage to the current application role so service queries work.
    # Wrapped in a DO block so that migrations run by a non-owner role (e.g. in
    # a restricted cloud environment where ag_catalog is pre-granted by the
    # initdb hook) do not fail on insufficient_privilege.
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

    # ── 14. Add ingestion_configs.mode column (if not present from 001) ───────
    # BACKEND_SCHEMA requires a `mode` column (active|passive).
    # 001 did not include it, so we add it here.
    op.add_column(
        "ingestion_configs",
        sa.Column("mode", sa.Text(), nullable=False, server_default="active"),
        schema=SCHEMA,
    )

    # ── 15. Drop stale ingestion_configs columns not in BACKEND_SCHEMA ────────
    # enrichment_sources and custom_extractors were generated in a previous
    # draft pass; they are absent from spec/feature/BACKEND_SCHEMA.md.
    op.drop_column("ingestion_configs", "enrichment_sources", schema=SCHEMA)
    op.drop_column("ingestion_configs", "custom_extractors", schema=SCHEMA)

    # ── 16. Singleton CHECK on overview_config.id ─────────────────────────────
    # Mirrors the ck_ontogen_config_singleton constraint on ontogen_config.
    op.create_check_constraint(
        "ck_overview_config_singleton",
        "overview_config",
        "id = 1",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Migration is destructive by design (see plan file bubbly-stargazing-tome.md); no downgrade.
    pass
