"""Qdrant vector database client wrapper for DataSpoke."""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    ScoredPoint,
    VectorParams,
)

from src.shared.config import EMBEDDING_DIMENSION


class QdrantManager:
    """Qdrant collection management, search, and upsert."""

    def __init__(self, host: str, port: int, api_key: str, grpc_port: int = 6334) -> None:
        self._client = AsyncQdrantClient(
            host=host,
            port=port,
            grpc_port=grpc_port,
            api_key=api_key if api_key else None,
            prefer_grpc=True,
        )

    async def ensure_collection(self, name: str, vector_size: int = EMBEDDING_DIMENSION) -> None:
        collections = await self._client.get_collections()
        existing = {c.name for c in collections.collections}
        if name not in existing:
            await self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    async def upsert(self, collection: str, points: list[PointStruct]) -> None:
        await self._client.upsert(collection_name=collection, points=points)

    async def search(
        self,
        collection: str,
        vector: list[float],
        limit: int = 20,
        filters: dict[str, str] | None = None,
    ) -> list[ScoredPoint]:
        query_filter = None
        if filters:
            query_filter = Filter(
                must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()]
            )
        result = await self._client.query_points(
            collection_name=collection,
            query=vector,
            limit=limit,
            query_filter=query_filter,
        )
        return result.points

    async def delete(self, collection: str, ids: list[str]) -> None:
        await self._client.delete(
            collection_name=collection,
            points_selector=ids,
        )

    async def check_connectivity(self) -> bool:
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False
