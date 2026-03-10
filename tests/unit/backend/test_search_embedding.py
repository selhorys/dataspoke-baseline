"""Unit tests for the embedding text builder and generate_embedding function."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.search.embedding import (
    _extract_name_from_urn,
    _extract_platform_from_urn,
    build_embedding_text,
    generate_embedding,
)


class TestBuildEmbeddingText:
    def test_full_metadata(self):
        text = build_embedding_text(
            name="mydb.public.users",
            description="User account data",
            fields=[
                {"name": "id", "description": "Primary key"},
                {"name": "email", "description": "User email address"},
            ],
            tags=["pii", "production"],
            lineage_context=["mydb.public.raw_users", "mydb.public.auth_events"],
        )
        assert "mydb.public.users" in text
        assert "User account data" in text
        assert "Fields: id: Primary key, email: User email address" in text
        assert "Tags: pii, production" in text
        assert "Lineage: upstream of mydb.public.raw_users, mydb.public.auth_events" in text

    def test_minimal(self):
        text = build_embedding_text(name="orders")
        assert text == "orders"

    def test_empty_field_descriptions(self):
        text = build_embedding_text(
            name="orders",
            fields=[
                {"name": "id", "description": ""},
                {"name": "total", "description": "Order total"},
            ],
        )
        assert "Fields: id, total: Order total" in text

    def test_no_tags_or_lineage(self):
        text = build_embedding_text(
            name="orders",
            description="Orders table",
        )
        assert "Tags:" not in text
        assert "Lineage:" not in text


class TestExtractHelpers:
    def test_extract_name_from_urn(self):
        urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
        assert _extract_name_from_urn(urn) == "mydb.public.users"

    def test_extract_name_from_malformed_urn(self):
        assert _extract_name_from_urn("bad_urn") == "bad_urn"

    def test_extract_platform_from_urn(self):
        urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
        assert _extract_platform_from_urn(urn) == "postgres"

    def test_extract_platform_from_malformed_urn(self):
        assert _extract_platform_from_urn("bad_urn") == "unknown"


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_calls_llm_embed(self):
        llm = AsyncMock()
        llm.embed.return_value = [0.1] * 1536

        datahub = AsyncMock()

        # Mock DataHub aspects
        props = MagicMock()
        props.name = "test_table"
        props.description = "A test table"

        schema = MagicMock()
        field = MagicMock()
        field.fieldPath = "id"
        field.description = "Primary key"
        schema.fields = [field]

        tags = MagicMock()
        tag = MagicMock()
        tag.tag = "urn:li:tag:test-tag"
        tags.tags = [tag]

        ownership = MagicMock()
        owner = MagicMock()
        owner.owner = "urn:li:corpuser:alice"
        ownership.owners = [owner]

        datahub.get_aspect.side_effect = [props, schema, tags, ownership]
        datahub.get_upstream_lineage.return_value = []

        urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
        embedding, payload = await generate_embedding(llm, datahub, urn)

        assert len(embedding) == 1536
        llm.embed.assert_called_once()
        embed_text = llm.embed.call_args[0][0]
        assert "test_table" in embed_text
        assert "A test table" in embed_text
        assert payload["dataset_urn"] == urn
        assert payload["platform"] == "postgres"

    @pytest.mark.asyncio
    async def test_handles_missing_aspects(self):
        llm = AsyncMock()
        llm.embed.return_value = [0.0] * 1536

        datahub = AsyncMock()
        datahub.get_aspect.return_value = None
        datahub.get_upstream_lineage.return_value = []

        urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
        embedding, payload = await generate_embedding(llm, datahub, urn)

        assert len(embedding) == 1536
        llm.embed.assert_called_once()
        # Should still produce embedding from URN-extracted name
        embed_text = llm.embed.call_args[0][0]
        assert "mydb.public.users" in embed_text
