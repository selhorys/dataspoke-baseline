"""Rename ingestion_configs.source_type to platform and normalise values to lowercase.

Revision ID: 014
Revises: 013
Create Date: 2026-04-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: str = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # Rename column source_type → platform
    op.alter_column(
        "ingestion_configs",
        "source_type",
        new_column_name="platform",
        schema=SCHEMA,
    )

    # Normalise legacy uppercase values to lowercase DataHub platform names
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.ingestion_configs
            SET platform = CASE
                WHEN platform = 'POSTGRESQL' THEN 'postgres'
                WHEN platform = 'MYSQL'      THEN 'mysql'
                WHEN platform = 'ORACLE'     THEN 'oracle'
                WHEN platform = 'BIGQUERY'   THEN 'bigquery'
                WHEN platform = 'SNOWFLAKE'  THEN 'snowflake'
                WHEN platform = 'KAFKA'      THEN 'kafka'
                ELSE LOWER(platform)
            END
            """
        )
    )


def downgrade() -> None:
    # Reverse data transform: lowercase → uppercase
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.ingestion_configs
            SET platform = CASE
                WHEN platform = 'postgres'  THEN 'POSTGRESQL'
                WHEN platform = 'mysql'     THEN 'MYSQL'
                WHEN platform = 'oracle'    THEN 'ORACLE'
                WHEN platform = 'bigquery'  THEN 'BIGQUERY'
                WHEN platform = 'snowflake' THEN 'SNOWFLAKE'
                WHEN platform = 'kafka'     THEN 'KAFKA'
                ELSE UPPER(platform)
            END
            """
        )
    )

    # Rename column platform → source_type
    op.alter_column(
        "ingestion_configs",
        "platform",
        new_column_name="source_type",
        schema=SCHEMA,
    )
