"""redesign ingestion_configs table

Revision ID: 004
Revises: 003
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: str = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # Drop old columns
    op.drop_column("ingestion_configs", "sources", schema=SCHEMA)
    op.drop_column("ingestion_configs", "deep_spec_enabled", schema=SCHEMA)
    op.drop_column("ingestion_configs", "owner", schema=SCHEMA)

    # Add new columns with defaults so the migration works on existing rows
    op.add_column(
        "ingestion_configs",
        sa.Column("source_type", sa.Text(), nullable=False, server_default="postgres"),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("location", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("periodic", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("enrichment_sources", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("custom_extractors", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )

    # Remove server defaults now that existing rows have been populated
    op.alter_column("ingestion_configs", "source_type", server_default=None, schema=SCHEMA)
    op.alter_column("ingestion_configs", "location", server_default=None, schema=SCHEMA)
    op.alter_column("ingestion_configs", "periodic", server_default=None, schema=SCHEMA)

    # Add CHECK constraint: when periodic=true, schedule must not be null
    op.create_check_constraint(
        "ck_ingestion_configs_periodic_schedule",
        "ingestion_configs",
        "NOT periodic OR schedule IS NOT NULL",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ingestion_configs_periodic_schedule",
        "ingestion_configs",
        type_="check",
        schema=SCHEMA,
    )

    op.drop_column("ingestion_configs", "custom_extractors", schema=SCHEMA)
    op.drop_column("ingestion_configs", "enrichment_sources", schema=SCHEMA)
    op.drop_column("ingestion_configs", "periodic", schema=SCHEMA)
    op.drop_column("ingestion_configs", "location", schema=SCHEMA)
    op.drop_column("ingestion_configs", "source_type", schema=SCHEMA)

    op.add_column(
        "ingestion_configs",
        sa.Column("owner", sa.Text(), nullable=False, server_default=""),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("deep_spec_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("sources", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )

    op.alter_column("ingestion_configs", "owner", server_default=None, schema=SCHEMA)
    op.alter_column("ingestion_configs", "deep_spec_enabled", server_default=None, schema=SCHEMA)
    op.alter_column("ingestion_configs", "sources", server_default=None, schema=SCHEMA)
