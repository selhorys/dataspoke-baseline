"""refactor ingestion_configs: location -> locator/identifier/auth, source_type to CAPS

Revision ID: 005
Revises: 004
Create Date: 2026-03-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"


def upgrade() -> None:
    # 1. Add new columns with server defaults
    op.add_column(
        "ingestion_configs",
        sa.Column("locator", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("identifier", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.add_column(
        "ingestion_configs",
        sa.Column("auth", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )

    # 2. Migrate data: split location into locator/identifier/auth per source_type,
    #    and map source_type to CAPITALIZED values.
    op.execute(sa.text(f"""
        UPDATE {SCHEMA}.ingestion_configs SET
            locator = CASE
                WHEN source_type IN ('postgres', 'mysql', 'oracle') THEN
                    jsonb_build_object(
                        'host', COALESCE(location->>'host', ''),
                        'port', COALESCE((location->>'port')::int, 0)
                    )
                WHEN source_type = 'kafka' THEN
                    jsonb_build_object(
                        'bootstrap_servers', COALESCE(location->>'bootstrap_servers', '')
                    )
                WHEN source_type = 'bigquery' THEN
                    jsonb_build_object(
                        'project_id', COALESCE(location->>'project_id', '')
                    )
                WHEN source_type = 'snowflake' THEN
                    jsonb_build_object(
                        'account_id', COALESCE(location->>'account_id', '')
                    )
                ELSE '{{}}'
            END,
            identifier = CASE
                WHEN source_type IN ('postgres', 'mysql', 'oracle') THEN
                    jsonb_build_object(
                        'database', COALESCE(location->>'database', '')
                    )
                ELSE '{{}}'
            END,
            auth = CASE
                WHEN source_type IN ('postgres', 'mysql', 'oracle', 'snowflake') THEN
                    jsonb_build_object(
                        'username', COALESCE(location->>'username', ''),
                        'secret_ref', COALESCE(location->>'secret_ref', '')
                    )
                ELSE NULL
            END,
            source_type = CASE
                WHEN source_type = 'postgres' THEN 'POSTGRESQL'
                WHEN source_type = 'mysql' THEN 'MYSQL'
                WHEN source_type = 'oracle' THEN 'ORACLE'
                WHEN source_type = 'bigquery' THEN 'BIGQUERY'
                WHEN source_type = 'snowflake' THEN 'SNOWFLAKE'
                WHEN source_type = 'kafka' THEN 'KAFKA'
                ELSE UPPER(source_type)
            END
    """))

    # 3. Remove server defaults
    op.alter_column("ingestion_configs", "locator", server_default=None, schema=SCHEMA)
    op.alter_column("ingestion_configs", "identifier", server_default=None, schema=SCHEMA)

    # 4. Drop old column
    op.drop_column("ingestion_configs", "location", schema=SCHEMA)


def downgrade() -> None:
    # 1. Re-add location column
    op.add_column(
        "ingestion_configs",
        sa.Column("location", postgresql.JSONB(), nullable=False, server_default="{}"),
        schema=SCHEMA,
    )

    # 2. Merge locator/identifier/auth back into location, revert source_type
    op.execute(sa.text(f"""
        UPDATE {SCHEMA}.ingestion_configs SET
            location = COALESCE(locator, '{{}}'::jsonb)
                || COALESCE(identifier, '{{}}'::jsonb)
                || COALESCE(auth, '{{}}'::jsonb),
            source_type = CASE
                WHEN source_type = 'POSTGRESQL' THEN 'postgres'
                WHEN source_type = 'MYSQL' THEN 'mysql'
                WHEN source_type = 'ORACLE' THEN 'oracle'
                WHEN source_type = 'BIGQUERY' THEN 'bigquery'
                WHEN source_type = 'SNOWFLAKE' THEN 'snowflake'
                WHEN source_type = 'KAFKA' THEN 'kafka'
                ELSE LOWER(source_type)
            END
    """))

    # 3. Remove server default
    op.alter_column("ingestion_configs", "location", server_default=None, schema=SCHEMA)

    # 4. Drop new columns
    op.drop_column("ingestion_configs", "auth", schema=SCHEMA)
    op.drop_column("ingestion_configs", "identifier", schema=SCHEMA)
    op.drop_column("ingestion_configs", "locator", schema=SCHEMA)
