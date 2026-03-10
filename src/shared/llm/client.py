"""Provider-agnostic LLM client using LangChain."""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel


class LLMClient:
    """LangChain-based LLM client supporting multiple providers."""

    def __init__(self, provider: str, api_key: str, model: str) -> None:
        self._provider = provider.lower()
        self._api_key = api_key
        self._model = _create_chat_model(provider, api_key, model)
        self._embeddings = _create_embeddings_model(self._provider, self._api_key)

    async def embed(self, text: str) -> list[float]:
        """Generate a vector embedding for the given text."""
        result = await self._embeddings.aembed_query(text)
        return result

    async def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        messages = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))
        response = await self._model.ainvoke(messages, temperature=temperature)
        return str(response.content)

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: type[BaseModel] | None = None,
    ) -> dict:
        json_instruction = "You must respond with valid JSON only. No other text."
        full_system = f"{system}\n\n{json_instruction}" if system else json_instruction

        if schema is not None:
            try:
                structured = self._model.with_structured_output(schema)
                messages = []
                if system:
                    messages.append(SystemMessage(content=system))
                messages.append(HumanMessage(content=prompt))
                result = await structured.ainvoke(messages)
                if isinstance(result, BaseModel):
                    return result.model_dump()
                return dict(result)  # type: ignore[arg-type]
            except (NotImplementedError, AttributeError):
                pass

        raw = await self.complete(prompt, system=full_system)
        parsed = json.loads(raw)
        if schema is not None:
            validated = schema.model_validate(parsed)
            return validated.model_dump()
        return parsed  # type: ignore[return-value]


def _create_chat_model(provider: str, api_key: str, model: str):  # type: ignore[no-untyped-def]
    provider_lower = provider.lower()
    if provider_lower == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=api_key)  # type: ignore[arg-type]
    elif provider_lower in ("google", "gemini"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model, google_api_key=api_key)  # type: ignore[arg-type]
    elif provider_lower == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_key)  # type: ignore[arg-type]
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def _create_embeddings_model(provider: str, api_key: str):  # type: ignore[no-untyped-def]
    from src.shared.config import EMBEDDING_MODEL_GOOGLE, EMBEDDING_MODEL_OPENAI

    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=EMBEDDING_MODEL_OPENAI, api_key=api_key)  # type: ignore[arg-type]
    elif provider in ("google", "gemini"):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(
            model=EMBEDDING_MODEL_GOOGLE,
            google_api_key=api_key,  # type: ignore[arg-type]
        )
    elif provider == "anthropic":
        # Anthropic does not provide a native embedding API.
        # Default to OpenAI embeddings if an OpenAI key is available via env,
        # otherwise raise a clear error.
        import os

        fallback_key = os.environ.get("DATASPOKE_EMBEDDING_API_KEY", "")
        if fallback_key:
            from langchain_openai import OpenAIEmbeddings

            return OpenAIEmbeddings(model=EMBEDDING_MODEL_OPENAI, api_key=fallback_key)  # type: ignore[arg-type]
        raise ValueError(
            "Anthropic does not provide an embedding API. "
            "Set DATASPOKE_EMBEDDING_API_KEY (OpenAI) to enable embeddings."
        )
    else:
        raise ValueError(f"Unknown embedding provider: {provider}")
