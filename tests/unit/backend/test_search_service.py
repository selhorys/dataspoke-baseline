"""Unit tests for SearchService (mocked infrastructure)."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.search.service import SearchService
from src.shared.exceptions import EntityNotFoundError

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


@pytest.fixture
def service(datahub, cache, llm, qdrant):
    return SearchService(datahub=datahub, cache=cache, llm=llm, qdrant=qdrant)


def _make_scored_point(urn: str = _DATASET_URN, score: float = 0.95) -> MagicMock:
    point = MagicMock()
    point.payload = {
        "dataset_urn": urn,
        "platform": "postgres",
        "quality_score": 85,
        "tags": ["production"],
    }
    point.score = score
    return point


def _mock_datahub_enrichment(datahub: AsyncMock) -> None:
    """Set up DataHub mock to return realistic aspect data for enrichment."""
    props = MagicMock()
    props.name = "mydb.public.users"
    props.description = "User account table"

    tags = MagicMock()
    tag_assoc = MagicMock()
    tag_assoc.tag = "urn:li:tag:production"
    tags.tags = [tag_assoc]

    ownership = MagicMock()
    owner = MagicMock()
    owner.owner = "urn:li:corpuser:alice@example.com"
    ownership.owners = [owner]

    datahub.get_aspect.side_effect = [props, tags, ownership]


class TestSearch:
    async def test_returns_cached_result(self, service, cache, qdrant, llm):
        cached_data = {
            "datasets": [{"urn": _DATASET_URN, "name": "users", "score": 0.9}],
            "offset": 0,
            "limit": 20,
            "total_count": 1,
            "resp_time": datetime.now(tz=UTC).isoformat(),
        }
        cache.get.return_value = json.dumps(cached_data)

        result = await service.search(q="user tables")

        assert result["datasets"][0]["urn"] == _DATASET_URN
        qdrant.search.assert_not_called()
        llm.embed.assert_not_called()

    async def test_cache_miss_queries_qdrant(self, service, cache, llm, qdrant, datahub):
        cache.get.return_value = None
        llm.embed.return_value = [0.1] * 1536
        qdrant.search.return_value = [_make_scored_point()]
        _mock_datahub_enrichment(datahub)

        result = await service.search(q="user tables")

        llm.embed.assert_called_once_with("user tables")
        qdrant.search.assert_called_once()
        assert len(result["datasets"]) == 1
        assert result["datasets"][0]["score"] == 0.95

    async def test_enriches_with_datahub_metadata(self, service, cache, llm, qdrant, datahub):
        cache.get.return_value = None
        llm.embed.return_value = [0.1] * 1536
        qdrant.search.return_value = [_make_scored_point()]
        _mock_datahub_enrichment(datahub)

        result = await service.search(q="user tables")

        item = result["datasets"][0]
        assert item["name"] == "mydb.public.users"
        assert "urn:li:corpuser:alice@example.com" in item["owners"]
        assert "production" in item["tags"]

    async def test_without_sql_context(self, service, cache, llm, qdrant, datahub):
        cache.get.return_value = None
        llm.embed.return_value = [0.1] * 1536
        qdrant.search.return_value = [_make_scored_point()]
        _mock_datahub_enrichment(datahub)

        result = await service.search(q="user tables", sql_context=False)

        item = result["datasets"][0]
        assert item["sql_context"] is None

    async def test_with_sql_context(self, service, cache, llm, qdrant, datahub):
        cache.get.return_value = None
        llm.embed.return_value = [0.1] * 1536
        qdrant.search.return_value = [_make_scored_point()]

        # Mock enrichment: props, tags, ownership (for _enrich_result)
        props = MagicMock()
        props.name = "mydb.public.users"
        props.description = "User account table"

        tags = MagicMock()
        tag_assoc = MagicMock()
        tag_assoc.tag = "urn:li:tag:production"
        tags.tags = [tag_assoc]

        ownership = MagicMock()
        owner = MagicMock()
        owner.owner = "urn:li:corpuser:alice@example.com"
        ownership.owners = [owner]

        # Mock SQL context: schema (for _build_sql_context)
        schema = MagicMock()
        field = MagicMock()
        field.fieldPath = "id"
        field.nativeDataType = "integer"
        schema.fields = [field]

        # get_aspect calls: props, tags, ownership (enrich), then schema (sql_context)
        datahub.get_aspect.side_effect = [props, tags, ownership, schema]
        datahub.get_downstream_lineage.return_value = []
        llm.complete.return_value = "SELECT id FROM users LIMIT 10"

        result = await service.search(q="user tables", sql_context=True)

        item = result["datasets"][0]
        assert item["sql_context"] is not None
        assert len(item["sql_context"]["columns"]) == 1
        assert item["sql_context"]["columns"][0]["name"] == "id"

    async def test_caches_result(self, service, cache, llm, qdrant, datahub):
        cache.get.return_value = None
        llm.embed.return_value = [0.1] * 1536
        qdrant.search.return_value = [_make_scored_point()]
        _mock_datahub_enrichment(datahub)

        await service.search(q="user tables")

        cache.set.assert_called_once()
        call_args = cache.set.call_args
        assert call_args[0][0].startswith("search:")
        assert call_args[0][2] == 120  # TTL

    async def test_empty_results(self, service, cache, llm, qdrant):
        cache.get.return_value = None
        llm.embed.return_value = [0.1] * 1536
        qdrant.search.return_value = []

        result = await service.search(q="nonexistent dataset")

        assert result["datasets"] == []
        assert result["total_count"] == 0


class TestReindex:
    async def test_fetches_and_upserts(self, service, datahub, llm, qdrant):
        # DataHub aspects for reindex: first get_aspect (props check), then generate_embedding calls
        props = MagicMock()
        props.name = "mydb.public.users"
        props.description = "User account table"

        schema = MagicMock()
        field = MagicMock()
        field.fieldPath = "id"
        field.description = "Primary key"
        schema.fields = [field]

        tags = MagicMock()
        tag = MagicMock()
        tag.tag = "urn:li:tag:production"
        tags.tags = [tag]

        ownership = MagicMock()
        owner = MagicMock()
        owner.owner = "urn:li:corpuser:alice"
        ownership.owners = [owner]

        # Existence check (props), then generate_embedding: props, schema, tags, ownership
        datahub.get_aspect.side_effect = [props, props, schema, tags, ownership]
        datahub.get_upstream_lineage.return_value = []
        llm.embed.return_value = [0.1] * 1536

        result = await service.reindex(_DATASET_URN)

        assert result["status"] == "ok"
        qdrant.ensure_collection.assert_called_once_with("dataset_embeddings")
        qdrant.upsert.assert_called_once()
        upsert_args = qdrant.upsert.call_args
        points = upsert_args[1]["points"]
        assert len(points) == 1
        assert points[0].payload["dataset_urn"] == _DATASET_URN

    async def test_dataset_not_found(self, service, datahub):
        datahub.get_aspect.return_value = None

        with pytest.raises(EntityNotFoundError):
            await service.reindex(_DATASET_URN)

    async def test_builds_correct_payload(self, service, datahub, llm, qdrant):
        props = MagicMock()
        props.name = "mydb.public.users"
        props.description = "User table"

        schema = MagicMock()
        schema.fields = []

        tags = MagicMock()
        tag = MagicMock()
        tag.tag = "urn:li:tag:pii"
        tags.tags = [tag]

        ownership = MagicMock()
        ownership.owners = []

        datahub.get_aspect.side_effect = [props, props, schema, tags, ownership]
        datahub.get_upstream_lineage.return_value = []
        llm.embed.return_value = [0.1] * 1536

        await service.reindex(_DATASET_URN)

        upsert_args = qdrant.upsert.call_args
        payload = upsert_args[1]["points"][0].payload
        assert payload["dataset_urn"] == _DATASET_URN
        assert payload["platform"] == "postgres"
        assert "pii" in payload["tags"]
        assert "updated_at" in payload
