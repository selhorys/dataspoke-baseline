"""Unit tests for Langfuse instrumentation in LLMClient.

Spec: spec/feature/BACKEND_LLM.md §Observability
      (Langfuse callback attach/skip behaviour;
       session_id/metadata threading into ainvoke)

Groups:
  A – __init__ without langfuse settings → no config attached to ainvoke
  B – __init__ with all three langfuse settings → handler is constructed
  C – __init__ with partial settings → no config attached to ainvoke
  F – langfuse_session_id cannot be overridden by caller metadata
  G – complete_with_tools threads session_id/metadata into ainvoke
  H – complete() does not attach config when no handler
"""

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from src.shared.llm.client import LLMClient

# ── Minimal schema for complete_with_tools tests ──────────────────────────────


class _MinimalSchema(BaseModel):
    value: str = "ok"


# ── Env isolation fixture ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any LANGFUSE_* env vars at test entry to prevent singleton state leakage.

    Ensures no ambient env var causes _langfuse_handler to be non-None when
    the test expects None, and vice versa.

    Spec: BACKEND_LLM.md §Observability §Process environment — Langfuse activates
    only when all three DATASPOKE_DEV_LANGFUSE_* vars are explicitly set.
    """
    for key in list(os.environ.keys()):
        if key.startswith("LANGFUSE_"):
            monkeypatch.delenv(key, raising=False)


# ─────────────────────────────────────────────────────────────────────────────
# Group A: __init__ without langfuse settings → no config attached to ainvoke
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_init_without_langfuse_no_config_in_ainvoke(mock_create, mock_embed) -> None:
    """complete() on a client without langfuse kwargs must NOT pass config= to ainvoke.

    Spec: BACKEND_LLM.md §Observability — 'When env not configured, no callback
    attached — zero overhead, no failure.'
    """
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="hi"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    client = LLMClient(provider="openai", api_key="sk-fake", model="gpt-4o-mini")

    await client.complete("hello")

    call_kwargs = mock_model.ainvoke.call_args.kwargs
    assert "config" not in call_kwargs, (
        f"ainvoke must NOT receive config= when Langfuse is not configured; "
        f"kwargs={call_kwargs!r}. "
        "Spec: BACKEND_LLM.md §Observability — zero overhead when not configured"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group B: __init__ with all three langfuse settings → handler is constructed
# ─────────────────────────────────────────────────────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
def test_init_with_all_langfuse_settings_handler_is_constructed(mock_create, mock_embed) -> None:
    """LLMClient with all three langfuse kwargs must construct a non-None handler.

    Regression test for the v4 SDK bug: CallbackHandler rejects secret_key/host as
    kwargs (only public_key is accepted).  The correct path goes through Langfuse(...)
    singleton first, then CallbackHandler(public_key=...) only.  If that code path is
    broken (e.g., CallbackHandler gets secret_key/host), this test raises TypeError.

    Spec: BACKEND_LLM.md §Observability — Langfuse(public_key, secret_key, host) registers
    the singleton; CallbackHandler(public_key=public_key) wires the LangChain callback.
    """
    mock_create.return_value = MagicMock()
    mock_embed.return_value = MagicMock()

    from langfuse.langchain import CallbackHandler

    with (
        patch("langfuse.Langfuse") as mock_langfuse_cls,
        patch("langfuse.langchain.CallbackHandler") as mock_handler_cls,
    ):
        mock_langfuse_cls.return_value = MagicMock()
        fake_handler = MagicMock(spec=CallbackHandler)
        mock_handler_cls.return_value = fake_handler

        # Must not raise — this is the regression contract
        client = LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )

    # Spec contract: a non-None handler is wired when all three settings are present
    assert client._langfuse_handler is not None, (
        "Expected _langfuse_handler to be set when all three langfuse kwargs are provided. "
        "Spec: BACKEND_LLM.md §Observability"
    )

    # The stored handler must be the CallbackHandler instance returned by the factory
    assert client._langfuse_handler is fake_handler, (
        "client._langfuse_handler must be the CallbackHandler instance. "
        "Spec: BACKEND_LLM.md §Observability"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group C: __init__ with partial langfuse settings → no config attached to ainvoke
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,label",
    [
        pytest.param(
            {"langfuse_host": "http://localhost:3000"},
            "only host",
            id="only-host",
        ),
        pytest.param(
            {"langfuse_host": "http://localhost:3000", "langfuse_public_key": "pk-test"},
            "host+public_key",
            id="host+pk",
        ),
        pytest.param(
            {"langfuse_host": "http://localhost:3000", "langfuse_secret_key": "sk-test"},
            "host+secret_key",
            id="host+sk",
        ),
        pytest.param(
            {"langfuse_public_key": "pk-test", "langfuse_secret_key": "sk-test"},
            "pk+sk (no host)",
            id="pk+sk-no-host",
        ),
        pytest.param(
            {"langfuse_public_key": "pk-test"},
            "only public_key",
            id="only-pk",
        ),
        pytest.param(
            {"langfuse_secret_key": "sk-test"},
            "only secret_key",
            id="only-sk",
        ),
    ],
)
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_init_partial_langfuse_settings_no_config_in_ainvoke(
    mock_create, mock_embed, kwargs: dict, label: str
) -> None:
    """complete() on a client with partial langfuse settings must NOT pass config= to ainvoke.

    No partial-configuration limp mode — all three keys must be present.

    Spec: BACKEND_LLM.md §Observability — 'When env not configured, no callback attached.'
    """
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="hi"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    client = LLMClient(provider="openai", api_key="sk-fake", model="gpt-4o-mini", **kwargs)

    await client.complete("hello")

    call_kwargs = mock_model.ainvoke.call_args.kwargs
    assert "config" not in call_kwargs, (
        f"ainvoke must NOT receive config= for partial config ({label}); "
        f"kwargs={call_kwargs!r}. "
        "Spec: BACKEND_LLM.md §Observability — no partial-configuration limp mode"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group F: langfuse_session_id cannot be overridden by caller metadata
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_complete_session_id_cannot_be_tampered(mock_create, mock_embed) -> None:
    """langfuse_session_id derived from session_id arg must override any caller-supplied value.

    Spec: BACKEND_LLM.md §Observability — 'Set after caller metadata so
    langfuse_session_id cannot be overridden by the caller.'
    Spec: BACKEND_LLM.md §Observability — RunnableConfig.metadata.langfuse_session_id
    set equal to the ontogen run_id; session_id argument takes precedence.
    """
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="ok"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    with (
        patch("langfuse.Langfuse"),
        patch("langfuse.langchain.CallbackHandler", return_value=MagicMock()),
    ):
        client = LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )

    await client.complete(
        "test prompt",
        session_id="real-id",
        metadata={"langfuse_session_id": "tampered", "actor": "producer"},
    )

    call_kwargs = mock_model.ainvoke.call_args.kwargs
    assert "config" in call_kwargs, (
        "ainvoke must receive config= when Langfuse handler is set. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    cfg_metadata = call_kwargs["config"].get("metadata") or {}
    assert cfg_metadata["langfuse_session_id"] == "real-id", (
        f"langfuse_session_id must be 'real-id' (from session_id arg) not 'tampered'; "
        f"got {cfg_metadata.get('langfuse_session_id')!r}. "
        "Spec: BACKEND_LLM.md §Observability — session_id cannot be overridden by caller"
    )
    assert cfg_metadata.get("actor") == "producer", (
        f"Caller metadata must be included; 'actor' missing from {cfg_metadata!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group G: complete_with_tools threads session_id/metadata into ainvoke
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_complete_with_tools_threads_session_id_and_metadata(mock_create, mock_embed) -> None:
    """complete_with_tools passes session_id/metadata into ainvoke via RunnableConfig.

    Spec: BACKEND_LLM.md §Observability — 'Tag traces with session_id=run_id,
    metadata={"actor": "producer|reviewer", "turn": N}.'
    Spec: BACKEND_LLM.md §Observability — RunnableConfig.metadata.langfuse_session_id
    set equal to the ontogen run_id.

    Asserts that ainvoke is called with config= containing callbacks=[handler]
    and metadata with langfuse_session_id="sid" and actor="producer".
    """
    fake_tool = MagicMock()
    fake_tool.name = "ontogen_validate"
    fake_tool.ainvoke = AsyncMock(return_value={"ok": True})

    # Set up mock model with tool binding
    mock_model = MagicMock()
    mock_embed_obj = MagicMock()
    mock_create.return_value = mock_model
    mock_embed.return_value = mock_embed_obj

    fake_handler = MagicMock()
    fake_handler_cls = MagicMock(return_value=fake_handler)

    # The bound-tools model returns a valid tool call response
    mock_bound = MagicMock()
    tool_call_response = AIMessage(
        content="",
        tool_calls=[{
            "name": "ontogen_validate",
            "args": {"value": "ok"},
            "id": "call-001",
        }],
    )
    mock_bound.ainvoke = AsyncMock(return_value=tool_call_response)
    mock_model.bind_tools = MagicMock(return_value=mock_bound)

    # Schema validates the tool args
    class _Schema(BaseModel):
        value: str = "ok"

    fake_tool.ainvoke = AsyncMock(return_value={"ok": True})

    with (
        patch("langfuse.Langfuse"),
        patch("langfuse.langchain.CallbackHandler", fake_handler_cls),
    ):
        client = LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )

    await client.complete_with_tools(
        "produce ontology for these datasets",
        tools=[fake_tool],
        success_tool_name="ontogen_validate",
        schema=_Schema,
        session_id="sid",
        metadata={"actor": "producer", "turn": 0},
    )

    # ainvoke was called on the bound model
    mock_bound.ainvoke.assert_called_once()
    call_kwargs: dict[str, Any] = mock_bound.ainvoke.call_args.kwargs

    # config= must be present in the ainvoke kwargs
    assert "config" in call_kwargs, (
        f"ainvoke must receive config= when Langfuse handler is set; kwargs={call_kwargs!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )

    cfg = call_kwargs["config"]
    cfg_callbacks = cfg.get("callbacks") or []
    assert fake_handler in cfg_callbacks, (
        f"config['callbacks'] must include the Langfuse handler; got {cfg_callbacks!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )

    cfg_metadata = cfg.get("metadata") or {}
    assert cfg_metadata.get("langfuse_session_id") == "sid", (
        f"config['metadata']['langfuse_session_id'] must be 'sid'; got {cfg_metadata!r}. "
        "Spec: BACKEND_LLM.md §Observability — RunnableConfig.metadata.langfuse_session_id"
    )
    assert cfg_metadata.get("actor") == "producer", (
        f"config['metadata']['actor'] must be 'producer'; got {cfg_metadata!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group I: environment_tag → Langfuse() environment; project_id → trace metadata
# ─────────────────────────────────────────────────────────────────────────────


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
def test_environment_tag_passed_to_langfuse_singleton(mock_create, mock_embed) -> None:
    """langfuse_environment is forwarded to the Langfuse(...) singleton as environment=.

    The configured DataHub/Langfuse peripheral ``environment_tag`` segments traces
    by environment within one project — it must reach the Langfuse SDK singleton.

    Spec: spec/feature/BACKEND_LLM.md §Observability — trace ``environment`` = the
    configured ``environment_tag`` (lines ~399-400).
    """
    mock_create.return_value = MagicMock()
    mock_embed.return_value = MagicMock()

    with (
        patch("langfuse.Langfuse") as mock_langfuse_cls,
        patch("langfuse.langchain.CallbackHandler", return_value=MagicMock()),
    ):
        LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            langfuse_environment="production",
            langfuse_project_id="imazon-metadata",
        )

    mock_langfuse_cls.assert_called_once()
    call_kwargs = mock_langfuse_cls.call_args.kwargs
    assert call_kwargs.get("environment") == "production", (
        f"Langfuse(...) must be called with environment='production'; got {call_kwargs!r}. "
        "Spec: spec/feature/BACKEND_LLM.md §Observability — environment_tag → trace environment."
    )


@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
def test_environment_tag_omitted_when_unset(mock_create, mock_embed) -> None:
    """When environment_tag is unset, Langfuse(...) receives no environment kwarg.

    Absence omits the optional field — tracing still works without it.

    Spec: spec/feature/BACKEND_LLM.md §Observability — both optional; absence omits.
    """
    mock_create.return_value = MagicMock()
    mock_embed.return_value = MagicMock()

    with (
        patch("langfuse.Langfuse") as mock_langfuse_cls,
        patch("langfuse.langchain.CallbackHandler", return_value=MagicMock()),
    ):
        LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            # no langfuse_environment
        )

    call_kwargs = mock_langfuse_cls.call_args.kwargs
    assert "environment" not in call_kwargs, (
        f"Langfuse(...) must omit environment= when unset; got {call_kwargs!r}. "
        "Spec: spec/feature/BACKEND_LLM.md §Observability — absence omits."
    )


@pytest.mark.asyncio
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_project_id_surfaced_as_trace_metadata(mock_create, mock_embed) -> None:
    """complete() surfaces the configured project_id as config.metadata.project_id.

    Spec: spec/feature/BACKEND_LLM.md §Observability — ``metadata.project_id`` =
    the configured ``project_id`` when the Langfuse peripheral setting is present
    (lines ~399-400).
    """
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="ok"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    with (
        patch("langfuse.Langfuse"),
        patch("langfuse.langchain.CallbackHandler", return_value=MagicMock()),
    ):
        client = LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            langfuse_project_id="imazon-metadata",
        )

    await client.complete("hello", session_id="run-1")

    call_kwargs = mock_model.ainvoke.call_args.kwargs
    assert "config" in call_kwargs, (
        "ainvoke must receive config= when Langfuse handler is set. "
        "Spec: spec/feature/BACKEND_LLM.md §Observability."
    )
    cfg_metadata = call_kwargs["config"].get("metadata") or {}
    assert cfg_metadata.get("project_id") == "imazon-metadata", (
        f"config.metadata.project_id must equal the configured project_id; got {cfg_metadata!r}. "
        "Spec: spec/feature/BACKEND_LLM.md §Observability — project_id → trace metadata."
    )


@pytest.mark.asyncio
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_project_id_omitted_from_metadata_when_unset(mock_create, mock_embed) -> None:
    """complete() does not inject a project_id metadata key when project_id is unset.

    Spec: spec/feature/BACKEND_LLM.md §Observability — both optional; absence omits.
    """
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="ok"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    with (
        patch("langfuse.Langfuse"),
        patch("langfuse.langchain.CallbackHandler", return_value=MagicMock()),
    ):
        client = LLMClient(
            provider="openai",
            api_key="sk-fake",
            model="gpt-4o-mini",
            langfuse_host="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            # no langfuse_project_id
        )

    await client.complete("hello", session_id="run-1")

    cfg_metadata = mock_model.ainvoke.call_args.kwargs["config"].get("metadata") or {}
    assert "project_id" not in cfg_metadata, (
        f"project_id must be absent from metadata when unset; got {cfg_metadata!r}. "
        "Spec: spec/feature/BACKEND_LLM.md §Observability — absence omits."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group H: complete() does not attach config when no handler
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.shared.llm.client._create_embeddings_model")
@patch("src.shared.llm.client._create_chat_model")
async def test_complete_does_not_attach_config_when_no_handler(mock_create, mock_embed) -> None:
    """complete() must omit config= kwarg entirely when _langfuse_handler is None.

    Replaces direct testing of private _runnable_config. The spec contract is at
    the complete()/complete_with_tools() public boundary; the no-handler short-circuit
    is verified by asserting config is absent from ainvoke's kwargs.

    Spec: BACKEND_LLM.md §Observability — 'When env not configured, no callback
    attached — zero overhead, no failure.'
    """
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(content="hello"))
    mock_create.return_value = mock_model
    mock_embed.return_value = MagicMock()

    client = LLMClient(provider="openai", api_key="sk-fake", model="gpt-4o-mini")
    # No langfuse kwargs — handler must be None
    assert client._langfuse_handler is None, (
        "Precondition: _langfuse_handler must be None when no langfuse kwargs are given."
    )

    await client.complete(
        "hello",
        session_id="abc-session",
        metadata={"actor": "producer"},
    )

    call_kwargs = mock_model.ainvoke.call_args.kwargs
    assert "config" not in call_kwargs, (
        f"ainvoke must NOT receive config= when handler is None; got {call_kwargs!r}. "
        "Spec: BACKEND_LLM.md §Observability — zero overhead when Langfuse not configured"
    )
