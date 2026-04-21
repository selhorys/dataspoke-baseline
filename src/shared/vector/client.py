"""pgvector-backed vector database client for DataSpoke."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.config import EMBEDDING_COLLECTION, EMBEDDING_DIMENSION

logger = logging.getLogger(__name__)

# Supported filter keys that map to dedicated table columns.
# Any unknown key raises NotImplementedError so future callers fail loudly.
_COLUMN_FILTERS: frozenset[str] = frozenset({"platform", "has_pii"})


@dataclass
class VectorHit:
    """A single result from a pgvector similarity search.

    ``score`` is cosine similarity in [0, 1], higher is better.
    ``embedding`` carries the raw embedding vector; populated by callers
    that need to upsert — left empty for search results.
    """

    dataset_urn: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)


class PgVectorManager:
    """PostgreSQL + pgvector collection management, search, and upsert.

    The ``collection`` parameter in each method must equal
    ``EMBEDDING_COLLECTION`` — any other value raises ``ValueError`` to
    prevent SQL injection via table-name interpolation.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Inject an ``async_sessionmaker[AsyncSession]`` — typically ``SessionLocal``."""
        self._session_factory = session_factory

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _validate_collection(collection: str) -> None:
        """Raise ValueError when ``collection`` is not the whitelisted table name."""
        if collection != EMBEDDING_COLLECTION:
            raise ValueError(f"Unsupported collection: {collection!r}")

    # ── Collection management ─────────────────────────────────────────────

    async def ensure_collection(
        self,
        name: str,
        vector_size: int = EMBEDDING_DIMENSION,
    ) -> None:
        """No-op when the Alembic migration has already created the table.

        Emits a WARNING when the expected table is absent (e.g. migration not
        yet applied) so operators can diagnose the problem without a hard crash.
        """
        self._validate_collection(name)
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'dataspoke' AND table_name = :tname"
                ),
                {"tname": name},
            )
            if result.scalar_one_or_none() is None:
                logger.warning(
                    "pgvector_table_missing",
                    extra={
                        "table": name,
                        "hint": "Run Alembic migrations: uv run alembic upgrade head",
                    },
                )

    # ── Upsert ────────────────────────────────────────────────────────────

    async def upsert(self, collection: str, hits: list[VectorHit]) -> None:
        """INSERT … ON CONFLICT (dataset_urn) DO UPDATE.

        ``collection`` must equal ``EMBEDDING_COLLECTION``.
        The embedding is read from ``hit.embedding`` and cast to the pgvector
        type via ``::vector`` in the SQL so asyncpg can bind a Python list.
        ``hit.payload`` keys are split across the table's dedicated columns
        (``platform``, ``tags``, ``owners``, ``quality_score``, ``has_pii``,
        ``updated_at``); unknown keys are silently ignored (logged at DEBUG).
        """
        self._validate_collection(collection)
        if not hits:
            return

        async with self._session_factory() as session:
            async with session.begin():
                for hit in hits:
                    p = hit.payload

                    # Warn callers about unexpected payload keys at debug level.
                    _known_payload_keys = frozenset(
                        {"platform", "tags", "owners", "quality_score", "has_pii", "updated_at"}
                    )
                    unknown_keys = set(p) - _known_payload_keys
                    if unknown_keys:
                        logger.debug(
                            "upsert_unknown_payload_keys",
                            extra={"keys": sorted(unknown_keys), "dataset_urn": hit.dataset_urn},
                        )

                    tags_json = json.dumps(p.get("tags") or [])
                    owners_json = json.dumps(p.get("owners") or [])
                    quality_score: float | None = p.get("quality_score")
                    has_pii: bool = bool(p.get("has_pii", False))
                    platform: str | None = p.get("platform")
                    updated_at: str | None = p.get("updated_at")

                    # Convert list[float] to the pgvector literal "[f1,f2,...]"
                    vector_literal = "[" + ",".join(str(v) for v in hit.embedding) + "]"

                    await session.execute(
                        text(
                            f"""
                            INSERT INTO dataspoke.{collection}
                                (dataset_urn, embedding, platform, tags, owners,
                                 quality_score, has_pii, updated_at)
                            VALUES
                                (:dataset_urn, :embedding::vector, :platform,
                                 :tags::jsonb, :owners::jsonb,
                                 :quality_score, :has_pii,
                                 COALESCE(:updated_at::timestamptz, NOW()))
                            ON CONFLICT (dataset_urn) DO UPDATE SET
                                embedding    = EXCLUDED.embedding,
                                platform     = EXCLUDED.platform,
                                tags         = EXCLUDED.tags,
                                owners       = EXCLUDED.owners,
                                quality_score = EXCLUDED.quality_score,
                                has_pii      = EXCLUDED.has_pii,
                                updated_at   = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "dataset_urn": hit.dataset_urn,
                            "embedding": vector_literal,
                            "platform": platform,
                            "tags": tags_json,
                            "owners": owners_json,
                            "quality_score": quality_score,
                            "has_pii": has_pii,
                            "updated_at": updated_at,
                        },
                    )

    # ── Search ────────────────────────────────────────────────────────────

    async def search(
        self,
        collection: str,
        vector: list[float],
        limit: int = 20,
        filters: dict[str, str] | None = None,
        score_threshold: float | None = None,
    ) -> list[VectorHit]:
        """Return hits sorted by cosine similarity desc; score ∈ [0, 1].

        ``score`` is computed as ``GREATEST(0.0, 1.0 - (embedding <=> query_vector))``
        so that higher values indicate greater similarity and the value is clamped to [0, 1].

        ``filters`` is AND-applied against dedicated table columns.  Supported
        keys: ``platform`` (TEXT) and ``has_pii`` (BOOLEAN).  Passing an
        unknown key raises ``NotImplementedError`` to fail loudly.

        ``score_threshold`` removes hits whose score falls below that value.
        """
        self._validate_collection(collection)

        # Validate filter keys early
        if filters:
            unknown = set(filters) - _COLUMN_FILTERS
            if unknown:
                raise NotImplementedError(
                    f"PgVectorManager.search: unsupported filter keys {unknown}. "
                    f"Supported: {_COLUMN_FILTERS}"
                )

        vector_literal = "[" + ",".join(str(v) for v in vector) + "]"

        # Build WHERE clause fragments
        where_clauses: list[str] = []
        params: dict[str, Any] = {
            "query_vector": vector_literal,
            "limit": limit,
        }

        if filters:
            if "platform" in filters:
                where_clauses.append("platform = :filter_platform")
                params["filter_platform"] = filters["platform"]
            if "has_pii" in filters:
                # Accept string "true"/"false" or bool
                raw = filters["has_pii"]
                params["filter_has_pii"] = raw if isinstance(raw, bool) else raw.lower() == "true"
                where_clauses.append("has_pii = :filter_has_pii")

        if score_threshold is not None:
            where_clauses.append(
                "GREATEST(0.0, 1.0 - (embedding <=> :query_vector::vector)) >= :score_threshold"
            )
            params["score_threshold"] = score_threshold

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        sql = text(
            f"""
            SELECT
                dataset_urn,
                platform,
                tags,
                owners,
                quality_score,
                has_pii,
                updated_at,
                GREATEST(0.0, 1.0 - (embedding <=> :query_vector::vector)) AS score
            FROM dataspoke.{collection}
            {where_sql}
            ORDER BY score DESC
            LIMIT :limit
            """
        )

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.fetchall()

        hits: list[VectorHit] = []
        for row in rows:
            score = float(row.score)
            # Tags and owners are stored as JSONB; asyncpg returns them as
            # dicts/lists already, but guard against string fallback.
            tags = row.tags if isinstance(row.tags, list) else json.loads(row.tags or "[]")
            owners = row.owners if isinstance(row.owners, list) else json.loads(row.owners or "[]")
            hits.append(
                VectorHit(
                    dataset_urn=row.dataset_urn,
                    score=score,
                    payload={
                        "dataset_urn": row.dataset_urn,
                        "platform": row.platform,
                        "tags": tags,
                        "owners": owners,
                        "quality_score": row.quality_score,
                        "has_pii": row.has_pii,
                        "updated_at": str(row.updated_at) if row.updated_at else None,
                    },
                )
            )
        return hits

    # ── Delete ────────────────────────────────────────────────────────────

    async def delete(self, collection: str, ids: list[str]) -> None:
        """Delete rows by ``dataset_urn``."""
        self._validate_collection(collection)
        if not ids:
            return
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        f"DELETE FROM dataspoke.{collection} WHERE dataset_urn = ANY(:ids)"
                    ),
                    {"ids": ids},
                )

    # ── Connectivity ──────────────────────────────────────────────────────

    async def check_connectivity(self) -> bool:
        """Return True when PostgreSQL is reachable, False otherwise."""
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.warning("pgvector_connectivity_check_failed", exc_info=True)
            return False
