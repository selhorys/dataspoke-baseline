"""Spot tests for per-request LLM construction from runtime_config.

Concern: Since commit 09f81e4, ``get_metagen_service`` and ``get_ontogen_service``
in ``src/api/dependencies.py`` build the LLM **per request** by calling
``make_llm(provider=rc.llm_provider, model=rc.llm_model)`` where ``rc`` is
freshly read from the DB-backed ``runtime_config`` singleton. The headline
guarantee is: a change to ``/admin/conf`` is honoured on the very next API
request — there is no process-startup caching of the LLM client.

Why spot and not api-wired:
  api-wired tests are black-box REST: they cannot observe which provider/model
  the stub LLM was constructed with (``StubLLMClient`` is model-agnostic).
  Verifying the wiring requires patching ``make_llm`` inside the process and
  inspecting its call arguments — an inherently in-process concern.

This file exercises only dev-env PostgreSQL (for runtime_config); DataHub,
Redis, and pgvector are replaced with sentinels. No real LLM call is made.

spec: src/api/dependencies.py — ``get_metagen_service``, ``get_ontogen_service``
      (per-request LLM construction from RuntimeConfigDTO).
spec: spec/feature/BACKEND.md §Admin Config — runtime_config DB-backed singleton.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from src.backend.admin.config_service import (
    RUNTIME_CONFIG_DEFAULTS,
    invalidate_runtime_config_cache,
    patch_runtime_config,
)


# ── Fixture: async_session is provided by tests/integration/conftest.py ──────
# (session-scoped async_engine + function-scoped async_session)
# No additional DB fixture is needed here.


@pytest_asyncio.fixture
async def restored_runtime_config(async_session) -> AsyncGenerator[None, None]:
    """Restore the runtime_config row to factory defaults after each test.

    Invalidates the process-level cache on entry (prevents a stale cached DTO
    from a concurrently-running test from leaking into this test) and on exit
    (leaves a clean slate for subsequent tests in the session).
    """
    invalidate_runtime_config_cache()
    try:
        yield
    finally:
        await patch_runtime_config(async_session, **RUNTIME_CONFIG_DEFAULTS)
        invalidate_runtime_config_cache()


# ── Sentinels for non-DB dependencies ────────────────────────────────────────

_STUB_DATAHUB = object()
_STUB_CACHE = object()
_STUB_VECTOR = object()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metagen_service_calls_make_llm_with_runtime_config_values(
    async_session,
    restored_runtime_config,
) -> None:
    """get_metagen_service passes runtime_config provider/model to make_llm.

    Sets a known provider/model pair in the DB, then calls the dependency
    provider directly (bypassing HTTP) and asserts that make_llm was invoked
    with exactly those values and that the resulting service's ._llm attribute
    is the object make_llm returned.

    spec: src/api/dependencies.py — get_metagen_service per-request LLM wiring.
    spec: spec/feature/BACKEND.md §Admin Config — provider/model in runtime_config.
    """
    await patch_runtime_config(
        async_session,
        llm_provider="openai",
        llm_model="gpt-4o-test",
    )

    sentinel_llm = object()

    from src.api.dependencies import get_metagen_service

    with patch("src.workflows._common.make_llm", return_value=sentinel_llm) as mock_make_llm:
        service = await get_metagen_service(
            datahub=_STUB_DATAHUB,
            db=async_session,
            cache=_STUB_CACHE,
            vector=_STUB_VECTOR,
        )

    mock_make_llm.assert_called_once()
    assert mock_make_llm.call_args.kwargs["provider"] == "openai"
    assert mock_make_llm.call_args.kwargs["model"] == "gpt-4o-test"
    assert service._llm is sentinel_llm, (
        "MetagenService._llm must be the object returned by make_llm. "
        "spec: src/api/dependencies.py — llm = make_llm(...); MetagenService(..., llm=llm, ...)"
    )


@pytest.mark.asyncio
async def test_get_ontogen_service_calls_make_llm_with_runtime_config_values(
    async_session,
    restored_runtime_config,
) -> None:
    """get_ontogen_service passes runtime_config provider/model to make_llm.

    Analogous to the metagen test above; verifies OntogenService receives the
    correct LLM instance constructed from the current runtime_config row.

    spec: src/api/dependencies.py — get_ontogen_service per-request LLM wiring.
    spec: spec/feature/BACKEND.md §Admin Config — provider/model in runtime_config.
    """
    await patch_runtime_config(
        async_session,
        llm_provider="anthropic",
        llm_model="claude-test",
    )

    sentinel_llm = object()

    from src.api.dependencies import get_ontogen_service

    with patch("src.workflows._common.make_llm", return_value=sentinel_llm) as mock_make_llm:
        service = await get_ontogen_service(
            datahub=_STUB_DATAHUB,
            db=async_session,
            cache=_STUB_CACHE,
            vector=_STUB_VECTOR,
        )

    mock_make_llm.assert_called_once()
    assert mock_make_llm.call_args.kwargs["provider"] == "anthropic"
    assert mock_make_llm.call_args.kwargs["model"] == "claude-test"
    assert service._llm is sentinel_llm, (
        "OntogenService._llm must be the object returned by make_llm. "
        "spec: src/api/dependencies.py — llm = make_llm(...); OntogenService(..., llm=llm, ...)"
    )


@pytest.mark.asyncio
async def test_runtime_config_change_honored_immediately_by_get_metagen_service(
    async_session,
    restored_runtime_config,
) -> None:
    """A runtime_config change is picked up on the very next get_metagen_service call.

    Calls get_metagen_service twice: once with provider "openai"/"gpt-4o-test",
    then again after patching to "anthropic"/"claude-test". The second call must
    construct the LLM with the NEW values — proving there is no per-process LLM
    caching that would suppress the change.

    spec: src/api/dependencies.py module docstring — "LLM clients are NOT
      long-lived on app.state — they are constructed per-request from the
      DB-backed RuntimeConfig so that provider/model changes via /admin/conf
      are honoured immediately".
    """
    from src.api.dependencies import get_metagen_service

    # First call — baseline provider/model
    await patch_runtime_config(
        async_session,
        llm_provider="openai",
        llm_model="gpt-4o-test",
    )

    first_sentinel = object()
    with patch("src.workflows._common.make_llm", return_value=first_sentinel) as mock_first:
        service_first = await get_metagen_service(
            datahub=_STUB_DATAHUB,
            db=async_session,
            cache=_STUB_CACHE,
            vector=_STUB_VECTOR,
        )

    mock_first.assert_called_once()
    assert mock_first.call_args.kwargs["provider"] == "openai"
    assert mock_first.call_args.kwargs["model"] == "gpt-4o-test"
    assert service_first._llm is first_sentinel

    # Simulate admin changing provider/model via /admin/conf
    # patch_runtime_config commits and refreshes the process-level cache
    # so the next get_runtime_config call returns the new values immediately.
    await patch_runtime_config(
        async_session,
        llm_provider="anthropic",
        llm_model="claude-test",
    )

    second_sentinel = object()
    with patch("src.workflows._common.make_llm", return_value=second_sentinel) as mock_second:
        service_second = await get_metagen_service(
            datahub=_STUB_DATAHUB,
            db=async_session,
            cache=_STUB_CACHE,
            vector=_STUB_VECTOR,
        )

    # After patching runtime_config, make_llm must be called with the NEW
    # provider/model on the next get_metagen_service invocation — proving
    # per-request construction honours immediate /admin/conf changes.
    # spec: src/api/dependencies.py — per-request LLM construction.
    mock_second.assert_called_once()
    assert mock_second.call_args.kwargs["provider"] == "anthropic"
    assert mock_second.call_args.kwargs["model"] == "claude-test"
    assert service_second._llm is second_sentinel, (
        "Second MetagenService._llm must be the object make_llm returned for "
        "the updated provider/model, not the one from the first call."
    )
    # The two services must hold distinct LLM instances — the first was not reused.
    assert service_first._llm is not service_second._llm, (
        "Each get_metagen_service call must produce a fresh LLM instance; "
        "process-level LLM caching would make these the same object."
    )
