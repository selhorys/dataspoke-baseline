"""Regression tests for embedding upsert SQL.

Each `_upsert_*_embedding` helper and `PgVectorManager.upsert` runs raw SQL via
SQLAlchemy + asyncpg with `:name` bind params, pgvector casts, and a
`timestamptz` column. Unit tests use MagicMock and cannot catch type-encoding
or bind-parameter syntax bugs. These tests execute the SQL against the real
DataSpoke Postgres so any regression in:

- `:param::TYPE` vs `CAST(:param AS TYPE)` bind syntax
- Python type → Postgres column type encoding (e.g. `str` ↛ `timestamptz`)
- vector literal formatting
- jsonb encoding of tags/owners

surfaces as a hard test failure instead of a silent warning in the API pod
logs (the helpers swallow exceptions and log `*_embedding_upsert_failed`).

spec: BACKEND_SCHEMA.md §node_embeddings, §edge_embeddings, §triple_embeddings,
§dataset_embeddings
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend.ontogen.service import (
    _upsert_edge_embedding,
    _upsert_node_embedding,
    _upsert_triple_embedding,
)
from src.shared.config import EMBEDDING_COLLECTION, EMBEDDING_DIMENSION
from src.shared.vector.client import PgVectorManager, VectorHit


def _dsn() -> str:
    host = os.environ["DATASPOKE_TEST_POSTGRES_HOST"]
    port = os.environ["DATASPOKE_TEST_POSTGRES_PORT"]
    user = os.environ["DATASPOKE_TEST_POSTGRES_USER"]
    password = os.environ["DATASPOKE_TEST_POSTGRES_PASSWORD"]
    db = os.environ["DATASPOKE_TEST_POSTGRES_DB"]
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


@pytest_asyncio.fixture
async def vector() -> AsyncGenerator[PgVectorManager, None]:
    engine = create_async_engine(_dsn(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield PgVectorManager(session_factory=factory)
    await engine.dispose()


def _dummy_embedding() -> list[float]:
    return [0.01] * EMBEDDING_DIMENSION


@pytest.mark.asyncio
async def test_upsert_node_embedding_inserts_row_via_real_asyncpg(
    vector: PgVectorManager,
) -> None:
    """`_upsert_node_embedding` round-trips through asyncpg without type errors.

    Regression for the `timestamptz` encoding bug where `:updated_at` was bound
    as an isoformat string and `CAST(:updated_at AS timestamptz)` couldn't
    persuade asyncpg's type inference to accept it.
    """
    node_id = f"spot_node_{uuid.uuid4().hex[:8]}"
    try:
        # Seed the parent node row (FK constraint on node_embeddings.node_id).
        await _seed_parent_node(vector, node_id)

        await _upsert_node_embedding(
            vector,
            node_id=node_id,
            embedding=_dummy_embedding(),
            name="Spot Test Node",
            status="approved",
        )

        async with vector._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT node_id, name, status FROM dataspoke.node_embeddings"
                    " WHERE node_id = :node_id"
                ),
                {"node_id": node_id},
            )
            row = result.one_or_none()
        assert row is not None, "node_embeddings row not persisted"
        assert row.name == "Spot Test Node"
        assert row.status == "approved"
    finally:
        await _cleanup_node(vector, node_id)


@pytest.mark.asyncio
async def test_upsert_edge_embedding_inserts_row_via_real_asyncpg(
    vector: PgVectorManager,
) -> None:
    """`_upsert_edge_embedding` round-trips through asyncpg without type errors."""
    edge_id = f"spot_edge_{uuid.uuid4().hex[:8]}"
    try:
        await _seed_parent_edge(vector, edge_id)

        await _upsert_edge_embedding(
            vector,
            edge_id=edge_id,
            embedding=_dummy_embedding(),
            label="spot test label",
            status="approved",
        )

        async with vector._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT edge_id, label, status FROM dataspoke.edge_embeddings"
                    " WHERE edge_id = :edge_id"
                ),
                {"edge_id": edge_id},
            )
            row = result.one_or_none()
        assert row is not None, "edge_embeddings row not persisted"
        assert row.label == "spot test label"
        assert row.status == "approved"
    finally:
        await _cleanup_edge(vector, edge_id)


@pytest.mark.asyncio
async def test_upsert_triple_embedding_inserts_row_via_real_asyncpg(
    vector: PgVectorManager,
) -> None:
    """`_upsert_triple_embedding` round-trips through asyncpg without type errors."""
    suffix = uuid.uuid4().hex[:8]
    subj_id = f"spot_subj_{suffix}"
    obj_id = f"spot_obj_{suffix}"
    edge_id = f"spot_te_{suffix}"
    triple_id = f"{subj_id}__{edge_id}__{obj_id}"
    try:
        await _seed_parent_node(vector, subj_id)
        await _seed_parent_node(vector, obj_id)
        await _seed_parent_edge(vector, edge_id)
        await _seed_parent_triple(vector, triple_id, subj_id, edge_id, obj_id)

        await _upsert_triple_embedding(
            vector,
            triple_id=triple_id,
            embedding=_dummy_embedding(),
            status="approved",
        )

        async with vector._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT triple_id, status FROM dataspoke.triple_embeddings"
                    " WHERE triple_id = :triple_id"
                ),
                {"triple_id": triple_id},
            )
            row = result.one_or_none()
        assert row is not None, "triple_embeddings row not persisted"
        assert row.status == "approved"
    finally:
        await _cleanup_triple(vector, triple_id, subj_id, edge_id, obj_id)


@pytest.mark.asyncio
async def test_pgvector_manager_upsert_dataset_embedding_via_real_asyncpg(
    vector: PgVectorManager,
) -> None:
    """`PgVectorManager.upsert` round-trips through asyncpg without type errors.

    Regression for the `:tags::jsonb` / `:owners::jsonb` / `:updated_at::timestamptz`
    bind-parameter bugs and for the `dataset_urn` payload encoding.
    """
    dataset_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,spot_test_db.spot.embed,DEV)"
    )
    hit = VectorHit(
        dataset_urn=dataset_urn,
        score=0.0,
        embedding=_dummy_embedding(),
        payload={
            "platform": "postgres",
            "tags": ["spot-test"],
            "owners": ["urn:li:corpuser:spot"],
            "quality_score": 0.75,
            "has_pii": False,
        },
    )
    try:
        await vector.upsert(EMBEDDING_COLLECTION, [hit])

        async with vector._session_factory() as session:
            result = await session.execute(
                text(
                    f"SELECT dataset_urn, platform, tags, owners, quality_score, has_pii"
                    f" FROM dataspoke.{EMBEDDING_COLLECTION}"
                    " WHERE dataset_urn = :dataset_urn"
                ),
                {"dataset_urn": dataset_urn},
            )
            row = result.one_or_none()
        assert row is not None, "dataset_embeddings row not persisted"
        assert row.platform == "postgres"
        assert row.tags == ["spot-test"]
        assert row.owners == ["urn:li:corpuser:spot"]
        assert row.quality_score == pytest.approx(0.75)
        assert row.has_pii is False
    finally:
        async with vector._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        f"DELETE FROM dataspoke.{EMBEDDING_COLLECTION}"
                        " WHERE dataset_urn = :urn"
                    ),
                    {"urn": dataset_urn},
                )


# ── Raw-SQL parent seeds (FK satisfaction for embedding tables) ───────────────


async def _seed_parent_node(vector: PgVectorManager, node_id: str) -> None:
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO dataspoke.ontogen_nodes"
                    " (id, name, description, confidence_score, status)"
                    " VALUES (:id, :name, :desc, :conf, 'approved')"
                ),
                {"id": node_id, "name": node_id, "desc": "spot-test", "conf": 0.9},
            )


async def _seed_parent_edge(vector: PgVectorManager, edge_id: str) -> None:
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO dataspoke.ontogen_edges"
                    " (id, label, semantics, confidence_score, status)"
                    " VALUES (:id, :label, :sem, :conf, 'approved')"
                ),
                {"id": edge_id, "label": edge_id, "sem": "spot-test", "conf": 0.9},
            )


async def _seed_parent_triple(
    vector: PgVectorManager,
    triple_id: str,
    subj_id: str,
    edge_id: str,
    obj_id: str,
) -> None:
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO dataspoke.ontogen_triples"
                    " (id, subject_node_id, edge_id, object_node_id,"
                    "  confidence_score, status)"
                    " VALUES (:id, :s, :e, :o, :conf, 'approved')"
                ),
                {
                    "id": triple_id,
                    "s": subj_id,
                    "e": edge_id,
                    "o": obj_id,
                    "conf": 0.9,
                },
            )


async def _cleanup_node(vector: PgVectorManager, node_id: str) -> None:
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM dataspoke.node_embeddings WHERE node_id = :id"
                ),
                {"id": node_id},
            )
            await session.execute(
                text("DELETE FROM dataspoke.ontogen_nodes WHERE id = :id"),
                {"id": node_id},
            )


async def _cleanup_edge(vector: PgVectorManager, edge_id: str) -> None:
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM dataspoke.edge_embeddings WHERE edge_id = :id"
                ),
                {"id": edge_id},
            )
            await session.execute(
                text("DELETE FROM dataspoke.ontogen_edges WHERE id = :id"),
                {"id": edge_id},
            )


async def _cleanup_triple(
    vector: PgVectorManager,
    triple_id: str,
    subj_id: str,
    edge_id: str,
    obj_id: str,
) -> None:
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM dataspoke.triple_embeddings WHERE triple_id = :id"
                ),
                {"id": triple_id},
            )
            await session.execute(
                text("DELETE FROM dataspoke.ontogen_triples WHERE id = :id"),
                {"id": triple_id},
            )
            await session.execute(
                text("DELETE FROM dataspoke.ontogen_edges WHERE id = :id"),
                {"id": edge_id},
            )
            await session.execute(
                text(
                    "DELETE FROM dataspoke.ontogen_nodes WHERE id IN (:s, :o)"
                ),
                {"s": subj_id, "o": obj_id},
            )
