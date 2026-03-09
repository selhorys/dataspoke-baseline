"""Integration tests for Alembic migrations against dev-env PostgreSQL.

No baseline dummy-data extensions needed — tests operate exclusively on the
``dataspoke`` schema created by the migration.

Prerequisites:
- Port-forward PostgreSQL to localhost:9201
- Port-forward lock service to localhost:9221
- dev_env/dummy-data-reset.sh has been run (conftest acquires lock)
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .conftest import _alembic_cmd

IMAZON_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.catalog.title_master,PROD)"
SCHEMA = "dataspoke"

EXPECTED_TABLES = {
    "ingestion_configs",
    "validation_configs",
    "validation_results",
    "generation_configs",
    "generation_results",
    "concept_categories",
    "dataset_concept_map",
    "concept_relationships",
    "metric_definitions",
    "metric_results",
    "metric_issues",
    "events",
    "department_mapping",
    "overview_config",
}

EXPECTED_INDEXES = {
    "ix_validation_results_urn_measured",
    "ix_generation_results_urn_generated",
    "ix_metric_results_metric_measured",
    "ix_events_entity_occurred",
    "ix_metric_issues_status_priority",
    "ix_metric_issues_urn_status",
    "ix_metric_issues_metric_created",
    "ix_dataset_concept_map_concept",
    "ix_concept_categories_parent",
}


@pytest.mark.asyncio
async def test_dataspoke_schema_exists(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = :schema"
            ),
            {"schema": SCHEMA},
        )
        rows = result.fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_all_14_tables_created(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
            ),
            {"schema": SCHEMA},
        )
        actual_tables = {row[0] for row in result.fetchall()}
    assert actual_tables == EXPECTED_TABLES


@pytest.mark.asyncio
async def test_indexes_created(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
            {"schema": SCHEMA},
        )
        actual_indexes = {row[0] for row in result.fetchall()}
    assert EXPECTED_INDEXES.issubset(actual_indexes), (
        f"Missing indexes: {EXPECTED_INDEXES - actual_indexes}"
    )


@pytest.mark.asyncio
async def test_column_types(async_engine: AsyncEngine) -> None:
    checks = [
        ("ingestion_configs", "id", "uuid"),
        ("ingestion_configs", "sources", "jsonb"),
        ("ingestion_configs", "created_at", "timestamp with time zone"),
        ("ingestion_configs", "dataset_urn", "text"),
        ("validation_results", "quality_score", "real"),
        ("metric_definitions", "id", "text"),
        ("overview_config", "id", "integer"),
        ("metric_issues", "estimated_fix_minutes", "integer"),
    ]
    async with async_engine.connect() as conn:
        for table, column, expected_type in checks:
            result = await conn.execute(
                sa.text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table "
                    "AND column_name = :column"
                ),
                {"schema": SCHEMA, "table": table, "column": column},
            )
            row = result.fetchone()
            assert row is not None, f"Column {table}.{column} not found"
            assert row[0] == expected_type, (
                f"{table}.{column}: expected {expected_type}, got {row[0]}"
            )


@pytest.mark.asyncio
async def test_unique_constraint_enforced(async_session: AsyncSession) -> None:
    row_id1 = uuid.uuid4()
    row_id2 = uuid.uuid4()
    insert = sa.text(
        f"INSERT INTO {SCHEMA}.ingestion_configs "
        "(id, dataset_urn, sources, deep_spec_enabled, status, owner) "
        "VALUES (:id, :urn, :sources, :deep, :status, :owner)"
    )
    await async_session.execute(
        insert,
        {
            "id": str(row_id1),
            "urn": IMAZON_URN,
            "sources": "[]",
            "deep": False,
            "status": "active",
            "owner": "test",
        },
    )
    await async_session.commit()

    with pytest.raises(Exception, match="unique|duplicate"):
        await async_session.execute(
            insert,
            {
                "id": str(row_id2),
                "urn": IMAZON_URN,
                "sources": "[]",
                "deep": False,
                "status": "active",
                "owner": "test",
            },
        )
        await async_session.commit()

    await async_session.rollback()
    await async_session.execute(
        sa.text(f"DELETE FROM {SCHEMA}.ingestion_configs WHERE id = :id"),
        {"id": str(row_id1)},
    )
    await async_session.commit()


@pytest.mark.asyncio
async def test_fk_constraint_enforced(async_session: AsyncSession) -> None:
    fake_concept_id = uuid.uuid4()
    insert = sa.text(
        f"INSERT INTO {SCHEMA}.dataset_concept_map "
        "(dataset_urn, concept_id, confidence_score, status) "
        "VALUES (:urn, :cid, :score, :status)"
    )
    with pytest.raises(Exception, match="foreign key|violates"):
        await async_session.execute(
            insert,
            {"urn": IMAZON_URN, "cid": str(fake_concept_id), "score": 0.9, "status": "approved"},
        )
        await async_session.commit()
    await async_session.rollback()


@pytest.mark.asyncio
async def test_self_referencing_fk(async_session: AsyncSession) -> None:
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    insert = sa.text(
        f"INSERT INTO {SCHEMA}.concept_categories "
        "(id, name, parent_id, description, status, version) "
        "VALUES (:id, :name, :pid, :desc, :status, :ver)"
    )
    # Insert parent
    await async_session.execute(
        insert,
        {
            "id": str(parent_id),
            "name": "test_parent",
            "pid": None,
            "desc": "parent",
            "status": "approved",
            "ver": 1,
        },
    )
    # Insert child with valid parent_id
    await async_session.execute(
        insert,
        {
            "id": str(child_id),
            "name": "test_child",
            "pid": str(parent_id),
            "desc": "child",
            "status": "approved",
            "ver": 1,
        },
    )
    await async_session.commit()

    # Attempt with nonexistent parent
    bad_id = uuid.uuid4()
    with pytest.raises(Exception, match="foreign key|violates"):
        await async_session.execute(
            insert,
            {
                "id": str(uuid.uuid4()),
                "name": "test_orphan",
                "pid": str(bad_id),
                "desc": "orphan",
                "status": "pending",
                "ver": 1,
            },
        )
        await async_session.commit()
    await async_session.rollback()

    # Cleanup
    await async_session.execute(
        sa.text(f"DELETE FROM {SCHEMA}.concept_categories WHERE id = :id"),
        {"id": str(child_id)},
    )
    await async_session.execute(
        sa.text(f"DELETE FROM {SCHEMA}.concept_categories WHERE id = :id"),
        {"id": str(parent_id)},
    )
    await async_session.commit()


def test_downgrade_and_reupgrade() -> None:
    result = _alembic_cmd("downgrade", "base")
    assert result.returncode == 0, f"downgrade failed: {result.stderr}"

    result = _alembic_cmd("upgrade", "head")
    assert result.returncode == 0, f"re-upgrade failed: {result.stderr}"
