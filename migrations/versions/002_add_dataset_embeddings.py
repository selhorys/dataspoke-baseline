"""Add dataspoke.dataset_embeddings table with pgvector HNSW index.

Migrates vector storage from Qdrant to PostgreSQL + pgvector.  The
``CREATE EXTENSION IF NOT EXISTS vector`` guard is idempotent — safe to
run even when the initdb script already created the extension.

Revision ID: 002
Revises: 001
Create Date: 2026-04-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# Import the compile-time constant so the DDL stays in sync with the
# application layer without hard-coding the dimension twice.
from src.shared.config import EMBEDDING_DIMENSION

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"
TIMESTAMPTZ = TIMESTAMP(timezone=True)
TABLE = "dataset_embeddings"
HNSW_INDEX = "dataset_embeddings_embedding_hnsw_idx"


def upgrade() -> None:
    # Enable the pgvector extension — idempotent, does nothing if already present.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        TABLE,
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        # The pgvector ``vector(N)`` type is not a SQLAlchemy built-in; we use
        # a generic ``Text`` column at the DDL layer and rely on the raw SQL
        # cast (``::vector``) in application queries.  The HNSW index created
        # below requires the column to be typed ``vector(N)`` — we use
        # ``op.execute`` for that.
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("owners", JSONB, nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("has_pii", sa.Boolean(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # Add the embedding column using raw DDL so Alembic does not need a
    # SQLAlchemy type mapping for ``vector(N)``.
    op.execute(
        f"ALTER TABLE {SCHEMA}.{TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )

    # Drop the server default now that all existing rows (none yet) have been
    # populated — production writes always supply the embedding explicitly.
    op.execute(
        f"ALTER TABLE {SCHEMA}.{TABLE} "
        f"ALTER COLUMN embedding DROP DEFAULT"
    )

    # HNSW index using cosine distance operator class.
    # ``vector_cosine_ops`` matches the ``<=>`` operator used in application
    # queries.  HNSW gives approximate nearest-neighbour search with O(log N)
    # lookup at the cost of slightly higher build time.
    op.execute(
        f"CREATE INDEX {HNSW_INDEX} "
        f"ON {SCHEMA}.{TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    # Drop index explicitly before table (PostgreSQL drops it automatically
    # with the table, but being explicit is safer in partial-failure scenarios).
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.{HNSW_INDEX}")
    op.drop_table(TABLE, schema=SCHEMA)
    # The vector extension is intentionally NOT dropped — it may be shared by
    # other tables or future migrations.
