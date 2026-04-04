"""Add dataset_registry table with datahub_registered flag.

Revision ID: 010
Revises: 009
Create Date: 2026-04-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    op.create_table(
        "dataset_registry",
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column(
            "datahub_registered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("dataset_urn"),
        schema=SCHEMA,
    )

    # Backfill from existing configs.  Set datahub_registered = TRUE so
    # pre-existing configs remain functional after this migration — they
    # were usable before the gate was introduced.
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.dataset_registry (dataset_urn, datahub_registered)
        SELECT DISTINCT dataset_urn, TRUE
        FROM (
            SELECT dataset_urn FROM {SCHEMA}.ingestion_configs
            UNION
            SELECT dataset_urn FROM {SCHEMA}.validation_configs
        ) AS combined
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("dataset_registry", schema=SCHEMA)
