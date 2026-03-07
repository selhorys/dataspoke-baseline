"""Unit tests for LLM client wrapper."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from src.shared.llm.client import LLMClient


class SampleSchema(BaseModel):
    name: str
    value: int


@patch("src.shared.llm.client._create_chat_model")
def test_init_openai_provider(mock_create) -> None:
    mock_create.return_value = MagicMock()
    LLMClient(provider="openai", api_key="key", model="gpt-4")
    mock_create.assert_called_once_with("openai", "key", "gpt-4")


@patch("src.shared.llm.client._create_chat_model")
def test_init_google_provider(mock_create) -> None:
    mock_create.return_value = MagicMock()
    LLMClient(provider="google", api_key="key", model="gemini-pro")
    mock_create.assert_called_once_with("google", "key", "gemini-pro")


@patch("src.shared.llm.client._create_chat_model")
def test_init_anthropic_provider(mock_create) -> None:
    mock_create.return_value = MagicMock()
    LLMClient(provider="anthropic", api_key="key", model="claude-3")
    mock_create.assert_called_once_with("anthropic", "key", "claude-3")


def test_init_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        LLMClient(provider="foo", api_key="key", model="bar")


@patch("src.shared.llm.client._create_chat_model")
async def test_complete_returns_text(mock_create) -> None:
    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = AIMessage(content="Hello!")
    mock_create.return_value = mock_model

    client = LLMClient(provider="openai", api_key="key", model="gpt-4")
    result = await client.complete("Say hello")
    assert result == "Hello!"


@patch("src.shared.llm.client._create_chat_model")
async def test_complete_with_system_prompt(mock_create) -> None:
    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = AIMessage(content="response")
    mock_create.return_value = mock_model

    client = LLMClient(provider="openai", api_key="key", model="gpt-4")
    await client.complete("prompt", system="be helpful")

    messages = mock_model.ainvoke.call_args[0][0]
    assert len(messages) == 2
    assert messages[0].content == "be helpful"


@patch("src.shared.llm.client._create_chat_model")
async def test_complete_json_returns_dict(mock_create) -> None:
    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = AIMessage(content='{"key": "value"}')
    mock_create.return_value = mock_model

    client = LLMClient(provider="openai", api_key="key", model="gpt-4")
    result = await client.complete_json("Give me JSON")
    assert result == {"key": "value"}


@patch("src.shared.llm.client._create_chat_model")
async def test_complete_json_with_schema_validates(mock_create) -> None:
    mock_model = AsyncMock()
    mock_model.with_structured_output.side_effect = NotImplementedError
    mock_model.ainvoke.return_value = AIMessage(content=json.dumps({"name": "test", "value": 42}))
    mock_create.return_value = mock_model

    client = LLMClient(provider="openai", api_key="key", model="gpt-4")
    result = await client.complete_json("Give me data", schema=SampleSchema)
    assert result == {"name": "test", "value": 42}


@patch("src.shared.llm.client._create_chat_model")
async def test_complete_json_invalid_schema_raises(mock_create) -> None:
    mock_model = AsyncMock()
    mock_model.with_structured_output.side_effect = NotImplementedError
    mock_model.ainvoke.return_value = AIMessage(
        content=json.dumps({"name": "test", "value": "not_an_int"})
    )
    mock_create.return_value = mock_model

    client = LLMClient(provider="openai", api_key="key", model="gpt-4")
    with pytest.raises(Exception):
        await client.complete_json("Give me data", schema=SampleSchema)
