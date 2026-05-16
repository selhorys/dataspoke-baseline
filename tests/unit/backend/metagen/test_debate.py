"""Unit tests for src/backend/metagen/debate.py — run_debate loop.

Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework
      spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate

Key metagen vs ontogen difference (spec §Metagen Adversarial Debate):
  - turns_exhausted / cycle_detected → candidates DROPPED (no llm_pending fallback)
  - Only outcome='accept' AND confidence >= threshold → persisted as 'llm_approved'

Groups:
  A – accept on turn 1 (happy path)
  B – revise then accept (turn 2 Producer revision, turn 3 Reviewer accept)
  C – turns_exhausted → dropped (NOT persisted)
  D – cycle_detected → dropped (NOT persisted)
  E – below-threshold candidates dropped on accept
  F – cold start (empty RAG anchors)
  G – reviewer_model override wires make_llm
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.metagen.debate import run_debate
from src.backend.metagen.debate_models import DebateResult
from src.shared.llm.loop_trace import LoopResult, LoopTrace

# ─────────────────────────────────────────────────────────────────────────────
# FakeLLM — script-driven stand-in for LLMClient
# ─────────────────────────────────────────────────────────────────────────────


class FakeLLM:
    """Script-driven fake LLMClient.

    ``script`` is a list of ``LoopResult`` objects consumed FIFO by successive
    ``complete_with_tools`` calls.  ``embed`` always returns a zero vector of
    length 10 (sufficient for the test surface).
    """

    def __init__(self, script: list[LoopResult]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete_with_tools(
        self,
        prompt: str,
        *,
        tools: list[Any],
        success_tool_name: str,
        schema: type[Any],
        system: str = "",
        max_iterations: int = 3,
        temperature: float = 0.0,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LoopResult:
        self.calls.append({"prompt": prompt, "success_tool_name": success_tool_name})
        if not self._script:
            raise RuntimeError("FakeLLM script exhausted")
        return self._script.pop(0)

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 10


def _trace(iterations: int = 1) -> LoopTrace:
    return LoopTrace(iterations=iterations, errors_per_iter=[], final_errors=[])


def _accept_result() -> LoopResult:
    return LoopResult(
        payload={
            "overall_verdict": "accept",
            "item_verdicts": [],
            "summary": "looks good",
        },
        trace=_trace(),
    )


def _revise_result() -> LoopResult:
    return LoopResult(
        payload={
            "overall_verdict": "revise",
            "item_verdicts": [{
                "item_kind": "dataset_description",
                "dataset_urn": "urn:x",
                "item_id": "dataset.description",
                "verdict": "revise",
                "issues": ["value_too_generic"],
                "comment": "too vague",
            }],
            "summary": "please revise",
        },
        trace=_trace(),
    )


def _producer_payload_1() -> dict[str, Any]:
    return {
        "candidates": [{
            "dataset_urn": "urn:x",
            "item_id": "dataset.description",
            "value": "A catalog of books for Imazon.",
            "confidence_score": 0.9,
        }]
    }


def _producer_result_1() -> LoopResult:
    return LoopResult(payload=_producer_payload_1(), trace=_trace())


def _producer_payload_2() -> dict[str, Any]:
    return {
        "candidates": [{
            "dataset_urn": "urn:x",
            "item_id": "dataset.description",
            "value": "The Imazon book catalog storing ISBNs, titles, and authors.",
            "confidence_score": 0.85,
        }]
    }


def _producer_result_2() -> LoopResult:
    return LoopResult(payload=_producer_payload_2(), trace=_trace())


def _fake_validate_tool() -> MagicMock:
    tool = MagicMock()
    tool.name = "metagen_validate"
    return tool


def _fake_review_tool() -> MagicMock:
    tool = MagicMock()
    tool.name = "metagen_review"
    return tool


async def _run(
    producer: FakeLLM,
    reviewer: FakeLLM | None = None,
    *,
    max_turns: int = 4,
    reviewer_model: str | None = None,
) -> DebateResult:
    """Call run_debate with minimal wiring; patch away all pgvector calls."""
    db = MagicMock()
    vector = MagicMock()

    with (
        patch(
            "src.backend.metagen.debate.search_candidate_embeddings",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.backend.metagen.debate.make_llm",
            return_value=(reviewer if reviewer_model and reviewer else producer),
        ),
    ):
        return await run_debate(
            llm=producer,  # type: ignore[arg-type]
            vector=vector,
            db=db,
            producer_prompt="generate metadata for dataset urn:x",
            validate_tool=_fake_validate_tool(),
            review_tool=_fake_review_tool(),
            in_scope_urns=frozenset(["urn:x"]),
            max_turns=max_turns,
            rag_k=2,
            reviewer_model=reviewer_model,
            producer_schema=MagicMock(),
            producer_max_iterations=3,
            run_id="test-run-id",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group A: accept on turn 1
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_accept_on_turn_1() -> None:
    """Reviewer accepts the Producer's first candidate on turn 1.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination
    — outcome='accept' when Reviewer returns overall_verdict='accept'.
    Loop shape: P(0) → R(1, accept) → terminate.
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer
        _accept_result(),       # turn 1: Reviewer — accept
    ])

    result = await _run(llm)

    assert result.outcome == "accept", (
        f"Expected outcome='accept'; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )
    assert result.transcript["turns_completed"] == 2, (
        f"Expected 2 turns_completed (P0+R1); got {result.transcript['turns_completed']}. "
        "spec: BACKEND_LLM.md §Loop shape"
    )
    assert result.payload == _producer_payload_1(), (
        "payload on accept must be the Producer's candidate. "
        "spec: BACKEND_LLM.md §Termination"
    )
    history = result.transcript["history"]
    assert len(history) == 2
    assert history[0]["actor"] == "producer" and history[0]["turn"] == 0
    assert history[1]["actor"] == "reviewer" and history[1]["turn"] == 1
    assert history[1].get("verdict") == "accept"


# ─────────────────────────────────────────────────────────────────────────────
# Group B: revise then accept
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_revise_then_accept() -> None:
    """Producer emits P1, Reviewer revises, Producer emits P2, Reviewer accepts.

    Spec: BACKEND_LLM.md §Loop shape — P0 → R1(revise) → P2 → R3(accept).
    Expected: outcome='accept', turns_completed=4, payload=P2 (last candidate).
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer → P1
        _revise_result(),       # turn 1: Reviewer → revise
        _producer_result_2(),   # turn 2: Producer → P2 (revised)
        _accept_result(),       # turn 3: Reviewer → accept
    ])

    result = await _run(llm)

    assert result.outcome == "accept", (
        f"Expected outcome='accept' after revise-then-accept; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )
    assert result.transcript["turns_completed"] == 4, (
        f"Expected turns_completed=4; got {result.transcript['turns_completed']}. "
        "spec: BACKEND_LLM.md §Loop shape"
    )
    assert result.payload == _producer_payload_2(), (
        "Payload must be P2 (last candidate after revision). "
        "spec: BACKEND_LLM.md §Loop shape — last candidate kept"
    )
    history = result.transcript["history"]
    assert len(history) == 4
    actors = [(e["actor"], e["turn"]) for e in history]
    assert actors == [("producer", 0), ("reviewer", 1), ("producer", 2), ("reviewer", 3)], (
        f"History actors/turns out of order: {actors!r}. "
        "spec: BACKEND_LLM.md §Evidence shape §history"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group C: turns_exhausted → candidates DROPPED
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_turns_exhausted_outcome() -> None:
    """Loop reaches max_turns=4 without accept; outcome=turns_exhausted.

    Spec: BACKEND_LLM.md §Termination — turns_exhausted: last candidate is kept in payload.
    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — turns_exhausted drops candidates (no llm_pending).
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0
        _revise_result(),       # turn 1
        _producer_result_2(),   # turn 2
        _revise_result(),       # turn 3
    ])

    result = await _run(llm, max_turns=4)

    assert result.outcome == "turns_exhausted", (
        f"Expected outcome='turns_exhausted'; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )
    assert result.transcript["turns_completed"] == 4
    # spec: last candidate (P2) is kept in payload
    assert result.payload == _producer_payload_2(), (
        "Payload must be last candidate (P2) on turns_exhausted. "
        "spec: BACKEND_LLM.md §Termination — 'last candidate is kept'"
    )


@pytest.mark.asyncio
async def test_run_debate_turns_exhausted_does_not_produce_llm_pending() -> None:
    """turns_exhausted outcome does not persist any llm_pending candidates.

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — unlike ontogen, metagen
    drops all candidates on turns_exhausted; no llm_pending state is written.

    The test verifies by observing that MetagenService.run() skips persistence
    when outcome != 'accept' — here we verify the DebateResult outcome flag
    that service.run() consults.
    """
    llm = FakeLLM([
        _producer_result_1(),
        _revise_result(),
        _producer_result_2(),
        _revise_result(),
    ])

    result = await _run(llm, max_turns=4)

    assert result.outcome == "turns_exhausted"
    # Service code consults outcome; 'turns_exhausted' signals drop.
    assert result.outcome != "accept", (
        "turns_exhausted must not be 'accept' so service drops the candidates. "
        "spec: BACKEND_LLM.md §Metagen Adversarial Debate"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group D: cycle_detected → candidates DROPPED
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_cycle_detected() -> None:
    """Producer emits P1-again (same payload as P1); cycle_detected.

    Spec: BACKEND_LLM.md §Cycle detection — 'If the new hash matches any prior
    Producer turn's hash, the loop terminates with outcome=cycle_detected.'
    """
    p1 = _producer_payload_1()
    llm = FakeLLM([
        LoopResult(payload=p1, trace=_trace()),   # turn 0: P1
        _revise_result(),                          # turn 1: Reviewer → revise
        LoopResult(payload=p1, trace=_trace()),   # turn 2: P1-again → cycle
    ])

    result = await _run(llm, max_turns=6)

    assert result.outcome == "cycle_detected", (
        f"Expected outcome='cycle_detected'; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )
    assert result.payload == p1, (
        "Payload on cycle_detected is the cycle-trigger candidate. "
        "spec: BACKEND_LLM.md §Cycle detection — 'last candidate is kept'"
    )


@pytest.mark.asyncio
async def test_run_debate_cycle_detected_does_not_produce_llm_pending() -> None:
    """cycle_detected outcome signals candidate drop (no llm_pending in metagen).

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — turns_exhausted and
    cycle_detected both drop candidates; no fallback persistence.
    """
    p1 = _producer_payload_1()
    llm = FakeLLM([
        LoopResult(payload=p1, trace=_trace()),
        _revise_result(),
        LoopResult(payload=p1, trace=_trace()),
    ])

    result = await _run(llm, max_turns=6)

    assert result.outcome == "cycle_detected"
    assert result.outcome != "accept", (
        "cycle_detected must not be 'accept'; service must not persist candidates. "
        "spec: BACKEND_LLM.md §Metagen Adversarial Debate"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group E: confidence threshold gate (service layer contract, debate API)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_accept_payload_carries_confidence_scores() -> None:
    """On accept, the debate payload contains candidates with confidence_score.

    The MetagenService.run() filters accepted candidates by confidence_score >=
    DATASPOKE_METAGEN_CONFIDENCE_THRESHOLD.  This test asserts that the payload
    field is present so the service can apply the threshold gate.

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — below-threshold candidates
    dropped; debate payload carries confidence_score for service to evaluate.
    """
    llm = FakeLLM([
        _producer_result_1(),   # confidence_score=0.9 (above any reasonable threshold)
        _accept_result(),
    ])

    result = await _run(llm)

    assert result.outcome == "accept"
    candidates = result.payload.get("candidates", [])
    assert len(candidates) > 0
    for cand in candidates:
        assert "confidence_score" in cand, (
            "Each candidate in the accepted payload must carry confidence_score. "
            "spec: BACKEND_LLM.md §Metagen Adversarial Debate — threshold gate"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group F: cold start (empty RAG anchors)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_cold_start_no_crash() -> None:
    """Debate proceeds normally when all pgvector searches return empty lists.

    Spec: BACKEND_LLM.md §RAG anchors — cold start: Reviewer runs without anchor
    grounding; no special fallback prompt needed.
    """
    llm = FakeLLM([
        _producer_result_1(),
        _accept_result(),
    ])

    result = await _run(llm)

    assert result.outcome == "accept", (
        f"Debate on cold start must still accept; got outcome={result.outcome!r}. "
        "spec: BACKEND_LLM.md §RAG anchors — cold start"
    )
    assert result.transcript.get("rag_anchors") == [], (
        "rag_anchors must be empty on cold start. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group G: reviewer_model override wires make_llm
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_reviewer_model_override_calls_make_llm() -> None:
    """When reviewer_model is set, make_llm(model_override=...) is called for the Reviewer.

    Spec: BACKEND_LLM.md §Settings Reference — DATASPOKE_METAGEN_DEBATE_REVIEWER_MODEL.
    Debate module must use make_llm() so test-mode stubbing applies to the Reviewer.
    """
    producer_llm = FakeLLM([_producer_result_1()])
    reviewer_fake_llm = FakeLLM([_accept_result()])

    db = MagicMock()
    vector = MagicMock()

    with (
        patch(
            "src.backend.metagen.debate.search_candidate_embeddings",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.backend.metagen.debate.make_llm",
            return_value=reviewer_fake_llm,
        ) as mock_make_llm,
    ):
        result = await run_debate(
            llm=producer_llm,  # type: ignore[arg-type]
            vector=vector,
            db=db,
            producer_prompt="generate metadata",
            validate_tool=_fake_validate_tool(),
            review_tool=_fake_review_tool(),
            in_scope_urns=frozenset(["urn:x"]),
            max_turns=4,
            rag_k=2,
            reviewer_model="some-other-model",
            producer_schema=MagicMock(),
            producer_max_iterations=3,
            run_id="test-run-id",
        )

    # Assert make_llm was called with the reviewer model string (positional or keyword)
    assert mock_make_llm.called, "make_llm must be called when reviewer_model is set."
    call_args = mock_make_llm.call_args
    all_args = list(call_args.args) + list(call_args.kwargs.values())
    assert "some-other-model" in all_args, (
        "make_llm must receive 'some-other-model' regardless of binding form. "
        "spec: BACKEND_LLM.md §Settings Reference — reviewer_model override"
    )
    assert result.outcome == "accept"


# ─────────────────────────────────────────────────────────────────────────────
# Cycle detection property tests (via public run_debate surface)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_key_order_invariance_does_not_prevent_cycle_detection() -> None:
    """Payloads with same content but different key order are detected as a cycle.

    Spec: BACKEND_LLM.md §Cycle detection — canonical: json.dumps(sort_keys=True).
    The cycle-detector hashes payloads in canonical form, so key-order differences
    in an otherwise identical payload still trigger cycle_detected.
    """
    # Same logical content, Python dicts preserve insertion order so we create
    # two dicts with different key order but equivalent JSON (sort_keys=True collapses them)
    payload_a = {
        "candidates": [{"dataset_urn": "urn:x", "item_id": "dataset.description", "value": "v1", "confidence_score": 0.9}],
        "extra": 1,
    }
    payload_b = {
        "extra": 1,
        "candidates": [{"confidence_score": 0.9, "value": "v1", "item_id": "dataset.description", "dataset_urn": "urn:x"}],
    }

    llm = FakeLLM([
        LoopResult(payload=payload_a, trace=_trace()),  # turn 0: Producer
        _revise_result(),                                # turn 1: Reviewer → revise
        LoopResult(payload=payload_b, trace=_trace()),  # turn 2: same content, different key order
    ])

    result = await _run(llm, max_turns=6)

    assert result.outcome == "cycle_detected", (
        "Key-order-normalized payloads with same content must trigger cycle_detected. "
        "spec: BACKEND_LLM.md §Cycle detection — sort_keys=True canonical hash"
    )


@pytest.mark.asyncio
async def test_run_debate_different_content_does_not_trigger_cycle() -> None:
    """Payloads with genuinely different content do not trigger cycle_detected.

    Spec: BACKEND_LLM.md §Cycle detection — only repeated content triggers the cycle.
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: P1
        _revise_result(),        # turn 1: Reviewer → revise
        _producer_result_2(),   # turn 2: P2 (different content — no cycle)
        _accept_result(),        # turn 3: Reviewer → accept
    ])

    result = await _run(llm, max_turns=6)

    assert result.outcome == "accept", (
        "Different-content payloads must NOT trigger cycle_detected. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )
