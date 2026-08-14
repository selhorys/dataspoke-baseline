"""Initial schema — all DataSpoke tables in the ``dataspoke`` schema.

Creates the full operational schema: 19 ORM-backed tables, the
``dataset_embeddings`` pgvector table used by the search/reindex pipeline,
the pgvector ``vector`` extension, and the Apache AGE ``dataspoke_ontogen``
graph used to materialise ``ontogen_triples`` for graph traversal.

Revision ID: 001
Revises: None
Create Date: 2026-04-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TIMESTAMP, UUID

from src.shared.config import EMBEDDING_DIMENSION

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"
TIMESTAMPTZ = TIMESTAMP(timezone=True)
DATASET_EMBEDDINGS_TABLE = "dataset_embeddings"
DATASET_EMBEDDINGS_HNSW_INDEX = "dataset_embeddings_embedding_hnsw_idx"
NODE_EMBEDDINGS_TABLE = "node_embeddings"
NODE_EMBEDDINGS_HNSW_INDEX = "node_embeddings_embedding_hnsw_idx"
EDGE_EMBEDDINGS_TABLE = "edge_embeddings"
EDGE_EMBEDDINGS_HNSW_INDEX = "edge_embeddings_embedding_hnsw_idx"
TRIPLE_EMBEDDINGS_TABLE = "triple_embeddings"
TRIPLE_EMBEDDINGS_HNSW_INDEX = "triple_embeddings_embedding_hnsw_idx"


def upgrade() -> None:
    # Concurrent runs are serialized one level up, in ``migrations/env.py``: this
    # body is not race-safe on its own (an ``IF NOT EXISTS`` existence check
    # precedes name reservation, so two runs collide on
    # ``pg_class_relname_nsp_index``), and a lock taken here would come too late —
    # Alembic creates and reads ``alembic_version`` before it calls ``upgrade()``.
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    # pgvector extension — idempotent. The Bitnami initdb hook also creates
    # this at cluster bootstrap; the guard here keeps the migration safe
    # against a DB provisioned without that hook.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # citext extension — case-insensitive text, used by users.email.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # ── users ────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", CITEXT, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("google_sub", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="Reader"),
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("google_sub", name="uq_users_google_sub"),
        sa.CheckConstraint(
            "password_hash IS NOT NULL OR google_sub IS NOT NULL",
            name="ck_users_auth_method",
        ),
        sa.CheckConstraint(
            "role IN ('Admin', 'Editor', 'Reader')",
            name="ck_users_role",
        ),
        schema=SCHEMA,
    )

    # ── api_tokens ───────────────────────────────────────────────────────
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("role_snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", TIMESTAMPTZ, nullable=True),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=True),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        sa.CheckConstraint(
            "role_snapshot IN ('Admin', 'Editor', 'Reader')",
            name="ck_api_tokens_role_snapshot",
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"CREATE INDEX ix_api_tokens_user_active "
        f"ON {SCHEMA}.api_tokens (user_id) WHERE revoked_at IS NULL"
    )

    # ── password_reset_tokens ─────────────────────────────────────────────
    op.create_table(
        "password_reset_tokens",
        sa.Column("token_hash", sa.CHAR(64), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("used_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_password_reset_tokens_user_expires",
        "password_reset_tokens",
        ["user_id", sa.text("expires_at DESC")],
        schema=SCHEMA,
    )

    # ── ingestion_source ─────────────────────────────────────────────────
    op.create_table(
        "ingestion_source",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("recipe", JSONB, nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("datahub_source_urn", sa.Text(), nullable=True),
        sa.Column(
            "parent_source_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.ingestion_source.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="OK"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ingestion_source_parent",
        "ingestion_source",
        ["parent_source_id"],
        schema=SCHEMA,
    )

    # ── ingestion_source_dataset ─────────────────────────────────────────
    op.create_table(
        "ingestion_source_dataset",
        sa.Column(
            "source_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.ingestion_source.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("derivation", sa.Text(), nullable=False),
        sa.Column("first_seen_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── dataset_registry ─────────────────────────────────────────────────
    op.create_table(
        "dataset_registry",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("datahub_registered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("platform_urn", sa.Text(), nullable=True),
        sa.Column(
            "tag_urns",
            PG_ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "glossary_term_urns",
            PG_ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("attrs_synced_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    # GIN serves the `dataset_filter` array predicates (`col @> ARRAY[…]`);
    # the btree pair serves the scalar equality / IN predicates.
    op.create_index(
        "ix_dataset_registry_tag_urns",
        "dataset_registry",
        ["tag_urns"],
        postgresql_using="gin",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_registry_glossary_term_urns",
        "dataset_registry",
        ["glossary_term_urns"],
        postgresql_using="gin",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_registry_origin",
        "dataset_registry",
        ["origin"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_registry_platform_urn",
        "dataset_registry",
        ["platform_urn"],
        schema=SCHEMA,
    )
    # Partial: the column is true registry-wide by default, so only the false
    # side is selective enough for the planner to use an index.
    op.create_index(
        "ix_dataset_registry_not_primary",
        "dataset_registry",
        ["is_primary"],
        postgresql_where=sa.text("NOT is_primary"),
        schema=SCHEMA,
    )

    # ── validation_configs ───────────────────────────────────────────────
    op.create_table(
        "validation_configs",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("variables", JSONB, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "jsonb_array_length(variables) BETWEEN 1 AND 200",
            name="ck_validation_configs_variables_length",
        ),
        schema=SCHEMA,
    )

    # ── validation_results ───────────────────────────────────────────────
    op.create_table(
        "validation_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column("data_time", TIMESTAMPTZ, nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("variables", JSONB, nullable=False),
        sa.Column("ingestion_time", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "score BETWEEN 0.0 AND 1.0",
            name="ck_validation_results_score_range",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_validation_results_urn_data_time",
        "validation_results",
        ["dataset_urn", sa.text("data_time DESC")],
        schema=SCHEMA,
    )

    # ── metagen_config (collection) ──────────────────────────────────────
    op.create_table(
        "metagen_config",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("dataset_filter", sa.Text(), nullable=False, server_default=""),
        sa.Column("result_limit", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("overwrite_pending", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_metagen_config_name"),
        sa.CheckConstraint(
            "result_limit BETWEEN 1 AND 20", name="ck_metagen_config_result_limit"
        ),
        schema=SCHEMA,
    )

    # ── metagen_boundary ─────────────────────────────────────────────────
    op.create_table(
        "metagen_boundary",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("allowed", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── metagen_items ────────────────────────────────────────────────────
    op.create_table(
        "metagen_items",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('dataset.description', 'column.description')",
            name="ck_metagen_items_kind",
        ),
        schema=SCHEMA,
    )

    # ── metagen_candidates ───────────────────────────────────────────────
    op.create_table(
        "metagen_candidates",
        sa.Column("candidate_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conf_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.metagen_config.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dataset_urn", sa.Text(), nullable=False),
        sa.Column(
            "item_id",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_urn", "item_id"],
            [f"{SCHEMA}.metagen_items.dataset_urn", f"{SCHEMA}.metagen_items.item_id"],
        ),
        sa.CheckConstraint(
            "status IN ('llm_approved', 'approved', 'rejected')",
            name="ck_metagen_candidates_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metagen_candidates_item_status_created",
        "metagen_candidates",
        ["conf_id", "dataset_urn", "item_id", "status", sa.text("created_at")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metagen_candidates_run_id",
        "metagen_candidates",
        ["run_id"],
        schema=SCHEMA,
    )
    op.execute(
        f"CREATE UNIQUE INDEX ix_metagen_candidates_one_approved "
        f"ON {SCHEMA}.metagen_candidates (dataset_urn, item_id) "
        f"WHERE status = 'approved'"
    )

    # ── metagen_candidate_embeddings (pgvector) ──────────────────────────
    METAGEN_CANDIDATE_EMBEDDINGS_TABLE = "metagen_candidate_embeddings"
    METAGEN_CANDIDATE_EMBEDDINGS_HNSW_INDEX = "metagen_candidate_embeddings_embedding_hnsw_idx"

    op.create_table(
        METAGEN_CANDIDATE_EMBEDDINGS_TABLE,
        sa.Column(
            "candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.metagen_candidates.candidate_id"),
            primary_key=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{METAGEN_CANDIDATE_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{METAGEN_CANDIDATE_EMBEDDINGS_TABLE} "
        f"ALTER COLUMN embedding DROP DEFAULT"
    )
    op.execute(
        f"CREATE INDEX {METAGEN_CANDIDATE_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{METAGEN_CANDIDATE_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── metric_definitions ───────────────────────────────────────────────
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("metric_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metric_conf", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dataset_filter", sa.Text(), nullable=False, server_default=""),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── metric_results ───────────────────────────────────────────────────
    op.create_table(
        "metric_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "metric_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.metric_definitions.id"),
            nullable=False,
        ),
        sa.Column("values", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("breakdown", JSONB, nullable=True),
        sa.Column("measured_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_metric_results_metric_measured",
        "metric_results",
        ["metric_id", sa.text("measured_at DESC")],
        schema=SCHEMA,
    )

    # ── metric_dataset_results ───────────────────────────────────────────
    op.create_table(
        "metric_dataset_results",
        sa.Column(
            "metric_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.metric_definitions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("met", sa.Boolean(), nullable=False),
        sa.Column("evidence_at", TIMESTAMPTZ, nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("measured_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── events ───────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_events_entity_occurred",
        "events",
        ["entity_type", "entity_id", sa.text("occurred_at DESC")],
        schema=SCHEMA,
    )
    # Ingestion event indexes — both partial on entity_type='ingestion_source',
    # both shaped by the two-branch predicates their queries emit. Rationale and
    # shape constraints: spec/feature/BACKEND_SCHEMA.md §Ingestion event indexes.
    op.execute(
        f"CREATE INDEX ix_events_ingestion_dataset_urn "
        f"ON {SCHEMA}.events (entity_id, (detail->>'dataset_urn'), occurred_at DESC) "
        f"WHERE entity_type = 'ingestion_source'"
    )
    op.execute(
        f"CREATE INDEX ix_events_ingestion_run_level "
        f"ON {SCHEMA}.events (entity_id, occurred_at DESC) "
        f"WHERE entity_type = 'ingestion_source' "
        f"AND event_type IN ('INGESTION.COMPLETE', 'INGESTION.FAIL') "
        f"AND (detail->>'source' IS NULL "
        f"OR detail->>'source' NOT IN "
        f"('last_ingested_observation', 'passive_observation'))"
    )
    # Extended statistics on the producer discriminator. Without it the planner has
    # no distribution for detail->>'source', over-estimates the run-level index's
    # matching rows by orders of magnitude, and under ORDER BY occurred_at DESC
    # LIMIT 1 falls back to ix_events_entity_occurred — precisely on the sources
    # the partial index exists for, whose feed is dominated by observations. It has
    # no declarative table-metadata form, so it exists only here: schemas built
    # straight from the ORM metadata lack it, which is acceptable for a planner hint
    # that no behaviour depends on.
    op.execute(
        f"CREATE STATISTICS {SCHEMA}.st_events_detail_source "
        f"ON (detail->>'source') FROM {SCHEMA}.events"
    )

    # ── ontogen_config (singleton) ───────────────────────────────────────
    op.create_table(
        "ontogen_config",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            server_default="1",
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_tier", sa.Text(), nullable=True),
        sa.Column("dataset_filter", sa.Text(), nullable=False, server_default=""),
        sa.Column("default_run_prompt", sa.Text(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_ontogen_config_singleton"),
        schema=SCHEMA,
    )

    # ── runtime_config (singleton) ───────────────────────────────────────
    op.create_table(
        "runtime_config",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            server_default="1",
        ),
        sa.Column("llm_provider", sa.Text(), nullable=False, server_default="gemini"),
        sa.Column("llm_model", sa.Text(), nullable=False, server_default="gemini-3.5-flash"),
        sa.Column("ontogen_llm_max_iterations", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("ontogen_debate_max_turns", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("ontogen_debate_rag_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("ontogen_debate_reviewer_model", sa.Text(), nullable=True),
        sa.Column("metagen_llm_max_iterations", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("metagen_debate_max_turns", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("metagen_debate_rag_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("metagen_debate_reviewer_model", sa.Text(), nullable=True),
        sa.Column(
            "metagen_confidence_threshold", sa.Float(), nullable=False, server_default="0.7"
        ),
        sa.Column("metagen_ontology_rag_node_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("metagen_ontology_rag_edge_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "metagen_ontology_rag_triple_k", sa.Integer(), nullable=False, server_default="5"
        ),
        sa.Column(
            "stub_redis_client", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "stub_llm_client", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "stub_pgvector_manager", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "stub_notification_service", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "auth_datahub_corp_group",
            sa.Text(),
            nullable=False,
            server_default="dataspoke-users",
        ),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_runtime_config_singleton"),
        schema=SCHEMA,
    )

    # ── peripheral_config ────────────────────────────────────────────────
    op.create_table(
        "peripheral_config",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "name IN ('datahub', 'langfuse', 'smtp')", name="ck_peripheral_config_name"
        ),
        schema=SCHEMA,
    )

    # ── peripheral_health ────────────────────────────────────────────────
    # Keyed per transport, not per peripheral product: DataHub's event stream
    # ('datahub') and its GMS metadata API ('datahub-api') fail independently and
    # have separate reporters. Deliberately without a foreign key to
    # peripheral_config: a missing config row is the condition this table reports,
    # so the constraint would suppress the very signal it carries.
    op.create_table(
        "peripheral_health",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_ok_at", TIMESTAMPTZ, nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "name IN ('datahub', 'datahub-api', 'langfuse', 'smtp')",
            name="ck_peripheral_health_name",
        ),
        sa.CheckConstraint(
            "status IN ('unknown', 'ok', 'error')", name="ck_peripheral_health_status"
        ),
        schema=SCHEMA,
    )

    # ── ontogen_seeds ────────────────────────────────────────────────────
    op.create_table(
        "ontogen_seeds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    # ── ontogen_nodes ────────────────────────────────────────────────────
    op.create_table(
        "ontogen_nodes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name"),
        # __ is reserved as the triple-ID separator
        sa.CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_nodes_id_no_double_underscore",
        ),
        schema=SCHEMA,
    )

    # ── ontogen_edges ────────────────────────────────────────────────────
    op.create_table(
        "ontogen_edges",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("semantics", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("label"),
        sa.CheckConstraint(
            "position('__' in id) = 0",
            name="ck_ontogen_edges_id_no_double_underscore",
        ),
        schema=SCHEMA,
    )

    # ── ontogen_triples ──────────────────────────────────────────────────
    op.create_table(
        "ontogen_triples",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "subject_node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            nullable=False,
        ),
        sa.Column(
            "edge_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_edges.id"),
            nullable=False,
        ),
        sa.Column(
            "object_node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            nullable=False,
        ),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "id = subject_node_id || '__' || edge_id || '__' || object_node_id",
            name="ck_ontogen_triples_id_composite",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ontogen_triples_subject",
        "ontogen_triples",
        ["subject_node_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ontogen_triples_object",
        "ontogen_triples",
        ["object_node_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ontogen_triples_edge",
        "ontogen_triples",
        ["edge_id"],
        schema=SCHEMA,
    )

    # ── dataset_node_map ─────────────────────────────────────────────────
    op.create_table(
        "dataset_node_map",
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column(
            "node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            primary_key=True,
        ),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_dataset_node_map_node_id",
        "dataset_node_map",
        ["node_id"],
        schema=SCHEMA,
    )

    # ── dataset_embeddings (pgvector) ────────────────────────────────────
    # The ``vector(N)`` type is not a SQLAlchemy built-in; the embedding
    # column is added via raw DDL so we don't need a custom type adapter.
    # Application queries bind vectors with an explicit ``::vector`` cast.
    op.create_table(
        DATASET_EMBEDDINGS_TABLE,
        sa.Column("dataset_urn", sa.Text(), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("owners", JSONB, nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("has_pii", sa.Boolean(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{DATASET_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{DATASET_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    # HNSW index with cosine distance ops — matches the ``<=>`` operator
    # used by ``PgVectorManager.search``.
    op.execute(
        f"CREATE INDEX {DATASET_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{DATASET_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── node_embeddings (pgvector) ───────────────────────────────────────
    op.create_table(
        NODE_EMBEDDINGS_TABLE,
        sa.Column(
            "node_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_nodes.id"),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{NODE_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{NODE_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    op.execute(
        f"CREATE INDEX {NODE_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{NODE_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── edge_embeddings (pgvector) ───────────────────────────────────────
    op.create_table(
        EDGE_EMBEDDINGS_TABLE,
        sa.Column(
            "edge_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_edges.id"),
            primary_key=True,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{EDGE_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{EDGE_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    op.execute(
        f"CREATE INDEX {EDGE_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{EDGE_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── triple_embeddings (pgvector) ─────────────────────────────────────
    op.create_table(
        TRIPLE_EMBEDDINGS_TABLE,
        sa.Column(
            "triple_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEMA}.ontogen_triples.id"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="llm_pending"),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{TRIPLE_EMBEDDINGS_TABLE} "
        f"ADD COLUMN embedding vector({EMBEDDING_DIMENSION}) NOT NULL "
        f"DEFAULT array_fill(0, ARRAY[{EMBEDDING_DIMENSION}])::vector({EMBEDDING_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.{TRIPLE_EMBEDDINGS_TABLE} ALTER COLUMN embedding DROP DEFAULT"
    )
    op.execute(
        f"CREATE INDEX {TRIPLE_EMBEDDINGS_HNSW_INDEX} "
        f"ON {SCHEMA}.{TRIPLE_EMBEDDINGS_TABLE} "
        f"USING hnsw (embedding vector_cosine_ops)"
    )

    # ── Apache AGE extension + graph ─────────────────────────────────────
    # age may already be installed via the initdb hook; CREATE EXTENSION
    # IF NOT EXISTS is idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS age")
    # AGE registers its operator classes (graphid_ops, etc.) in ag_catalog.
    # create_graph() generates CREATE INDEX statements without schema-qualifying
    # those operator classes, so ag_catalog must be in search_path for the
    # session running the migration. shared_preload_libraries='age' makes the
    # extension available cluster-wide but does not modify search_path.
    op.execute("SET search_path TO ag_catalog, public")
    # Create graph — wrapped in PL/pgSQL exception block to be idempotent.
    op.execute(
        """
        DO $$ BEGIN
            PERFORM ag_catalog.create_graph('dataspoke_ontogen');
        EXCEPTION WHEN others THEN
            -- Ignore "graph already exists" and any duplicate_object error.
            IF SQLERRM LIKE '%already exists%' OR SQLSTATE = '42710' THEN
                NULL;
            ELSE
                RAISE;
            END IF;
        END $$
        """
    )
    # Grant ag_catalog usage to the current application role so service
    # queries work. Wrapped in a DO block so non-owner roles (where the
    # initdb hook already granted access) don't fail on insufficient_privilege.
    op.execute(
        """
        DO $$
        BEGIN
          GRANT USAGE ON SCHEMA ag_catalog TO CURRENT_USER;
        EXCEPTION WHEN insufficient_privilege THEN
          RAISE NOTICE 'ag_catalog GRANT skipped (insufficient privilege; assumed pre-granted by initdb hook)';
        END $$;
        """  # noqa: E501
    )


def downgrade() -> None:
    # Pre-release schema; no reverse path is provided.
    pass
