"""Provider-agnostic LLM client using LangChain."""

import json
from typing import Any

import pydantic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from src.shared.llm.loop_trace import LoopResult, LoopTrace

# Narrow set of exceptions from which we can recover inside the tool loop.
# Network/auth/timeout errors from the underlying provider must bubble up.
_RECOVERABLE_EXCEPTIONS = (
    pydantic.ValidationError,
    json.JSONDecodeError,
    KeyError,
    AttributeError,
    TypeError,
)


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
        messages: list[Any] = []
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
    ) -> dict[str, Any]:
        json_instruction = "You must respond with valid JSON only. No other text."
        full_system = f"{system}\n\n{json_instruction}" if system else json_instruction

        if schema is not None:
            try:
                structured = self._model.with_structured_output(schema)
                messages: list[Any] = []
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

    async def complete_with_tools(
        self,
        prompt: str,
        *,
        tools: list,
        success_tool_name: str,
        schema: type[BaseModel],
        system: str = "",
        max_iterations: int = 3,
        temperature: float = 0.0,
    ) -> LoopResult:
        """Run a bounded ReAct loop that terminates when the model calls
        ``success_tool_name`` with no validation errors.

        The model must call the designated tool to submit its candidate payload;
        if it returns text only or calls the wrong tool, a corrective message is
        appended and the loop continues.  On exhaustion the last proposed
        candidate is returned with ``final_errors`` populated.

        ``schema`` is enforced inside the validator tool; this layer also
        catches malformed payloads before tool invocation.
        """
        # F4 — Fail-fast on programming error: success tool must be in tools list.
        if not any(getattr(t, "name", None) == success_tool_name for t in tools):
            raise ValueError(
                f"success_tool_name {success_tool_name!r} not found in tools list"
            )

        messages: list[Any] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))

        model_with_tools = self._model.bind_tools(tools)

        errors_per_iter: list[list[dict[str, str]]] = []
        last_payload: dict[str, Any] = {}
        last_iter_errors: list[dict[str, str]] = []

        for iteration in range(1, max_iterations + 1):
            iter_errors: list[dict[str, str]] = []

            # Outer try: catches model-call failures (no tool_call_id to recover).
            # Network/auth errors bubble — only recoverable exceptions are caught.
            try:
                response: AIMessage = await model_with_tools.ainvoke(
                    messages, temperature=temperature
                )
            except _RECOVERABLE_EXCEPTIONS as exc:
                iter_errors.append(
                    {
                        "path": "",
                        "code": "ITERATION_ERROR",
                        "message": type(exc).__name__,
                    }
                )
                errors_per_iter.append(iter_errors)
                last_iter_errors = iter_errors
                continue

            messages.append(response)

            tool_calls = getattr(response, "tool_calls", []) or []

            if not tool_calls:
                iter_errors.append(
                    {
                        "path": "",
                        "code": "NO_TOOL_CALL",
                        "message": f"must call {success_tool_name} before returning",
                    }
                )
                errors_per_iter.append(iter_errors)
                last_iter_errors = iter_errors
                messages.append(
                    HumanMessage(
                        content=f"You must call the `{success_tool_name}` tool with your proposed payload. Do not return plain text."
                    )
                )
                continue

            for tool_call in tool_calls:
                # F9 — cap attacker-controlled tool name before echoing it back.
                name: str = (tool_call.get("name") or "")[:64]
                args: dict[str, Any] = tool_call["args"]
                tool_call_id: str = tool_call["id"]

                if name != success_tool_name:
                    iter_errors.append(
                        {
                            "path": "",
                            "code": "WRONG_TOOL",
                            "message": f"called {name} but only {success_tool_name} is permitted",
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "ok": False,
                                    "errors": [iter_errors[-1]],
                                }
                            ),
                            tool_call_id=tool_call_id,
                        )
                    )
                    continue

                # Model called the correct tool — extract candidate payload.
                # F4 — tool_obj is guaranteed present (validated at function entry).
                tool_obj = next(t for t in tools if t.name == success_tool_name)

                candidate_payload: dict[str, Any] = args.get("payload", args)
                last_payload = candidate_payload

                # F6 — Pre-validate shape against schema before invoking tool.
                # schema is also enforced inside the validator tool; this layer
                # catches malformed payloads before tool invocation.
                try:
                    schema.model_validate(candidate_payload)
                except pydantic.ValidationError as schema_exc:
                    schema_errors: list[dict[str, str]] = [
                        {
                            "path": ".".join(str(loc) for loc in e["loc"]),
                            "code": "SCHEMA",
                            "message": e["msg"],
                        }
                        for e in schema_exc.errors()
                    ]
                    iter_errors.extend(schema_errors)
                    messages.append(
                        ToolMessage(
                            content=json.dumps({"ok": False, "errors": schema_errors}),
                            tool_call_id=tool_call_id,
                        )
                    )
                    continue

                # F2 — Per-tool-call try/except so orphan tool_calls are
                # recovered with a synthetic ToolMessage before continuing.
                try:
                    tool_result: dict[str, Any] = await tool_obj.ainvoke(args)
                    if isinstance(tool_result, str):
                        tool_result = json.loads(tool_result)
                except _RECOVERABLE_EXCEPTIONS as exc:
                    tool_err: dict[str, str] = {
                        "path": "",
                        "code": "ITERATION_ERROR",
                        "message": type(exc).__name__,
                    }
                    iter_errors.append(tool_err)
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {"ok": False, "errors": [tool_err]}
                            ),
                            tool_call_id=tool_call_id,
                        )
                    )
                    continue

                if tool_result.get("ok") is True:
                    # F5 — preserve iter_errors accumulated before success in this
                    # iteration (e.g. a WRONG_TOOL earlier in the same AIMessage).
                    errors_per_iter.append(iter_errors)
                    return LoopResult(
                        payload=candidate_payload,
                        trace=LoopTrace(
                            iterations=iteration,
                            errors_per_iter=errors_per_iter,
                            final_errors=[],
                        ),
                    )

                # Validation returned errors — feed back and continue.
                tool_errors: list[dict[str, str]] = tool_result.get("errors", [])
                iter_errors.extend(tool_errors)
                messages.append(
                    ToolMessage(
                        content=json.dumps({"ok": False, "errors": tool_errors}),
                        tool_call_id=tool_call_id,
                    )
                )

            errors_per_iter.append(iter_errors)
            last_iter_errors = iter_errors

        return LoopResult(
            payload=last_payload,
            trace=LoopTrace(
                iterations=max_iterations,
                errors_per_iter=errors_per_iter,
                final_errors=last_iter_errors,
            ),
        )


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
