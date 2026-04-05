"""Simplify metrics domain: drop alarm columns, run_id from results, and metric_issues table.

Revision ID: 013
Revises: 012
Create Date: 2026-04-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013"
down_revision: str = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # ── Drop metric_issues table (with CASCADE) ──────────────────────────────

    op.drop_index("ix_metric_issues_status_priority", table_name="metric_issues", schema=SCHEMA)
    op.drop_index("ix_metric_issues_urn_status", table_name="metric_issues", schema=SCHEMA)
    op.drop_index("ix_metric_issues_metric_created", table_name="metric_issues", schema=SCHEMA)
    op.drop_table("metric_issues", schema=SCHEMA)

    # ── Drop alarm columns from metric_definitions ───────────────────────────

    op.drop_column("metric_definitions", "alarm_enabled", schema=SCHEMA)
    op.drop_column("metric_definitions", "alarm_threshold", schema=SCHEMA)
    op.drop_column("metric_definitions", "alarm_recipients", schema=SCHEMA)

    # ── Drop alarm_triggered and run_id from metric_results ──────────────────

    op.drop_column("metric_results", "alarm_triggered", schema=SCHEMA)
    op.drop_column("metric_results", "run_id", schema=SCHEMA)


def downgrade() -> None:
    # ── Re-add run_id and alarm_triggered to metric_results ──────────────────

    op.add_column(
        "metric_results",
        sa.Column("run_id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        schema=SCHEMA,
    )
    op.add_column(
        "metric_results",
        sa.Column("alarm_triggered", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=SCHEMA,
    )

    # ── Re-add alarm columns to metric_definitions ───────────────────────────

    op.add_column(
        "metric_definitions",
        sa.Column("alarm_recipients", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "metric_definitions",
        sa.Column("alarm_threshold", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "metric_definitions",
        sa.Column("alarm_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=SCHEMA,
    )

    # ── Re-create metric_issues table ────────────────────────────────────────

    op.create_table(
        "metric_issues",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("metric_id", sa.Text(), nullable=False),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("issue_type", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("assignee", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("estimated_fix_minutes", sa.Integer(), nullable=False),
        sa.Column("projected_score_impact", sa.REAL(), nullable=False),
        sa.Column("due_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"],
            [f"{SCHEMA}.metric_definitions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
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
        ["metric_id", sa.text("created_at DESC")],
        schema=SCHEMA,
    )
