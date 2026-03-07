"""Unit tests for LLMClient — no real LLM API calls needed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


class SampleSchema(BaseModel):
    name: str
    score: float


@pytest.fixture
def mock_chat_model():
    model = AsyncMock()
    return model


@pytest.fixture
def mock_embeddings():
    emb = AsyncMock()
    return emb


def _make_client(mock_chat_model, mock_embeddings):
    with (
        patch("src.shared.llm.client.LLMClient._build_chat_model", return_value=mock_chat_model),
        patch("src.shared.llm.client.LLMClient._build_embeddings", return_value=mock_embeddings),
    ):
        from src.shared.llm.client import LLMClient

        return LLMClient(provider="openai", api_key="test-key", model="gpt-4o")


@pytest.mark.asyncio
async def test_complete_returns_text(mock_chat_model, mock_embeddings):
    response = MagicMock()
    response.content = "Hello world"
    mock_chat_model.ainvoke.return_value = response

    client = _make_client(mock_chat_model, mock_embeddings)
    result = await client.complete("Say hello")

    assert result == "Hello world"
    mock_chat_model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_with_system_prompt(mock_chat_model, mock_embeddings):
    response = MagicMock()
    response.content = "response"
    mock_chat_model.ainvoke.return_value = response

    client = _make_client(mock_chat_model, mock_embeddings)
    await client.complete("prompt", system="You are a helper")

    call_args = mock_chat_model.ainvoke.call_args
    messages = call_args[0][0]
    assert len(messages) == 2  # system + human


@pytest.mark.asyncio
async def test_complete_json_returns_dict(mock_chat_model, mock_embeddings):
    response = MagicMock()
    response.content = '{"name": "test", "score": 0.95}'
    mock_chat_model.ainvoke.return_value = response

    client = _make_client(mock_chat_model, mock_embeddings)
    result = await client.complete_json("Generate JSON")

    assert result == {"name": "test", "score": 0.95}


@pytest.mark.asyncio
async def test_complete_json_strips_markdown_fences(mock_chat_model, mock_embeddings):
    response = MagicMock()
    response.content = '```json\n{"name": "test", "score": 0.5}\n```'
    mock_chat_model.ainvoke.return_value = response

    client = _make_client(mock_chat_model, mock_embeddings)
    result = await client.complete_json("Generate JSON")

    assert result == {"name": "test", "score": 0.5}


@pytest.mark.asyncio
async def test_complete_json_validates_schema(mock_chat_model, mock_embeddings):
    response = MagicMock()
    response.content = '{"name": "ok", "score": 1.0}'
    mock_chat_model.ainvoke.return_value = response

    client = _make_client(mock_chat_model, mock_embeddings)
    result = await client.complete_json("Generate JSON", schema=SampleSchema)

    assert result["name"] == "ok"


@pytest.mark.asyncio
async def test_complete_json_invalid_schema_raises(mock_chat_model, mock_embeddings):
    from pydantic import ValidationError

    response = MagicMock()
    response.content = '{"wrong_field": true}'
    mock_chat_model.ainvoke.return_value = response

    client = _make_client(mock_chat_model, mock_embeddings)
    with pytest.raises(ValidationError):
        await client.complete_json("Generate JSON", schema=SampleSchema)


@pytest.mark.asyncio
async def test_embed_returns_vectors(mock_chat_model, mock_embeddings):
    mock_embeddings.aembed_documents.return_value = [[0.1] * 1536, [0.2] * 1536]

    client = _make_client(mock_chat_model, mock_embeddings)
    result = await client.embed(["text1", "text2"])

    assert len(result) == 2
    assert len(result[0]) == 1536
    mock_embeddings.aembed_documents.assert_awaited_once_with(["text1", "text2"])


@pytest.mark.asyncio
async def test_embed_raises_when_no_provider(mock_chat_model):
    client = _make_client(mock_chat_model, None)

    with pytest.raises(NotImplementedError, match="Embedding not available"):
        await client.embed(["text"])


def test_build_chat_model_unsupported_provider():
    from src.shared.llm.client import LLMClient

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        LLMClient._build_chat_model("unsupported", "key", "model")


def test_build_embeddings_unsupported_provider():
    from src.shared.llm.client import LLMClient

    with pytest.raises(ValueError, match="Unsupported embedding provider"):
        LLMClient._build_embeddings("unsupported", "key")
