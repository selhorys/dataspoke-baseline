"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # ── ingestion_configs ────────────────────────────────────────────────
    op.create_table(
        "ingestion_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("deep_spec_enabled", sa.Boolean(), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── validation_configs ───────────────────────────────────────────────
    op.create_table(
        "validation_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("sla_target", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── validation_results ───────────────────────────────────────────────
    op.create_table(
        "validation_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("quality_score", sa.REAL(), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("anomalies", postgresql.JSONB(), nullable=False),
        sa.Column("recommendations", postgresql.JSONB(), nullable=False),
        sa.Column("alternatives", postgresql.JSONB(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "measured_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_validation_results_urn_measured",
        "validation_results",
        ["dataset_urn", "measured_at"],
        schema=SCHEMA,
    )

    # ── generation_configs ───────────────────────────────────────────────
    op.create_table(
        "generation_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("target_fields", postgresql.JSONB(), nullable=False),
        sa.Column("code_refs", postgresql.JSONB(), nullable=True),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # ── generation_results ───────────────────────────────────────────────
    op.create_table(
        "generation_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("proposals", postgresql.JSONB(), nullable=False),
        sa.Column("similar_diffs", postgresql.JSONB(), nullable=False),
        sa.Column("approval_status", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "generated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_generation_results_urn_generated",
        "generation_results",
        ["dataset_urn", "generated_at"],
        schema=SCHEMA,
    )

    # ── concept_categories ───────────────────────────────────────────────
    op.create_table(
        "concept_categories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.ForeignKeyConstraint(["parent_id"], [f"{SCHEMA}.concept_categories.id"]),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_concept_categories_parent",
        "concept_categories",
        ["parent_id"],
        schema=SCHEMA,
    )

    # ── dataset_concept_map ──────────────────────────────────────────────
    op.create_table(
        "dataset_concept_map",
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("concept_id", sa.UUID(), nullable=False),
        sa.Column("confidence_score", sa.REAL(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("dataset_urn", "concept_id"),
        sa.ForeignKeyConstraint(["concept_id"], [f"{SCHEMA}.concept_categories.id"]),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_concept_map_concept",
        "dataset_concept_map",
        ["concept_id"],
        schema=SCHEMA,
    )

    # ── concept_relationships ────────────────────────────────────────────
    op.create_table(
        "concept_relationships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("concept_a", sa.UUID(), nullable=False),
        sa.Column("concept_b", sa.UUID(), nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.REAL(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["concept_a"], [f"{SCHEMA}.concept_categories.id"]),
        sa.ForeignKeyConstraint(["concept_b"], [f"{SCHEMA}.concept_categories.id"]),
        schema=SCHEMA,
    )

    # ── metric_definitions ───────────────────────────────────────────────
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("measurement_query", postgresql.JSONB(), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("alarm_enabled", sa.Boolean(), nullable=False),
        sa.Column("alarm_threshold", postgresql.JSONB(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    # ── metric_results ───────────────────────────────────────────────────
    op.create_table(
        "metric_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("metric_id", sa.Text(), nullable=False),
        sa.Column("value", sa.REAL(), nullable=False),
        sa.Column("breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("alarm_triggered", sa.Boolean(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "measured_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["metric_id"], [f"{SCHEMA}.metric_definitions.id"]),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metric_results_metric_measured",
        "metric_results",
        ["metric_id", "measured_at"],
        schema=SCHEMA,
    )

    # ── metric_issues ────────────────────────────────────────────────────
    op.create_table(
        "metric_issues",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("metric_id", sa.Text(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("issue_type", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assignee", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("estimated_fix_minutes", sa.Integer(), nullable=False),
        sa.Column("projected_score_impact", sa.REAL(), nullable=False),
        sa.Column("due_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["metric_id"], [f"{SCHEMA}.metric_definitions.id"]),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metric_issues_status_priority",
        "metric_issues",
        ["status", "priority"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metric_issues_urn_status",
        "metric_issues",
        ["dataset_urn", "status"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metric_issues_metric_created",
        "metric_issues",
        ["metric_id", "created_at"],
        schema=SCHEMA,
    )

    # ── events ───────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_events_entity_occurred",
        "events",
        ["entity_type", "entity_id", "occurred_at"],
        schema=SCHEMA,
    )

    # ── department_mapping ───────────────────────────────────────────────
    op.create_table(
        "department_mapping",
        sa.Column("owner_urn", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("owner_urn"),
        schema=SCHEMA,
    )

    # ── overview_config ──────────────────────────────────────────────────
    op.create_table(
        "overview_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("layout", sa.Text(), nullable=False),
        sa.Column("color_by", sa.Text(), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in [
        "overview_config",
        "department_mapping",
        "events",
        "metric_issues",
        "metric_results",
        "metric_definitions",
        "concept_relationships",
        "dataset_concept_map",
        "concept_categories",
        "generation_results",
        "generation_configs",
        "validation_results",
        "validation_configs",
        "ingestion_configs",
    ]:
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
