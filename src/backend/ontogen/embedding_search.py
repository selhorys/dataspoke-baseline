"""pgvector search helpers shared between OntogenService and the debate loop.

Keeping these in a dedicated module avoids a circular import between
service.py (which imports debate.py) and debate.py.
"""

import logging

from src.shared.config import ONTOLOGY_CONFIDENCE_THRESHOLD
from src.shared.vector.client import PgVectorManager, VectorHit

logger = logging.getLogger(__name__)

# Minimum similarity for node reuse-guard callers.
NODE_REUSE_THRESHOLD: float = ONTOLOGY_CONFIDENCE_THRESHOLD


async def search_node_embeddings(
    vector: PgVectorManager,
    query_vec: list[float],
    top_k: int = 5,
    threshold: float | None = NODE_REUSE_THRESHOLD,
) -> list[VectorHit]:
    """Search the node_embeddings table for approved nodes similar to *query_vec*.

    When ``threshold`` is provided, only rows meeting the minimum similarity
    are returned (default: ``NODE_REUSE_THRESHOLD`` for reuse-guard callers).
    Pass ``threshold=None`` to return all top-k results regardless of score
    (used by RAG anchor sampling in the debate loop).

    ``VectorHit.payload`` carries ``{"name": <node name>}`` for callers that
    need the display name (e.g. RAG anchor construction).
    Falls back to empty list on any error.
    """
    try:
        from sqlalchemy import text

        async with vector._session_factory() as session:
            vector_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
            if threshold is not None:
                sql = text(
                    """
                    SELECT
                        node_id,
                        name,
                        GREATEST(0.0, 1.0 - (embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.node_embeddings
                    WHERE status IN ('approved', 'llm_approved')
                      AND GREATEST(0.0, 1.0 - (embedding <=> CAST(:query_vector AS vector))) >= :threshold
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {
                        "query_vector": vector_literal,
                        "threshold": threshold,
                        "limit": top_k,
                    },
                )
            else:
                sql = text(
                    """
                    SELECT
                        node_id,
                        name,
                        GREATEST(0.0, 1.0 - (embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.node_embeddings
                    WHERE status IN ('approved', 'llm_approved')
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {"query_vector": vector_literal, "limit": top_k},
                )
            rows = result.fetchall()

        # dataset_urn stores node_id; payload carries name for RAG anchor consumers.
        return [
            VectorHit(
                dataset_urn=row.node_id,
                score=float(row.score),
                payload={"name": row.name or ""},
            )
            for row in rows
        ]
    except Exception:
        logger.warning("ontogen_node_embedding_search_error", exc_info=True)
        return []


async def search_edge_embeddings(
    vector: PgVectorManager,
    query_vec: list[float],
    top_k: int = 5,
    threshold: float | None = None,
) -> list[VectorHit]:
    """Search edge_embeddings for approved edges similar to *query_vec*.

    ``threshold=None`` (default) returns all top-k results; set a float to
    filter by minimum similarity. Falls back to empty list on any error.
    Stores edge_id in the dataset_urn field of VectorHit for consumer convenience.
    """
    try:
        from sqlalchemy import text

        async with vector._session_factory() as session:
            vector_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
            if threshold is not None:
                sql = text(
                    """
                    SELECT
                        ee.edge_id,
                        GREATEST(0.0,
                            1.0 - (ee.embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.edge_embeddings ee
                    JOIN dataspoke.ontogen_edges oe ON oe.id = ee.edge_id
                    WHERE oe.status IN ('approved', 'llm_approved')
                      AND GREATEST(0.0,
                            1.0 - (ee.embedding <=> CAST(:query_vector AS vector))) >= :threshold
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {"query_vector": vector_literal, "threshold": threshold, "limit": top_k},
                )
            else:
                sql = text(
                    """
                    SELECT
                        ee.edge_id,
                        GREATEST(0.0, 1.0 - (ee.embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.edge_embeddings ee
                    JOIN dataspoke.ontogen_edges oe ON oe.id = ee.edge_id
                    WHERE oe.status IN ('approved', 'llm_approved')
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {"query_vector": vector_literal, "limit": top_k},
                )
            rows = result.fetchall()

        return [VectorHit(dataset_urn=row.edge_id, score=float(row.score)) for row in rows]
    except Exception:
        logger.warning("ontogen_edge_embedding_search_error", exc_info=True)
        return []


async def search_triple_embeddings(
    vector: PgVectorManager,
    query_vec: list[float],
    top_k: int = 5,
    threshold: float | None = None,
) -> list[VectorHit]:
    """Search triple_embeddings for approved triples similar to *query_vec*.

    ``threshold=None`` (default) returns all top-k results; set a float to
    filter by minimum similarity. Falls back to empty list on any error.
    Stores triple_id in the dataset_urn field of VectorHit for consumer convenience.
    """
    try:
        from sqlalchemy import text

        async with vector._session_factory() as session:
            vector_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
            if threshold is not None:
                sql = text(
                    """
                    SELECT
                        te.triple_id,
                        GREATEST(0.0,
                            1.0 - (te.embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.triple_embeddings te
                    JOIN dataspoke.ontogen_triples ot ON ot.id = te.triple_id
                    WHERE ot.status IN ('approved', 'llm_approved')
                      AND GREATEST(0.0,
                            1.0 - (te.embedding <=> CAST(:query_vector AS vector))) >= :threshold
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {"query_vector": vector_literal, "threshold": threshold, "limit": top_k},
                )
            else:
                sql = text(
                    """
                    SELECT
                        te.triple_id,
                        GREATEST(0.0, 1.0 - (te.embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.triple_embeddings te
                    JOIN dataspoke.ontogen_triples ot ON ot.id = te.triple_id
                    WHERE ot.status IN ('approved', 'llm_approved')
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {"query_vector": vector_literal, "limit": top_k},
                )
            rows = result.fetchall()

        return [VectorHit(dataset_urn=row.triple_id, score=float(row.score)) for row in rows]
    except Exception:
        logger.warning("ontogen_triple_embedding_search_error", exc_info=True)
        return []
