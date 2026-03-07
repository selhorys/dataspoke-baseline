"""Qdrant client wrapper with collection management, upsert, and search."""

from qdrant_client import AsyncQdrantClient, models

from src.shared.exceptions import StorageUnavailableError


class QdrantManager:
    """Manages Qdrant collections and provides typed search/upsert."""

    def __init__(self, host: str, port: int, grpc_port: int, api_key: str) -> None:
        self._client = AsyncQdrantClient(
            host=host,
            port=port,
            grpc_port=grpc_port,
            api_key=api_key or None,
        )

    async def ensure_collection(self, name: str, vector_size: int) -> None:
        """Create collection if it does not exist (idempotent)."""
        try:
            collections = await self._client.get_collections()
            existing = {c.name for c in collections.collections}
            if name not in existing:
                await self._client.create_collection(
                    collection_name=name,
                    vectors_config=models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE,
                    ),
                )
        except Exception as exc:
            raise StorageUnavailableError(f"Qdrant ensure_collection failed: {exc}") from exc

    async def upsert(self, collection: str, points: list[models.PointStruct]) -> None:
        try:
            await self._client.upsert(collection_name=collection, points=points)
        except Exception as exc:
            raise StorageUnavailableError(f"Qdrant upsert failed: {exc}") from exc

    async def search(
        self,
        collection: str,
        vector: list[float],
        limit: int = 20,
        filters: dict | None = None,
    ) -> list[models.ScoredPoint]:
        try:
            query_filter = None
            if filters:
                must_conditions = [
                    models.FieldCondition(
                        key=k,
                        match=models.MatchValue(value=v),
                    )
                    for k, v in filters.items()
                ]
                query_filter = models.Filter(must=must_conditions)

            return await self._client.query_points(
                collection_name=collection,
                query=vector,
                limit=limit,
                query_filter=query_filter,
            ).points
        except Exception as exc:
            raise StorageUnavailableError(f"Qdrant search failed: {exc}") from exc

    async def delete(self, collection: str, ids: list[str]) -> None:
        try:
            await self._client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=ids),
            )
        except Exception as exc:
            raise StorageUnavailableError(f"Qdrant delete failed: {exc}") from exc

    async def close(self) -> None:
        await self._client.close()
