"""SearchService — NL-to-vector search over dataset metadata with optional SQL context."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from qdrant_client.models import PointStruct

from src.backend.search.embedding import (
    _extract_name_from_urn,
    _extract_platform_from_urn,
    generate_embedding,
)
from src.shared.cache.client import RedisClient
from src.shared.config import (
    EMBEDDING_COLLECTION,
    SEARCH_RESULT_CACHE_TTL,
    SEARCH_SCORE_THRESHOLD,
)
from src.shared.datahub.client import DataHubClient
from src.shared.exceptions import EntityNotFoundError
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager


class SearchService:
    """NL search over dataset metadata with Redis caching and Qdrant vector search."""

    def __init__(
        self,
        datahub: DataHubClient,
        cache: RedisClient,
        llm: LLMClient,
        qdrant: QdrantManager,
    ) -> None:
        self._datahub = datahub
        self._cache = cache
        self._llm = llm
        self._qdrant = qdrant

    # ── Search ─────────────────────────────────────────────────────────

    async def search(
        self,
        q: str,
        sql_context: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search datasets by natural language query."""
        cache_key = self._cache_key(q, sql_context, offset, limit)

        # Check cache
        cached = await self._cache.get(cache_key)
        if cached:
            return json.loads(cached)

        # Generate query embedding
        query_vector = await self._llm.embed(q)

        # Fetch offset + limit from Qdrant, filtering by minimum similarity score
        fetch_limit = offset + limit
        scored_points = await self._qdrant.search(
            collection=EMBEDDING_COLLECTION,
            vector=query_vector,
            limit=fetch_limit,
            score_threshold=SEARCH_SCORE_THRESHOLD,
        )
        paged_points = scored_points[offset:]

        # Enrich each result with DataHub metadata
        datasets = await asyncio.gather(
            *[self._enrich_result(pt, sql_context) for pt in paged_points]
        )

        result = {
            "datasets": datasets,
            "offset": offset,
            "limit": limit,
            "total_count": len(scored_points),
            "resp_time": datetime.now(tz=UTC).isoformat(),
        }

        # Cache the result
        await self._cache.set(cache_key, json.dumps(result, default=str), SEARCH_RESULT_CACHE_TTL)

        return result

    async def _enrich_result(self, point: Any, sql_context: bool) -> dict[str, Any]:
        """Enrich a Qdrant scored point with DataHub metadata."""
        from datahub.metadata.schema_classes import (
            DatasetPropertiesClass,
            GlobalTagsClass,
            OwnershipClass,
        )

        payload = point.payload or {}
        dataset_urn = payload.get("dataset_urn", "")
        score = point.score

        # Fetch core metadata
        props = await self._datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        global_tags = await self._datahub.get_aspect(dataset_urn, GlobalTagsClass)
        ownership = await self._datahub.get_aspect(dataset_urn, OwnershipClass)

        name = _extract_name_from_urn(dataset_urn)
        description: str | None = None
        if props:
            name = getattr(props, "name", None) or name
            description = getattr(props, "description", None)

        tags: list[str] = []
        if global_tags and hasattr(global_tags, "tags"):
            tags = [str(t.tag).removeprefix("urn:li:tag:") for t in global_tags.tags]

        owners: list[str] = []
        if ownership and hasattr(ownership, "owners"):
            owners = [str(o.owner) for o in ownership.owners]

        platform = payload.get("platform", _extract_platform_from_urn(dataset_urn))
        quality_score = payload.get("quality_score")

        item: dict[str, Any] = {
            "urn": dataset_urn,
            "name": name,
            "platform": platform,
            "description": description,
            "tags": tags,
            "owners": owners,
            "quality_score": quality_score,
            "score": score,
            "sql_context": None,
        }

        if sql_context:
            item["sql_context"] = await self._build_sql_context(dataset_urn)

        return item

    async def _build_sql_context(self, dataset_urn: str) -> dict[str, Any]:
        """Build SQL context with column info, join paths, and sample query."""
        from datahub.metadata.schema_classes import SchemaMetadataClass

        schema_meta = await self._datahub.get_aspect(dataset_urn, SchemaMetadataClass)

        columns: list[dict[str, Any]] = []
        if schema_meta and hasattr(schema_meta, "fields"):
            for field in schema_meta.fields:
                columns.append(
                    {
                        "name": field.fieldPath,
                        "type": getattr(field, "nativeDataType", "unknown"),
                        "sample_values": [],
                    }
                )

        # Get downstream lineage to infer join paths
        downstream_urns = await self._datahub.get_downstream_lineage(dataset_urn)
        join_paths: list[dict[str, Any]] = [
            {"target_urn": urn, "join_keys": []} for urn in (downstream_urns or [])[:5]
        ]

        # Generate sample query via LLM (best-effort)
        sample_query: str | None = None
        if columns:
            col_list = ", ".join(c["name"] for c in columns[:10])
            table_name = _extract_name_from_urn(dataset_urn)
            try:
                sample_query = await self._llm.complete(
                    prompt=f"Write a simple SQL SELECT query for table '{table_name}' "
                    f"with columns: {col_list}. Return only the SQL, no explanation.",
                    system="You are a SQL assistant. Return only valid SQL.",
                )
            except Exception:
                sample_query = None

        return {
            "columns": columns,
            "join_paths": join_paths,
            "sample_query": sample_query,
        }

    # ── Reindex ────────────────────────────────────────────────────────

    async def reindex(self, dataset_urn: str) -> dict[str, Any]:
        """Regenerate the vector embedding for a dataset and upsert into Qdrant."""
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        props = await self._datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        if props is None:
            raise EntityNotFoundError("dataset", dataset_urn)

        embedding, payload = await generate_embedding(self._llm, self._datahub, dataset_urn)

        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dataset_urn))
        payload["updated_at"] = datetime.now(tz=UTC).isoformat()

        await self._qdrant.ensure_collection(EMBEDDING_COLLECTION)
        await self._qdrant.upsert(
            collection=EMBEDDING_COLLECTION,
            points=[PointStruct(id=point_id, vector=embedding, payload=payload)],
        )

        return {
            "status": "ok",
            "message": f"Reindexed {dataset_urn}",
        }

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(q: str, sql_context: bool, offset: int, limit: int) -> str:
        raw = f"{q}:{sql_context}:{offset}:{limit}"
        digest = hashlib.md5(raw.encode()).hexdigest()  # noqa: S324
        return f"search:{digest}"
