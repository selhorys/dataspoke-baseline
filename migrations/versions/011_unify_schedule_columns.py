"""Unify periodic/schedule → is_active/schedule_cron across ingestion and validation configs.

Revision ID: 011
Revises: 010
Create Date: 2026-04-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "011"
down_revision: str = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # ── ingestion_configs (pure rename) ──────────────────────────────────────

    op.drop_constraint(
        "ck_ingestion_configs_periodic_schedule",
        "ingestion_configs",
        type_="check",
        schema=SCHEMA,
    )
    op.alter_column(
        "ingestion_configs",
        "periodic",
        new_column_name="is_active",
        schema=SCHEMA,
    )
    op.alter_column(
        "ingestion_configs",
        "schedule",
        new_column_name="schedule_cron",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_ingestion_configs_is_active_schedule_cron",
        "ingestion_configs",
        "NOT is_active OR schedule_cron IS NOT NULL",
        schema=SCHEMA,
    )

    # ── validation_configs (flatten JSONB → TEXT + rename) ───────────────────

    op.add_column(
        "validation_configs",
        sa.Column("schedule_cron", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.validation_configs"
        " SET schedule_cron = schedule->>'cron'"
        " WHERE schedule IS NOT NULL"
    )
    op.alter_column(
        "validation_configs",
        "periodic",
        new_column_name="is_active",
        schema=SCHEMA,
    )
    op.drop_column("validation_configs", "schedule", schema=SCHEMA)
    op.create_check_constraint(
        "ck_validation_configs_is_active_schedule_cron",
        "validation_configs",
        "NOT is_active OR schedule_cron IS NOT NULL",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # ── ingestion_configs (reverse) ──────────────────────────────────────────

    op.drop_constraint(
        "ck_ingestion_configs_is_active_schedule_cron",
        "ingestion_configs",
        type_="check",
        schema=SCHEMA,
    )
    op.alter_column(
        "ingestion_configs",
        "is_active",
        new_column_name="periodic",
        schema=SCHEMA,
    )
    op.alter_column(
        "ingestion_configs",
        "schedule_cron",
        new_column_name="schedule",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_ingestion_configs_periodic_schedule",
        "ingestion_configs",
        "NOT periodic OR schedule IS NOT NULL",
        schema=SCHEMA,
    )

    # ── validation_configs (reverse) ─────────────────────────────────────────

    op.drop_constraint(
        "ck_validation_configs_is_active_schedule_cron",
        "validation_configs",
        type_="check",
        schema=SCHEMA,
    )
    op.add_column(
        "validation_configs",
        sa.Column("schedule", JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.validation_configs"
        " SET schedule = jsonb_build_object('cron', schedule_cron)"
        " WHERE schedule_cron IS NOT NULL"
    )
    op.alter_column(
        "validation_configs",
        "is_active",
        new_column_name="periodic",
        schema=SCHEMA,
    )
    op.drop_column("validation_configs", "schedule_cron", schema=SCHEMA)
