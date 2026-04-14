"""Initial schema — all DataSpoke tables in the ``dataspoke`` schema.

Reflects the current ORM models in ``src/shared/db/models.py``.

Revision ID: 001
Revises: None
Create Date: 2026-04-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"
TIMESTAMPTZ = TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    # ── ingestion_configs ────────────────────────────────────────────────
    op.create_table(
        "ingestion_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("locator", JSONB, nullable=False),
        sa.Column("identifier", JSONB, nullable=False),
        sa.Column("auth", JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("enrichment_sources", JSONB, nullable=True),
        sa.Column("custom_extractors", JSONB, nullable=True),
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
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
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

    # ── generation_configs ───────────────────────────────────────────────
    op.create_table(
        "generation_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("target_fields", JSONB, nullable=False),
        sa.Column("code_refs", JSONB, nullable=True),
        sa.Column("schedule_cron", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── generation_results ───────────────────────────────────────────────
    op.create_table(
        "generation_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("proposals", JSONB, nullable=False),
        sa.Column("similar_diffs", JSONB, nullable=False),
        sa.Column("approval_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("generated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", TIMESTAMPTZ, nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_generation_results_urn_generated",
        "generation_results",
        ["dataset_urn", sa.text("generated_at DESC")],
        schema=SCHEMA,
    )

    # ── concept_categories ───────────────────────────────────────────────
    op.create_table(
        "concept_categories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "parent_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.concept_categories.id"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_concept_categories_parent", "concept_categories", ["parent_id"], schema=SCHEMA
    )

    # ── dataset_concept_map ──────────────────────────────────────────────
    op.create_table(
        "dataset_concept_map",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column(
            "concept_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.concept_categories.id"),
            primary_key=True,
        ),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_concept_map_concept", "dataset_concept_map", ["concept_id"], schema=SCHEMA
    )

    # ── concept_relationships ────────────────────────────────────────────
    op.create_table(
        "concept_relationships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "concept_a",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.concept_categories.id"),
            nullable=False,
        ),
        sa.Column(
            "concept_b",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.concept_categories.id"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
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
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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

    # ── overview_config ──────────────────────────────────────────────────
    op.create_table(
        "overview_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("layout", sa.Text(), nullable=False, server_default="force"),
        sa.Column("color_by", sa.Text(), nullable=False, server_default="quality_score"),
        sa.Column("filters", JSONB, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in [
        "overview_config",
        "department_mapping",
        "events",
        "metric_results",
        "metric_definitions",
        "concept_relationships",
        "dataset_concept_map",
        "concept_categories",
        "generation_results",
        "generation_configs",
        "validation_results",
        "validation_configs",
        "dataset_registry",
        "ingestion_configs",
    ]:
        op.drop_table(table, schema=SCHEMA)
    op.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA}"))
