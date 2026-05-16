"""pgvector search helpers for metagen candidate embeddings.

Spec: spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate §RAG anchors
"""

import logging

from src.shared.vector.client import PgVectorManager, VectorHit

logger = logging.getLogger(__name__)


async def search_candidate_embeddings(
    vector: PgVectorManager,
    query_vec: list[float],
    kind: str,
    top_k: int = 5,
    threshold: float | None = None,
) -> list[VectorHit]:
    """Search metagen_candidate_embeddings for approved candidates similar to *query_vec*.

    Filters by *kind* (``dataset.description`` or ``column.description``) so the
    Reviewer's RAG pool is separated by element type.  Falls back to empty list on
    any error.

    ``VectorHit.payload`` carries ``{"value": <candidate value>}`` for RAG anchor
    prompt rendering.
    """
    try:
        from sqlalchemy import text

        async with vector._session_factory() as session:
            vector_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
            if threshold is not None:
                sql = text(
                    """
                    SELECT
                        mce.candidate_id::text,
                        mc.value,
                        mc.dataset_urn,
                        mc.item_id,
                        GREATEST(0.0, 1.0 - (mce.embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.metagen_candidate_embeddings mce
                    JOIN dataspoke.metagen_candidates mc
                      ON mc.candidate_id = mce.candidate_id
                    WHERE mc.status = 'approved'
                      AND mce.kind = :kind
                      AND GREATEST(0.0, 1.0 - (mce.embedding <=> CAST(:query_vector AS vector))) >= :threshold
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {
                        "query_vector": vector_literal,
                        "kind": kind,
                        "threshold": threshold,
                        "limit": top_k,
                    },
                )
            else:
                sql = text(
                    """
                    SELECT
                        mce.candidate_id::text,
                        mc.value,
                        mc.dataset_urn,
                        mc.item_id,
                        GREATEST(0.0, 1.0 - (mce.embedding <=> CAST(:query_vector AS vector))) AS score
                    FROM dataspoke.metagen_candidate_embeddings mce
                    JOIN dataspoke.metagen_candidates mc
                      ON mc.candidate_id = mce.candidate_id
                    WHERE mc.status = 'approved'
                      AND mce.kind = :kind
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                )
                result = await session.execute(
                    sql,
                    {"query_vector": vector_literal, "kind": kind, "limit": top_k},
                )
            rows = result.fetchall()

        return [
            VectorHit(
                dataset_urn=row.candidate_id,
                score=float(row.score),
                payload={
                    "value": row.value or "",
                    "dataset_urn": row.dataset_urn,
                    "item_id": row.item_id,
                },
            )
            for row in rows
        ]
    except Exception:
        logger.warning("metagen_candidate_embedding_search_error", exc_info=True)
        return []
