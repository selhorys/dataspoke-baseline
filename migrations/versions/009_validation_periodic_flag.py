"""Replace validation_configs.status with periodic boolean flag.

Revision ID: 009
Revises: 008
Create Date: 2026-03-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # Add periodic flag
    op.add_column(
        "validation_configs",
        sa.Column("periodic", sa.Boolean(), nullable=False, server_default="false"),
        schema=SCHEMA,
    )

    # Migrate: configs that were 'active' with a cron schedule become periodic=true
    op.execute(
        f"UPDATE {SCHEMA}.validation_configs"
        " SET periodic = TRUE"
        " WHERE status = 'active' AND schedule->>'cron' IS NOT NULL"
    )

    # Remove server default now that the data is populated
    op.alter_column("validation_configs", "periodic", server_default=None, schema=SCHEMA)

    # Drop the old status column
    op.drop_column("validation_configs", "status", schema=SCHEMA)


def downgrade() -> None:
    # Restore status column
    op.add_column(
        "validation_configs",
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        schema=SCHEMA,
    )

    # Migrate: periodic=true with cron schedule → status='active'
    op.execute(
        f"UPDATE {SCHEMA}.validation_configs"
        " SET status = 'active'"
        " WHERE periodic = TRUE AND schedule->>'cron' IS NOT NULL"
    )

    # Remove server default
    op.alter_column("validation_configs", "status", server_default=None, schema=SCHEMA)

    # Drop the periodic flag
    op.drop_column("validation_configs", "periodic", schema=SCHEMA)
