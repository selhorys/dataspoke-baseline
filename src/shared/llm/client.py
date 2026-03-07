"""Provider-agnostic LLM client using LangChain."""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel


class LLMClient:
    """Provider-agnostic LLM client."""

    def __init__(self, provider: str, api_key: str, model: str) -> None:
        self._provider = provider
        self._model = model
        self._chat_model = self._build_chat_model(provider, api_key, model)
        self._embeddings = self._build_embeddings(provider, api_key)

    @staticmethod
    def _build_chat_model(provider: str, api_key: str, model: str):  # noqa: ANN205
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=model, api_key=api_key)
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(model=model, api_key=api_key)
        raise ValueError(f"Unsupported LLM provider: {provider}")

    @staticmethod
    def _build_embeddings(provider: str, api_key: str):  # noqa: ANN205
        if provider == "openai":
            from langchain_openai import OpenAIEmbeddings

            return OpenAIEmbeddings(api_key=api_key)
        if provider == "anthropic":
            # Anthropic does not provide embeddings; fall back to None
            return None
        raise ValueError(f"Unsupported embedding provider: {provider}")

    async def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        """Single completion. Returns raw text."""
        messages = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))

        response = await self._chat_model.ainvoke(messages, temperature=temperature)
        return str(response.content)

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: type[BaseModel] | None = None,
    ) -> dict:
        """Completion with JSON output. Optionally validate against a Pydantic schema."""
        json_instruction = "Respond with valid JSON only, no markdown or explanation."
        full_system = f"{system}\n{json_instruction}" if system else json_instruction

        raw = await self.complete(prompt, system=full_system)

        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # drop opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        data = json.loads(text)
        if schema is not None:
            schema.model_validate(data)
        return data

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embedding vectors for the given texts."""
        if self._embeddings is None:
            raise NotImplementedError(f"Embedding not available for provider '{self._provider}'")
        return await self._embeddings.aembed_documents(texts)
