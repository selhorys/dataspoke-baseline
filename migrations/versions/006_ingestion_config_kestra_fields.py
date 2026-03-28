"""Add kestra_flow_namespace, kestra_flow_id to ingestion_configs; change status default to OK.

Revision ID: 006
Revises: 005
Create Date: 2026-03-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    op.add_column(
        "ingestion_configs",
        sa.Column("kestra_flow_namespace", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("kestra_flow_id", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    # Normalize all existing status values to "OK"
    op.execute(f"UPDATE {SCHEMA}.ingestion_configs SET status = 'OK'")
    op.alter_column(
        "ingestion_configs",
        "status",
        server_default="OK",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.alter_column(
        "ingestion_configs",
        "status",
        server_default="draft",
        schema=SCHEMA,
    )
    op.execute(f"UPDATE {SCHEMA}.ingestion_configs SET status = 'draft'")
    op.drop_column("ingestion_configs", "kestra_flow_id", schema=SCHEMA)
    op.drop_column("ingestion_configs", "kestra_flow_namespace", schema=SCHEMA)
