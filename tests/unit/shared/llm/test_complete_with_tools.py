"""Tests for LLMClient.complete_with_tools and supporting types.

Derives from:
  - spec/feature/BACKEND.md §LLM Inference Loop
  - /Users/soonmok/.claude/plans/quizzical-hatching-shamir.md §PR1 scope

Groups:
  A – Happy path (single-iteration success)
  B – Retry loop (multi-iteration success)
  C – Max-iteration exhaustion (soft failure)
  D – Wrong-tool calls
  E – No-tool-call response
  F – Caller misconfiguration (fail-fast)
  G – Schema validation in loop (F6 fix)
  H – Narrow exception handling
  I – Settings bounds (F3)
  J – Stub fidelity
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pydantic
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from src.shared.llm.client import LLMClient
from src.shared.llm.loop_trace import LoopResult, LoopTrace
from src.workflows._stubs import StubLLMClient


# ── Shared fixture schema ────────────────────────────────────────────────────


class SimpleSchema(BaseModel):
    """Minimal Pydantic schema used by Groups A-H."""

    name: str
    value: int


def _make_model() -> MagicMock:
    """Return a MagicMock that simulates a LangChain chat model with bind_tools."""
    mock_model = MagicMock()
    bound_model = AsyncMock()
    mock_model.bind_tools.return_value = bound_model
    return mock_model


def _success_tool(name: str = "ontogen_validate") -> MagicMock:
    """A mock LangChain tool that returns {ok: true} on ainvoke."""
    tool = MagicMock()
    tool.name = name
    tool.ainvoke = AsyncMock(return_value={"ok": True})
    return tool


def _ai_tool_call(tool_name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    """Build an AIMessage that contains a single tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": args, "id": call_id}],
    )


# ── Group A: Happy path (single-iteration success) ───────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_a1_single_iter_success_trace(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — loop terminates on first ok:true; returns
    LoopResult(iterations=1, errors_per_iter with one entry, final_errors=[]).
    Plan §PR1: F5 fix — iter_errors for the success iteration is the actual list accumulated,
    not a hardcoded [].  When no errors occurred in iter 1, errors_per_iter[-1] == [].
    The inner list is [] (not None) because F5 initialises iter_errors = [] each iteration.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value
    bound.ainvoke = AsyncMock(
        return_value=_ai_tool_call("ontogen_validate", {"name": "alice", "value": 1})
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")
    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "build something",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert isinstance(result, LoopResult)
    assert result.trace.iterations == 1
    assert len(result.trace.errors_per_iter) == 1   # one iteration ran
    assert result.trace.errors_per_iter[0] == []    # no errors that iteration (F5 fix contract)
    assert result.trace.final_errors == []


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_a2_payload_extraction_nested(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — when tool args contain a 'payload' key,
    the returned payload is the inner dict (unwrapped one level).
    Plan §PR1: candidate_payload = args.get('payload', args).
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value
    inner = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        return_value=_ai_tool_call("ontogen_validate", {"payload": inner})
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")
    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "build something",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.payload == inner


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_a3_payload_extraction_flat(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — when tool args are flat (no 'payload' key),
    the whole args dict is the payload.
    Plan §PR1: candidate_payload = args.get('payload', args).
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value
    flat_args = {"name": "bob", "value": 2}
    bound.ainvoke = AsyncMock(
        return_value=_ai_tool_call("ontogen_validate", flat_args)
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")
    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "build something",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.payload == flat_args


# ── Group B: Retry loop (multi-iteration success) ───────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_b1_two_iteration_success_trace(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — 'On errors the model receives the
    consolidated error list as a ToolMessage and revises.'
    After iter-1 validator returns errors, iter-2 returns ok:true.
    LoopTrace.iterations == 2; errors_per_iter has iter-1 errors and empty iter-2 list.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    iter1_args = {"name": "alice", "value": 1}
    iter2_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", iter1_args, "call_1"),
            _ai_tool_call("ontogen_validate", iter2_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    iter1_errors = [{"path": "nodes", "code": "RULE_ERR", "message": "bad node"}]
    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        side_effect=[
            {"ok": False, "errors": iter1_errors},
            {"ok": True},
        ]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "build something",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 2
    assert result.trace.errors_per_iter[0] == iter1_errors
    assert result.trace.errors_per_iter[1] == []
    assert result.trace.final_errors == []


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_b2_tool_message_fed_back_between_iterations(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — 'On errors the model receives the
    consolidated error list as a ToolMessage and revises.'
    Verify a ToolMessage containing the iter-1 errors is in the messages list
    that the model receives on the iter-2 ainvoke call.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    recorded_messages: list[list[Any]] = []

    iter1_errors = [{"path": "nodes", "code": "RULE_ERR", "message": "bad node"}]

    async def capture_ainvoke(messages: list, **kwargs: Any) -> AIMessage:
        recorded_messages.append(list(messages))
        if len(recorded_messages) == 1:
            return _ai_tool_call("ontogen_validate", {"name": "alice", "value": 1}, "call_1")
        return _ai_tool_call("ontogen_validate", {"name": "alice", "value": 1}, "call_2")

    bound.ainvoke = capture_ainvoke

    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        side_effect=[
            {"ok": False, "errors": iter1_errors},
            {"ok": True},
        ]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    await client.complete_with_tools(
        "build something",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    # iter-2 messages must contain a ToolMessage with the iter-1 errors
    iter2_messages = recorded_messages[1]
    tool_messages = [m for m in iter2_messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) >= 1
    content = json.loads(tool_messages[0].content)
    assert content["ok"] is False
    assert content["errors"] == iter1_errors


# ── Group C: Max-iteration exhaustion (soft failure) ────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_c1_exhaustion_returns_last_candidate(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — 'Exhaustion behavior: Soft. The last
    candidate is accepted.' Returns LoopResult(payload=<last_candidate>, iterations=3,
    errors_per_iter has 3 lists, final_errors=<iter-3 errors>).
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    payload_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", payload_args, "call_1"),
            _ai_tool_call("ontogen_validate", payload_args, "call_2"),
            _ai_tool_call("ontogen_validate", payload_args, "call_3"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    rule_errors = [{"path": "nodes", "code": "RULE_ERR", "message": "bad node"}]
    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(return_value={"ok": False, "errors": rule_errors})

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "build something",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
        max_iterations=3,
    )

    assert result.payload == payload_args
    assert result.trace.iterations == 3
    assert len(result.trace.errors_per_iter) == 3
    assert result.trace.final_errors == rule_errors


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_c2_exhaustion_errors_per_iter_length(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — each iteration contributes one entry to
    errors_per_iter; on 3-iteration exhaustion the list has exactly 3 entries.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", {"name": "x", "value": 0}, f"call_{i}")
            for i in range(1, 4)
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        return_value={"ok": False, "errors": [{"path": "", "code": "ERR", "message": "e"}]}
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
        max_iterations=3,
    )

    assert len(result.trace.errors_per_iter) == 3


# ── Group D: Wrong-tool calls ────────────────────────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_d1_wrong_tool_records_error_and_feeds_back(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — calling the wrong tool must produce a
    WRONG_TOOL error recorded in iter_errors and a ToolMessage fed back.
    Plan §PR1: code='WRONG_TOOL', ToolMessage appended with the error.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    # iter-1: wrong tool; iter-2: correct tool with success
    wrong_args = {"some": "data"}
    right_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("bad_tool", wrong_args, "call_1"),
            _ai_tool_call("ontogen_validate", right_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    # iter-1 should have a WRONG_TOOL error (order not part of contract)
    assert any(e["code"] == "WRONG_TOOL" for e in result.trace.errors_per_iter[0])


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_d2_wrong_tool_message_contains_error(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — 'On errors the model receives the
    consolidated error list as a ToolMessage and revises.' WRONG_TOOL error feeds back
    as a ToolMessage so the model can self-correct.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    recorded_messages: list[list[Any]] = []

    async def capture_ainvoke(messages: list, **kwargs: Any) -> AIMessage:
        recorded_messages.append(list(messages))
        if len(recorded_messages) == 1:
            return _ai_tool_call("bad_tool", {"x": 1}, "call_1")
        return _ai_tool_call("ontogen_validate", {"name": "alice", "value": 1}, "call_2")

    bound.ainvoke = capture_ainvoke
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    # The messages seen by iter-2 must contain a ToolMessage with WRONG_TOOL
    iter2_messages = recorded_messages[1]
    tool_messages = [m for m in iter2_messages if isinstance(m, ToolMessage)]
    assert any(
        json.loads(tm.content).get("errors", [{}])[0].get("code") == "WRONG_TOOL"
        for tm in tool_messages
    )


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_d3_wrong_tool_and_right_tool_same_message(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F5 fix: when wrong tool and
    right tool both appear in the same AIMessage and the right tool returns ok:true,
    the iteration succeeds but errors_per_iter[-1] contains the WRONG_TOOL error
    (trace fidelity — not an empty list).
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    # Single AIMessage with two tool_calls: wrong + correct
    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "bad_tool", "args": {"x": 1}, "id": "call_bad"},
            {"name": "ontogen_validate", "args": {"name": "alice", "value": 1}, "id": "call_ok"},
        ],
    )
    bound.ainvoke = AsyncMock(return_value=msg)
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 1
    # F5 fix: errors_per_iter[0] must contain the WRONG_TOOL error, not be empty
    assert any(e["code"] == "WRONG_TOOL" for e in result.trace.errors_per_iter[0])


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_d4_wrong_tool_name_truncated_to_64_chars(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F9: attacker-controlled tool
    name is truncated to 64 chars before being echoed back in the WRONG_TOOL error message.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    long_name = "x" * 200
    right_args = {"name": "alice", "value": 1}
    # iter-1: tool call with excessively long name; iter-2: correct tool
    msg_with_long_name = AIMessage(
        content="",
        tool_calls=[{"name": long_name, "args": {"data": 1}, "id": "call_long"}],
    )
    bound.ainvoke = AsyncMock(
        side_effect=[
            msg_with_long_name,
            _ai_tool_call("ontogen_validate", right_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    # Extract the WRONG_TOOL error message and confirm name is truncated to <=64 chars
    wrong_tool_errors = [
        e for e in result.trace.errors_per_iter[0] if e["code"] == "WRONG_TOOL"
    ]
    assert len(wrong_tool_errors) == 1
    error_message = wrong_tool_errors[0]["message"]
    # The echoed name in "called <name> but only ..." is the truncated version
    echoed_name = error_message.split("called ")[1].split(" but only")[0]
    assert len(echoed_name) <= 64


# ── Group E: No-tool-call response ───────────────────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_e1_no_tool_call_records_no_tool_call_error(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — model must call the tool before returning.
    Text-only response records NO_TOOL_CALL error and appends a corrective HumanMessage.
    Plan §PR1: code='NO_TOOL_CALL'.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    text_response = AIMessage(content="Here is my answer: ...", tool_calls=[])
    right_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            text_response,
            _ai_tool_call("ontogen_validate", right_args, "call_1"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.errors_per_iter[0][0]["code"] == "NO_TOOL_CALL"


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_e2_no_tool_call_appends_corrective_human_message(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — after a text-only response the loop
    appends a corrective HumanMessage telling the model to call the tool.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    recorded_messages: list[list[Any]] = []

    async def capture_ainvoke(messages: list, **kwargs: Any) -> AIMessage:
        recorded_messages.append(list(messages))
        if len(recorded_messages) == 1:
            return AIMessage(content="plain text", tool_calls=[])
        return _ai_tool_call("ontogen_validate", {"name": "alice", "value": 1}, "call_1")

    bound.ainvoke = capture_ainvoke
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    iter2_messages = recorded_messages[1]
    human_messages = [m for m in iter2_messages if isinstance(m, HumanMessage)]
    assert len(human_messages) >= 2  # original prompt + corrective
    corrective = human_messages[-1].content
    assert "ontogen_validate" in corrective


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_e3_exhaustion_on_all_no_tool_calls_returns_empty_payload(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — soft exhaustion when no candidate is
    ever proposed (all text-only responses) returns payload={}.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    text_response = AIMessage(content="Here is my answer", tool_calls=[])
    bound.ainvoke = AsyncMock(
        side_effect=[text_response, text_response, text_response]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
        max_iterations=3,
    )

    assert result.payload == {}


# ── Group F: Caller misconfiguration (fail-fast) ─────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_f1_missing_success_tool_raises_before_loop(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F4 fix: calling
    complete_with_tools with a success_tool_name that is not in the tools list raises
    ValueError BEFORE entering the loop; no ainvoke call should occur.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value
    bound.ainvoke = AsyncMock()
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    other_tool = MagicMock()
    other_tool.name = "some_other_tool"

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")

    with pytest.raises(ValueError, match="success_tool_name"):
        await client.complete_with_tools(
            "prompt",
            tools=[other_tool],
            success_tool_name="missing",
            schema=SimpleSchema,
        )

    bound.ainvoke.assert_not_called()


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_f2_empty_tools_raises(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F4 fix: an empty tools list
    means success_tool_name is never present; raises ValueError before entering the loop.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value
    bound.ainvoke = AsyncMock()
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")

    with pytest.raises(ValueError, match="ontogen_validate"):
        await client.complete_with_tools(
            prompt="x",
            tools=[],
            success_tool_name="ontogen_validate",
            schema=SimpleSchema,
        )

    bound.ainvoke.assert_not_called()


# ── Group G: Schema validation in loop (F6 fix) ──────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_g1_schema_failure_records_schema_error(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F6 fix: tool args that fail
    schema.model_validate append SCHEMA error to iter_errors and a synthetic
    ToolMessage, without invoking the validator tool.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    # value should be int but model sends a string — triggers schema validation failure
    bad_args = {"name": "alice", "value": "not-an-int"}
    # After schema failure on iter-1, iter-2 sends correct args
    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", bad_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(return_value={"ok": True})

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    # iter-1 should have SCHEMA error
    assert any(e["code"] == "SCHEMA" for e in result.trace.errors_per_iter[0])


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_g2_schema_failure_does_not_invoke_validator_tool(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F6 fix: when schema validation
    fails, the validator tool's ainvoke must NOT be called for that iteration.
    The shape layer catches the error before the semantic layer runs.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    bad_args = {"name": "alice", "value": "not-an-int"}
    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", bad_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(return_value={"ok": True})

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    # tool was called only once (iter-2 with good args), not on iter-1 schema failure
    assert tool.ainvoke.call_count == 1


# ── Group H: Narrow exception handling ───────────────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h1_tool_pydantic_validation_error_recovered(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F2 fix (orphan tool-call fix):
    pydantic.ValidationError raised by tool.ainvoke is recovered; iter_errors gets
    ITERATION_ERROR; a synthetic ToolMessage is appended; loop continues.
    Error message is type(exc).__name__ only, not str(exc).
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", good_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    # Build a real pydantic.ValidationError
    class _TinyModel(BaseModel):
        x: int

    try:
        _TinyModel.model_validate({"x": "not-an-int"})
    except pydantic.ValidationError as exc:
        pydantic_exc = exc

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        side_effect=[pydantic_exc, {"ok": True}]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    iter1_errors = result.trace.errors_per_iter[0]
    assert any(e["code"] == "ITERATION_ERROR" for e in iter1_errors)
    # message is type name only, not the full exception string
    iteration_error = next(e for e in iter1_errors if e["code"] == "ITERATION_ERROR")
    assert iteration_error["message"] == "ValidationError"


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h2_tool_runtime_error_propagates(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — non-recoverable exceptions (RuntimeError)
    from tool.ainvoke must propagate out of complete_with_tools; they are NOT swallowed.
    Plan §PR1: only _RECOVERABLE_EXCEPTIONS are caught inside the loop.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        return_value=_ai_tool_call("ontogen_validate", good_args, "call_1")
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")

    with pytest.raises(RuntimeError, match="boom"):
        await client.complete_with_tools(
            "prompt",
            tools=[tool],
            success_tool_name="ontogen_validate",
            schema=SimpleSchema,
        )


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h3_model_ainvoke_runtime_error_propagates(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — non-recoverable exceptions (RuntimeError)
    from model_with_tools.ainvoke must propagate out; they are NOT in _RECOVERABLE_EXCEPTIONS.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value
    bound.ainvoke = AsyncMock(side_effect=RuntimeError("model exploded"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = _success_tool("ontogen_validate")

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")

    with pytest.raises(RuntimeError, match="model exploded"):
        await client.complete_with_tools(
            "prompt",
            tools=[tool],
            success_tool_name="ontogen_validate",
            schema=SimpleSchema,
        )


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h4_tool_keyerror_recovered(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F2 fix: KeyError raised by
    tool.ainvoke is in _RECOVERABLE_EXCEPTIONS; loop records ITERATION_ERROR with
    message 'KeyError', appends synthetic ToolMessage with matching tool_call_id, continues.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", good_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        side_effect=[KeyError("missing"), {"ok": True}]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 2
    iter1_errors = result.trace.errors_per_iter[0]
    assert any(e["code"] == "ITERATION_ERROR" for e in iter1_errors)
    iteration_error = next(e for e in iter1_errors if e["code"] == "ITERATION_ERROR")
    assert iteration_error["message"] == "KeyError"


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h5_tool_attributeerror_recovered(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F2 fix: AttributeError raised by
    tool.ainvoke is in _RECOVERABLE_EXCEPTIONS; loop records ITERATION_ERROR with
    message 'AttributeError', appends synthetic ToolMessage with matching tool_call_id, continues.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", good_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        side_effect=[AttributeError("no such attr"), {"ok": True}]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 2
    iter1_errors = result.trace.errors_per_iter[0]
    assert any(e["code"] == "ITERATION_ERROR" for e in iter1_errors)
    iteration_error = next(e for e in iter1_errors if e["code"] == "ITERATION_ERROR")
    assert iteration_error["message"] == "AttributeError"


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h6_tool_typeerror_recovered(mock_create: MagicMock, mock_embed: MagicMock) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F2 fix: TypeError raised by
    tool.ainvoke is in _RECOVERABLE_EXCEPTIONS; loop records ITERATION_ERROR with
    message 'TypeError', appends synthetic ToolMessage with matching tool_call_id, continues.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", good_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    tool.ainvoke = AsyncMock(
        side_effect=[TypeError("bad type"), {"ok": True}]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 2
    iter1_errors = result.trace.errors_per_iter[0]
    assert any(e["code"] == "ITERATION_ERROR" for e in iter1_errors)
    iteration_error = next(e for e in iter1_errors if e["code"] == "ITERATION_ERROR")
    assert iteration_error["message"] == "TypeError"


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_h7_tool_returns_malformed_json_string_recovered(
    mock_create: MagicMock, mock_embed: MagicMock
) -> None:
    """Spec: BACKEND.md §LLM Inference Loop — Plan §PR1 F2 fix: when tool.ainvoke
    returns a string that is not valid JSON, the json.loads branch raises JSONDecodeError
    which is in _RECOVERABLE_EXCEPTIONS; loop records ITERATION_ERROR with message
    'JSONDecodeError' and continues to the next iteration.
    """
    mock_model = _make_model()
    bound = mock_model.bind_tools.return_value

    good_args = {"name": "alice", "value": 1}
    bound.ainvoke = AsyncMock(
        side_effect=[
            _ai_tool_call("ontogen_validate", good_args, "call_1"),
            _ai_tool_call("ontogen_validate", good_args, "call_2"),
        ]
    )
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    tool = MagicMock()
    tool.name = "ontogen_validate"
    # First call returns malformed JSON string; second call returns success dict
    tool.ainvoke = AsyncMock(
        side_effect=["not valid json {{{", {"ok": True}]
    )

    client = LLMClient(provider="openai", api_key="key", model="gpt-4o")
    result = await client.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 2
    iter1_errors = result.trace.errors_per_iter[0]
    assert any(e["code"] == "ITERATION_ERROR" for e in iter1_errors)
    iteration_error = next(e for e in iter1_errors if e["code"] == "ITERATION_ERROR")
    assert iteration_error["message"] == "JSONDecodeError"


# ── Group I: Settings bounds (F3) ────────────────────────────────────────────


def test_i1_runtime_config_default_max_iterations() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — 'Max iterations: 3 per service'.
    Factory default for ontogen_llm_max_iterations is 3.
    """
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS

    assert RUNTIME_CONFIG_DEFAULTS["ontogen_llm_max_iterations"] == 3


def test_i2_patch_request_accepts_min_boundary() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — min iterations is 1 (ge=1).
    RuntimeConfPatchRequest must accept value 1 without validation error.
    """
    from src.api.schemas.admin import RuntimeConfPatchRequest

    req = RuntimeConfPatchRequest(ontogen_llm_max_iterations=1)
    assert req.ontogen_llm_max_iterations == 1


def test_i3_patch_request_accepts_max_boundary() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — max iterations is 20 (le=20).
    RuntimeConfPatchRequest must accept value 20 without validation error.
    """
    from src.api.schemas.admin import RuntimeConfPatchRequest

    req = RuntimeConfPatchRequest(ontogen_llm_max_iterations=20)
    assert req.ontogen_llm_max_iterations == 20


def test_i4_patch_request_rejects_zero() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — zero iterations is invalid (ge=1).
    RuntimeConfPatchRequest must raise pydantic.ValidationError for value 0.
    """
    from src.api.schemas.admin import RuntimeConfPatchRequest

    with pytest.raises(pydantic.ValidationError):
        RuntimeConfPatchRequest(ontogen_llm_max_iterations=0)


def test_i5_patch_request_rejects_negative() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — negative iterations are invalid (ge=1).
    RuntimeConfPatchRequest must raise pydantic.ValidationError for value -1.
    """
    from src.api.schemas.admin import RuntimeConfPatchRequest

    with pytest.raises(pydantic.ValidationError):
        RuntimeConfPatchRequest(ontogen_llm_max_iterations=-1)


def test_i6_patch_request_rejects_above_max() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — iterations above 20 are invalid (le=20).
    RuntimeConfPatchRequest must raise pydantic.ValidationError for value 21.
    """
    from src.api.schemas.admin import RuntimeConfPatchRequest

    with pytest.raises(pydantic.ValidationError):
        RuntimeConfPatchRequest(ontogen_llm_max_iterations=21)


# ── Group J: Stub fidelity ────────────────────────────────────────────────────


async def test_j1_stub_returns_loop_result_with_schema_valid_payload() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — 'Test mode: StubLLMClient.complete_with_tools
    returns one schema-valid empty payload on iteration 1; the loop never iterates.'
    The returned payload dict must be shape-valid for the schema.
    """
    stub = StubLLMClient()
    tool = _success_tool("ontogen_validate")

    result = await stub.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert isinstance(result, LoopResult)
    assert result.trace.iterations == 1
    assert result.trace.final_errors == []
    # payload must be parseable as SimpleSchema
    parsed = SimpleSchema.model_validate(result.payload)
    assert parsed is not None


async def test_j2_stub_returns_single_iteration_trace() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — stub loop never iterates; iterations == 1.
    Plan §PR1: StubLLMClient.complete_with_tools returns LoopTrace(iterations=1,
    errors_per_iter=[], final_errors=[]).
    """
    stub = StubLLMClient()
    tool = _success_tool("ontogen_validate")

    result = await stub.complete_with_tools(
        "prompt",
        tools=[tool],
        success_tool_name="ontogen_validate",
        schema=SimpleSchema,
    )

    assert result.trace.iterations == 1
    # Stub returns empty outer list; live impl returns [[]] for 1-iter success — plan §PR1.
    assert result.trace.errors_per_iter == []
    assert result.trace.final_errors == []


async def test_j3_stub_ignores_all_kwargs() -> None:
    """Spec: BACKEND.md §LLM Inference Loop — stub ignores tools, success_tool_name,
    system, max_iterations, temperature; no error raised regardless of values.
    """
    stub = StubLLMClient()
    other_tool = MagicMock()
    other_tool.name = "some_other_tool"

    # Even with a mismatched success_tool_name the stub does not raise
    result = await stub.complete_with_tools(
        "prompt",
        tools=[other_tool],
        success_tool_name="nonexistent",
        schema=SimpleSchema,
        system="be helpful",
        max_iterations=1,
        temperature=0.7,
    )

    assert isinstance(result, LoopResult)
