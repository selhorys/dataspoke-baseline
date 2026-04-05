"""Unify generation/metrics field names to match ingestion/validation golden standard.

Revision ID: 012
Revises: 011
Create Date: 2026-04-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "012"
down_revision: str = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # ── generation_configs (rename schedule → schedule_cron) ─────────────────

    op.alter_column(
        "generation_configs",
        "schedule",
        new_column_name="schedule_cron",
        schema=SCHEMA,
    )

    # ── metric_definitions (rename schedule → schedule_cron, active → is_active)

    op.alter_column(
        "metric_definitions",
        "schedule",
        new_column_name="schedule_cron",
        schema=SCHEMA,
    )
    op.alter_column(
        "metric_definitions",
        "active",
        new_column_name="is_active",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # ── metric_definitions (reverse) ─────────────────────────────────────────

    op.alter_column(
        "metric_definitions",
        "is_active",
        new_column_name="active",
        schema=SCHEMA,
    )
    op.alter_column(
        "metric_definitions",
        "schedule_cron",
        new_column_name="schedule",
        schema=SCHEMA,
    )

    # ── generation_configs (reverse) ─────────────────────────────────────────

    op.alter_column(
        "generation_configs",
        "schedule_cron",
        new_column_name="schedule",
        schema=SCHEMA,
    )
