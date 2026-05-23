"""Unit tests for workflow stub implementations (_stubs.py).

Tests the interface contract of each stub vs. the real client:
- Same async public method names exist on the stub as on the real client.
- Methods return canned-but-correct types (not raise, not return wrong type).
- Stubs are safe to call from test code.

spec: TESTING.md §Test-Mode Stubs — stubs implement the same interface as real clients.
"""

import inspect

import pytest

from src.workflows._stubs import (
    StubLLMClient,
    StubNotificationService,
    StubRedisClient,
    StubVectorManager,
)
from src.shared.cache.client import RedisClient
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager
from src.shared.notifications.service import NotificationService


# ── Helpers ───────────────────────────────────────────────────────────────────


def _public_async_methods(cls: type) -> set[str]:
    """Return the set of public async method names on a class (not dunder)."""
    return {
        name
        for name, fn in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_") and inspect.iscoroutinefunction(fn)
    }


# ── StubLLMClient interface contract ─────────────────────────────────────────


def test_stub_llm_client_has_embed_method() -> None:
    """StubLLMClient must expose async embed(text) → list[float].

    spec: TESTING.md §Test-Mode Stubs — StubLLMClient: embed() returns deterministic unit vector.
    """
    assert hasattr(StubLLMClient, "embed")
    assert inspect.iscoroutinefunction(StubLLMClient.embed)


def test_stub_llm_client_has_complete_method() -> None:
    """StubLLMClient must expose async complete(prompt, system, temperature).

    spec: TESTING.md §Test-Mode Stubs — StubLLMClient: complete() returns 'stub'.
    """
    assert hasattr(StubLLMClient, "complete")
    assert inspect.iscoroutinefunction(StubLLMClient.complete)


def test_stub_llm_client_has_complete_json_method() -> None:
    """StubLLMClient must expose async complete_json(prompt, system, schema).

    spec: TESTING.md §Test-Mode Stubs — StubLLMClient: complete_json() returns dict.
    """
    assert hasattr(StubLLMClient, "complete_json")
    assert inspect.iscoroutinefunction(StubLLMClient.complete_json)


@pytest.mark.asyncio
async def test_stub_llm_embed_returns_list_of_floats() -> None:
    """StubLLMClient.embed must return a list[float].

    spec: TESTING.md §Test-Mode Stubs — embed() returns deterministic unit vector.
    """
    stub = StubLLMClient()
    result = await stub.embed("some text")
    assert isinstance(result, list)
    assert all(isinstance(v, float) for v in result)


@pytest.mark.asyncio
async def test_stub_llm_complete_returns_string() -> None:
    """StubLLMClient.complete must return a str.

    spec: TESTING.md §Test-Mode Stubs — complete() returns 'stub'.
    """
    stub = StubLLMClient()
    result = await stub.complete("prompt")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_stub_llm_complete_json_returns_dict() -> None:
    """StubLLMClient.complete_json must return a dict.

    spec: TESTING.md §Test-Mode Stubs — complete_json() returns minimal dict.
    """
    stub = StubLLMClient()
    result = await stub.complete_json("prompt")
    assert isinstance(result, dict)


# ── StubVectorManager interface contract ──────────────────────────────────────


def test_stub_vector_manager_has_search_method() -> None:
    """StubVectorManager must expose async search(...) → list.

    spec: TESTING.md §Test-Mode Stubs — StubVectorManager: search() returns [].
    """
    assert hasattr(StubVectorManager, "search")
    assert inspect.iscoroutinefunction(StubVectorManager.search)


def test_stub_vector_manager_has_upsert_method() -> None:
    """StubVectorManager must expose async upsert(...).

    spec: TESTING.md §Test-Mode Stubs — stub interface matches PgVectorManager.
    """
    assert hasattr(StubVectorManager, "upsert")
    assert inspect.iscoroutinefunction(StubVectorManager.upsert)


def test_stub_vector_manager_has_delete_method() -> None:
    """StubVectorManager must expose async delete(...).

    spec: TESTING.md §Test-Mode Stubs — stub interface matches PgVectorManager.
    """
    assert hasattr(StubVectorManager, "delete")
    assert inspect.iscoroutinefunction(StubVectorManager.delete)


@pytest.mark.asyncio
async def test_stub_vector_search_returns_empty_list() -> None:
    """StubVectorManager.search must return an empty list.

    spec: TESTING.md §Test-Mode Stubs — search() returns [].
    """
    stub = StubVectorManager()
    result = await stub.search(collection="test", vector=[0.0] * 5)
    assert result == []


# ── StubRedisClient interface contract ────────────────────────────────────────


def test_stub_redis_client_has_get_set_delete_methods() -> None:
    """StubRedisClient must expose async get, set, delete.

    spec: TESTING.md §Test-Mode Stubs — StubRedisClient: all ops are no-ops.
    """
    for method in ("get", "set", "delete"):
        assert hasattr(StubRedisClient, method), f"StubRedisClient missing '{method}'"
        assert inspect.iscoroutinefunction(getattr(StubRedisClient, method))


def test_stub_redis_client_has_set_nx_method() -> None:
    """StubRedisClient must expose async set_nx(...) → bool.

    spec: TESTING.md §Test-Mode Stubs — no-op implementation.
    """
    assert hasattr(StubRedisClient, "set_nx")
    assert inspect.iscoroutinefunction(StubRedisClient.set_nx)


@pytest.mark.asyncio
async def test_stub_redis_get_returns_none() -> None:
    """StubRedisClient.get must return None (no-op).

    spec: TESTING.md §Test-Mode Stubs — cache get is a no-op returning None.
    """
    stub = StubRedisClient()
    result = await stub.get("any-key")
    assert result is None


@pytest.mark.asyncio
async def test_stub_redis_set_nx_returns_true() -> None:
    """StubRedisClient.set_nx must return True (successful no-op).

    spec: TESTING.md §Test-Mode Stubs — set_nx always succeeds in stub.
    """
    stub = StubRedisClient()
    result = await stub.set_nx("lock-key", "value", ttl_seconds=60)
    assert result is True


# ── StubNotificationService interface contract ────────────────────────────────


def test_stub_notification_service_has_send_sla_alert() -> None:
    """StubNotificationService must expose async send_sla_alert(...).

    spec: TESTING.md §Test-Mode Stubs — StubNotificationService: send_sla_alert() is no-op.
    """
    assert hasattr(StubNotificationService, "send_sla_alert")
    assert inspect.iscoroutinefunction(StubNotificationService.send_sla_alert)


@pytest.mark.asyncio
async def test_stub_notification_send_sla_alert_does_not_raise() -> None:
    """StubNotificationService.send_sla_alert must not raise.

    spec: TESTING.md §Test-Mode Stubs — no-op; never sends emails.
    """
    stub = StubNotificationService()
    # Must not raise
    await stub.send_sla_alert(
        metric_id="pct_fresh",
        threshold=0.5,
        current_value=0.3,
        recipients=["team@example.com"],
    )


# ── Drift detection: stub method names match real client ──────────────────────


def test_stub_llm_does_not_have_fewer_methods_than_real() -> None:
    """StubLLMClient must implement every public async method that LLMClient has.

    spec: TESTING.md §Test-Mode Stubs — stubs must not silently drop methods.
    """
    real_methods = _public_async_methods(LLMClient)
    stub_methods = _public_async_methods(StubLLMClient)
    missing = real_methods - stub_methods
    assert not missing, (
        f"StubLLMClient is missing async methods present on LLMClient: {missing}. "
        "Update _stubs.py to match the real interface."
    )


def test_stub_vector_does_not_have_fewer_methods_than_real() -> None:
    """StubVectorManager must implement every public async method that PgVectorManager has.

    spec: TESTING.md §Test-Mode Stubs — stub interface must not drift from real client.
    """
    real_methods = _public_async_methods(PgVectorManager)
    stub_methods = _public_async_methods(StubVectorManager)
    missing = real_methods - stub_methods
    assert not missing, (
        f"StubVectorManager is missing async methods: {missing}. "
        "Update _stubs.py to match PgVectorManager."
    )


def test_stub_redis_does_not_have_fewer_methods_than_real() -> None:
    """StubRedisClient must implement every public async method that RedisClient has.

    spec: TESTING.md §Test-Mode Stubs — stub interface must not drift from real client.
    """
    real_methods = _public_async_methods(RedisClient)
    stub_methods = _public_async_methods(StubRedisClient)
    missing = real_methods - stub_methods
    assert not missing, (
        f"StubRedisClient is missing async methods: {missing}. "
        "Update _stubs.py to match RedisClient."
    )


# ── Metagen stub branches ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stub_metagen_validate_well_formed_prompt_returns_valid_output() -> None:
    """complete_with_tools with a well-formed metagen prompt returns MetagenLLMOutput
    with at least one candidate per target item using the expected per-item value pattern.

    Uses build_run_prompt to construct the prompt so the test fails if prompts.py
    formatting drifts away from the format the stub parser expects.

    spec: TESTING.md §Test-Mode Stubs — metagen Producer stub emits one candidate per
    target item.
    spec: BACKEND_LLM.md §Test Mode — metagen Producer stub emits one candidate per
    target item; metagen Reviewer stub accepts.
    """
    from src.backend.metagen.debate_models import MetagenLLMOutput
    from src.backend.metagen.prompts import build_run_prompt
    from src.workflows._stubs import StubLLMClient

    dataset_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,test_db.orders,DEV)"
    target_items = [
        {
            "dataset_urn": dataset_urn,
            "item_id": "dataset.description",
            "kind": "dataset.description",
            "field_path": None,
        },
        {
            "dataset_urn": dataset_urn,
            "item_id": "column.order_id.description",
            "kind": "column.description",
            "field_path": "order_id",
        },
    ]
    evidence_per_dataset = {
        dataset_urn: {
            "datasetProperties": {"name": "orders"},
            "schemaMetadata": {
                "fields": [
                    {"fieldPath": "order_id", "type": "string", "description": ""},
                ]
            },
            "editableDatasetProperties": {},
            "editableSchemaMetadata": {"editableSchemaFieldInfo": []},
            "glossaryTerms": [],
            "ontology": {},
        }
    }

    prompt = build_run_prompt(
        evidence_per_dataset=evidence_per_dataset,
        target_items=target_items,
        nonce="testtest",
    )

    stub = StubLLMClient()
    result = await stub.complete_with_tools(
        prompt=prompt,
        tools=[],
        success_tool_name="metagen_validate",
        schema=MetagenLLMOutput,
    )

    # Payload must validate against MetagenLLMOutput.
    output = MetagenLLMOutput.model_validate(result.payload)
    assert len(output.candidates) >= 1, (
        "Stub metagen_validate must produce at least one candidate for a well-formed prompt. "
        "spec: BACKEND_LLM.md §Test Mode — one candidate per target item."
    )
    # Each candidate must have the expected per-item value pattern.
    for candidate in output.candidates:
        assert candidate.dataset_urn == dataset_urn, (
            f"Stub candidate dataset_urn={candidate.dataset_urn!r} does not match "
            f"target {dataset_urn!r}."
        )
        assert candidate.item_id in (
            "dataset.description",
            "column.order_id.description",
        ), f"Unexpected item_id in stub candidate: {candidate.item_id!r}"
        assert isinstance(candidate.value, str) and candidate.value, (
            f"Stub candidate value must be a non-empty string; got {candidate.value!r}. "
            "spec: BACKEND_LLM.md §Test Mode — metagen producer stub emits one "
            "candidate per target item"
        )
        assert 0.0 <= candidate.confidence_score <= 1.0, (
            f"Stub candidate confidence_score out of range: {candidate.confidence_score!r}"
        )


@pytest.mark.asyncio
async def test_stub_metagen_validate_malformed_prompt_falls_back_to_empty_candidates() -> None:
    """complete_with_tools with a malformed metagen prompt (missing TARGET ITEMS block)
    falls through to _minimal_dict_for_schema and returns {"candidates": []}.

    spec: TESTING.md §Test-Mode Stubs — stub parser falls through gracefully.
    """
    from src.backend.metagen.debate_models import MetagenLLMOutput
    from src.workflows._stubs import StubLLMClient

    malformed_prompt = "This prompt has no TARGET ITEMS block at all."

    stub = StubLLMClient()
    result = await stub.complete_with_tools(
        prompt=malformed_prompt,
        tools=[],
        success_tool_name="metagen_validate",
        schema=MetagenLLMOutput,
    )

    # _minimal_dict_for_schema produces {"candidates": []} for MetagenLLMOutput.
    assert "candidates" in result.payload, (
        "Fallback payload must have 'candidates' key. "
        "spec: TESTING.md §Test-Mode Stubs — _minimal_dict_for_schema returns list default."
    )
    assert result.payload["candidates"] == [], (
        f"Malformed prompt must produce candidates=[] via _minimal_dict_for_schema; "
        f"got {result.payload['candidates']!r}."
    )


@pytest.mark.asyncio
async def test_stub_metagen_review_returns_accept() -> None:
    """complete_with_tools with success_tool_name='metagen_review' always returns
    overall_verdict='accept' regardless of prompt content.

    spec: BACKEND_LLM.md §Test Mode — metagen Reviewer stub accepts.
    """
    from src.backend.metagen.debate_models import MetagenReviewOutput
    from src.workflows._stubs import StubLLMClient

    stub = StubLLMClient()
    result = await stub.complete_with_tools(
        prompt="any prompt content",
        tools=[],
        success_tool_name="metagen_review",
        schema=MetagenReviewOutput,
    )

    # Payload must validate against MetagenReviewOutput.
    review = MetagenReviewOutput.model_validate(result.payload)
    assert review.overall_verdict == "accept", (
        f"Stub metagen_review must return overall_verdict='accept'; "
        f"got {review.overall_verdict!r}. "
        "spec: BACKEND_LLM.md §Test Mode — metagen Reviewer stub accepts."
    )
    assert review.summary == "stub-accept", (
        f"Stub metagen_review summary must be 'stub-accept'; got {review.summary!r}."
    )
