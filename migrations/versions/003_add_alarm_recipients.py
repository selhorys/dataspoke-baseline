"""add alarm_recipients to metric_definitions

Revision ID: 003
Revises: 002
Create Date: 2026-03-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: str = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    op.add_column(
        "metric_definitions",
        sa.Column("alarm_recipients", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("metric_definitions", "alarm_recipients", schema=SCHEMA)
