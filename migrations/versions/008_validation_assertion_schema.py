"""Redesign validation tables for DataHub assertion layer.

Revision ID: 008
Revises: 007
Create Date: 2026-03-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008"
down_revision: str = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # ── validation_configs ────────────────────────────────────────────────
    # Drop sla_target column
    op.drop_column("validation_configs", "sla_target", schema=SCHEMA)

    # Change schedule from TEXT to JSONB, wrapping existing TEXT values
    # as {"cron": "<value>"} using a CASE expression
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.validation_configs
        ALTER COLUMN schedule TYPE jsonb
        USING CASE
            WHEN schedule IS NOT NULL THEN jsonb_build_object('cron', schedule)
            ELSE NULL
        END
        """
    )

    # ── validation_results ────────────────────────────────────────────────
    # Drop old quality-score-based columns
    op.drop_column("validation_results", "quality_score", schema=SCHEMA)
    op.drop_column("validation_results", "dimensions", schema=SCHEMA)
    op.drop_column("validation_results", "dimension_details", schema=SCHEMA)
    op.drop_column("validation_results", "anomalies", schema=SCHEMA)
    op.drop_column("validation_results", "recommendations", schema=SCHEMA)
    op.drop_column("validation_results", "alternatives", schema=SCHEMA)

    # Add new assertion-layer columns
    op.add_column(
        "validation_results",
        sa.Column("rule_id", sa.Text(), nullable=False, server_default=""),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("partition", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("values", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("validation", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("assertion_result", sa.Text(), nullable=False, server_default="ERROR"),
        schema=SCHEMA,
    )

    # Rename issues column — it already exists in the old schema; keep it.
    # (issues was already JSONB NOT NULL in the old schema)

    # Remove the server defaults we used only to satisfy NOT NULL during migration
    op.alter_column("validation_results", "rule_id", server_default=None, schema=SCHEMA)
    op.alter_column("validation_results", "partition", server_default=None, schema=SCHEMA)
    op.alter_column("validation_results", "values", server_default=None, schema=SCHEMA)
    op.alter_column("validation_results", "assertion_result", server_default=None, schema=SCHEMA)

    # Add index on run_id for grouping results by run
    op.create_index(
        "ix_validation_results_run_id",
        "validation_results",
        ["run_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Drop new index
    op.drop_index("ix_validation_results_run_id", table_name="validation_results", schema=SCHEMA)

    # Drop assertion-layer columns
    op.drop_column("validation_results", "assertion_result", schema=SCHEMA)
    op.drop_column("validation_results", "validation", schema=SCHEMA)
    op.drop_column("validation_results", "values", schema=SCHEMA)
    op.drop_column("validation_results", "partition", schema=SCHEMA)
    op.drop_column("validation_results", "rule_id", schema=SCHEMA)

    # Restore old quality-score-based columns
    op.add_column(
        "validation_results",
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("dimensions", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("dimension_details", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("anomalies", postgresql.JSONB(), nullable=False, server_default="[]"),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("recommendations", postgresql.JSONB(), nullable=False, server_default="[]"),
        schema=SCHEMA,
    )
    op.add_column(
        "validation_results",
        sa.Column("alternatives", postgresql.JSONB(), nullable=False, server_default="[]"),
        schema=SCHEMA,
    )

    # Remove server defaults
    for col in ("quality_score", "dimensions", "anomalies", "recommendations", "alternatives"):
        op.alter_column("validation_results", col, server_default=None, schema=SCHEMA)

    # Restore schedule as TEXT (JSONB → TEXT, extract cron value if present)
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.validation_configs
        ALTER COLUMN schedule TYPE text
        USING CASE
            WHEN schedule IS NOT NULL THEN schedule->>'cron'
            ELSE NULL
        END
        """
    )

    # Restore sla_target column
    op.add_column(
        "validation_configs",
        sa.Column("sla_target", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
