"""Unit tests for src/backend/ontogen/debate.py — run_debate loop.

Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework

Groups:
  A – accept on turn 1 (happy path)
  B – revise then accept (turn 0 Producer, turn 1 Reviewer revise,
      turn 2 Producer revision, turn 3 Reviewer accept)
  C – turns_exhausted (bounded by max_turns=4, last candidate kept)
  D – cycle_detected (Producer emits identical payload twice)
  E – canonical_hash is key-order-independent (SHA-256 with sort_keys)
  F – empty RAG anchors (cold-start — debate must not crash)
  G – reviewer_model override wires make_llm(model_override=...)
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ontogen.debate import _canonical_hash, run_debate
from src.backend.ontogen.debate_models import DebateResult
from src.shared.llm.loop_trace import LoopResult, LoopTrace

# ─────────────────────────────────────────────────────────────────────────────
# FakeLLM — script-driven stand-in for LLMClient
# ─────────────────────────────────────────────────────────────────────────────


class FakeLLM:
    """Script-driven fake LLMClient.

    ``script`` is a list of ``LoopResult`` objects consumed in FIFO order by
    successive ``complete_with_tools`` calls.  An ``embed`` call always returns
    a fixed zero-vector of length 1536 (matches EMBEDDING_DIMENSION default).
    """

    EMBEDDING_DIMENSION = 1536

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
        return [0.0] * self.EMBEDDING_DIMENSION


def _trace(iterations: int = 1) -> LoopTrace:
    return LoopTrace(iterations=iterations, errors_per_iter=[], final_errors=[])


def _accept_result(payload: dict[str, Any] | None = None) -> LoopResult:
    """Reviewer result for overall_verdict='accept'."""
    return LoopResult(
        payload={
            "overall_verdict": "accept",
            "item_verdicts": [],
            "summary": "looks good",
        },
        trace=_trace(),
    )


def _revise_result(issues: list[str] | None = None) -> LoopResult:
    """Reviewer result for overall_verdict='revise'."""
    return LoopResult(
        payload={
            "overall_verdict": "revise",
            "item_verdicts": [
                {
                    "item_kind": "node",
                    "item_id": "book",
                    "verdict": "revise",
                    "issues": issues or ["confidence_miscalibrated"],
                    "comment": "score too high",
                }
            ],
            "summary": "please revise confidence",
        },
        trace=_trace(),
    )


def _producer_payload_1() -> dict[str, Any]:
    return {
        "nodes": [
            {"name": "Book", "id": "book", "confidence_score": 0.95, "dataset_urns": ["urn:x"]}
        ],
        "edges": [],
        "triples": [],
    }


def _producer_result_1() -> LoopResult:
    return LoopResult(payload=_producer_payload_1(), trace=_trace())


def _producer_payload_2() -> dict[str, Any]:
    """Different from payload 1 — lower confidence, applied reviewer suggestion."""
    return {
        "nodes": [
            {"name": "Book", "id": "book", "confidence_score": 0.7, "dataset_urns": ["urn:x"]}
        ],
        "edges": [],
        "triples": [],
    }


def _producer_result_2() -> LoopResult:
    return LoopResult(payload=_producer_payload_2(), trace=_trace())


def _fake_validate_tool() -> MagicMock:
    tool = MagicMock()
    tool.name = "ontogen_validate"
    return tool


def _fake_review_tool() -> MagicMock:
    tool = MagicMock()
    tool.name = "ontogen_review"
    return tool


async def _run(
    producer: FakeLLM,
    reviewer: FakeLLM | None = None,
    *,
    max_turns: int = 4,
    reviewer_model: str | None = None,
) -> DebateResult:
    """Call run_debate with minimal wiring; patch away all pgvector calls."""
    from unittest.mock import AsyncMock as AM

    db = MagicMock()
    vector = MagicMock()

    # search helpers return empty lists (cold start) unless provided otherwise
    with (
        patch("src.backend.ontogen.debate._search_node_embeddings", new=AM(return_value=[])),
        patch("src.backend.ontogen.debate._search_edge_embeddings", new=AM(return_value=[])),
        patch("src.backend.ontogen.debate._search_triple_embeddings", new=AM(return_value=[])),
        patch(
            "src.backend.ontogen.debate.make_llm",
            return_value=(reviewer if reviewer_model and reviewer else producer),
        ),
    ):
        return await run_debate(
            llm=producer,  # type: ignore[arg-type]
            vector=vector,
            db=db,
            producer_prompt="produce ontology",
            validate_tool=_fake_validate_tool(),
            review_tool=_fake_review_tool(),
            in_scope_urns=frozenset(["urn:x"]),
            max_turns=max_turns,
            rag_k=2,
            reviewer_model=reviewer_model,
            llm_provider="openai",
            llm_base_model="gpt-4o",
            producer_schema=MagicMock(),
            producer_max_iterations=3,
            run_id="test-run-id",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group A: accept on turn 1
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_accepts_turn_1() -> None:
    """Reviewer accepts the Producer's first candidate on turn 1.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination
    — 'accept: Reviewer returned overall_verdict='accept'; persist with transcript.'
    Loop shape: turn 0 Producer → turn 1 Reviewer (accept) → terminate.
    Expected: outcome='accept', turns_completed=2, payload=P1,
    transcript history has exactly 2 entries (actor=producer at turn 0,
    actor=reviewer at turn 1 with verdict='accept').
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer
        _accept_result(),       # turn 1: Reviewer
    ])

    result = await _run(llm)

    # spec: §Termination — outcome
    assert result.outcome == "accept", (
        f"Expected outcome='accept'; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination"
    )

    # spec: §Loop shape — 2 turns: 1 Producer + 1 Reviewer
    assert result.transcript["turns_completed"] == 2, (
        f"Expected turns_completed=2; got {result.transcript['turns_completed']!r}. "
        "spec: BACKEND_LLM.md §Loop shape"
    )

    # spec: §Evidence shape — payload is the Producer's candidate
    assert result.payload == _producer_payload_1(), (
        f"Expected P1 payload; got {result.payload!r}. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )

    history = result.transcript["history"]
    assert len(history) == 2, (
        f"Expected 2 history entries; got {len(history)}. "
        "spec: BACKEND_LLM.md §Evidence shape §history"
    )
    assert history[0]["actor"] == "producer" and history[0]["turn"] == 0, (
        f"First history entry must be Producer turn 0; got {history[0]!r}. "
        "spec: BACKEND_LLM.md §Evidence shape §history"
    )
    assert history[1]["actor"] == "reviewer" and history[1]["turn"] == 1, (
        f"Second history entry must be Reviewer turn 1; got {history[1]!r}. "
        "spec: BACKEND_LLM.md §Evidence shape §history"
    )
    assert history[1].get("verdict") == "accept", (
        f"Reviewer turn 1 verdict must be 'accept'; got {history[1].get('verdict')!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )
    # spec: §Evidence shape — candidate_hash present on producer entry
    assert "candidate_hash" in history[0], (
        "Producer history entry must carry candidate_hash. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group B: revise then accept
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_revise_then_accept() -> None:
    """Producer emits P1, Reviewer requests revise, Producer emits P2, Reviewer accepts.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Loop shape
    — turn 2 Producer revises after Reviewer feedback; turn 3 Reviewer accepts.
    Expected: outcome='accept', turns_completed=4, payload=P2 (not P1),
    history has 4 entries in order.
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer → P1
        _revise_result(),       # turn 1: Reviewer → revise
        _producer_result_2(),   # turn 2: Producer → P2 (revised)
        _accept_result(),       # turn 3: Reviewer → accept
    ])

    result = await _run(llm)

    # spec: §Termination — outcome after revision accepted
    assert result.outcome == "accept", (
        f"Expected outcome='accept' after revise-then-accept; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )

    # spec: §Loop shape — 4 turns total (P0, R1, P2, R3)
    assert result.transcript["turns_completed"] == 4, (
        f"Expected turns_completed=4; got {result.transcript['turns_completed']!r}. "
        "spec: BACKEND_LLM.md §Loop shape"
    )

    # spec: §Loop shape — payload is the LAST candidate (P2 after revision)
    assert result.payload == _producer_payload_2(), (
        f"Expected P2 (revised) payload; got {result.payload!r}. "
        "spec: BACKEND_LLM.md §Loop shape — last candidate kept"
    )

    history = result.transcript["history"]
    assert len(history) == 4, (
        f"Expected 4 history entries; got {len(history)}. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )
    actors = [(e["actor"], e["turn"]) for e in history]
    assert actors == [
        ("producer", 0),
        ("reviewer", 1),
        ("producer", 2),
        ("reviewer", 3),
    ], (
        f"History actors/turns out of order: {actors!r}. "
        "spec: BACKEND_LLM.md §Evidence shape §history"
    )

    # Reviewer turn 3 verdict must be accept
    assert history[3].get("verdict") == "accept", (
        f"Turn 3 reviewer verdict must be 'accept'; got {history[3].get('verdict')!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group C: turns_exhausted
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_turns_exhausted() -> None:
    """Loop reaches max_turns=4 without an accept; outcome=turns_exhausted, last candidate kept.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination
    — 'turns_exhausted: Reached max_turns without an accept; last candidate is kept.'
    Setup: P1(turn 0) → Reviewer revise(turn 1) → P2(turn 2) → Reviewer revise(turn 3).
    max_turns=4 triggers turns_exhausted after turn 3 (reviewer turn) even though
    Reviewer requested another revision.
    The LAST candidate (P2) must be kept — NOT P1.
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer → P1
        _revise_result(),       # turn 1: Reviewer → revise
        _producer_result_2(),   # turn 2: Producer → P2
        _revise_result(),       # turn 3: Reviewer → revise (but max_turns=4 reached)
    ])

    result = await _run(llm, max_turns=4)

    # spec: §Termination — turns_exhausted outcome
    assert result.outcome == "turns_exhausted", (
        f"Expected outcome='turns_exhausted'; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Termination"
    )

    assert result.transcript["turns_completed"] == 4, (
        f"Expected turns_completed=4 at exhaustion; got {result.transcript['turns_completed']!r}. "
        "spec: BACKEND_LLM.md §Loop shape — max_turns=4"
    )

    # spec: §Termination — 'last candidate is kept' — must be P2, NOT P1
    assert result.payload == _producer_payload_2(), (
        f"Expected last candidate (P2) on turns_exhausted; got {result.payload!r}. "
        "spec: BACKEND_LLM.md §Termination — 'last candidate is kept'"
    )

    # Verify P2 is different from P1 to confirm the right one was kept
    assert result.payload != _producer_payload_1(), (
        "Payload must be P2 (revised), NOT P1 (first candidate). "
        "spec: BACKEND_LLM.md §Termination"
    )

    history = result.transcript["history"]
    assert len(history) == 4, (
        f"Expected 4 history entries on turns_exhausted; got {len(history)}. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group D: cycle_detected
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_cycle_detected() -> None:
    """Producer emits P1-again (same canonical JSON as P1) on turn 2; cycle detected.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Cycle detection
    — 'If the new hash matches any prior Producer turn's hash, the loop terminates
    with outcome=cycle_detected and the last candidate is kept.'

    Setup: P1(turn 0) → Reviewer revise(turn 1) → P1-again(turn 2, identical hash).
    This is the immediate-prior pattern: the duplicate appears one revision after the
    original.  The skip-one pattern (non-adjacent repeat) is tested separately in
    test_run_debate_cycle_detected_skip_one_pattern.
    Expected: outcome='cycle_detected', turns_completed=3 (turn 0, 1, then cycle on 2
    — implementation appends the duplicate producer turn before returning),
    payload=P1 (the duplicate), history records the duplicate.
    """
    p1 = _producer_payload_1()
    llm = FakeLLM([
        LoopResult(payload=p1, trace=_trace()),     # turn 0: Producer → P1
        _revise_result(),                           # turn 1: Reviewer → revise
        LoopResult(payload=p1, trace=_trace()),     # turn 2: Producer → P1-AGAIN (cycle)
    ])

    result = await _run(llm, max_turns=6)

    # spec: §Cycle detection — outcome
    assert result.outcome == "cycle_detected", (
        f"Expected outcome='cycle_detected'; got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )

    # spec: §Cycle detection — 'last candidate is kept'
    assert result.payload == p1, (
        f"Expected cycle duplicate (P1) as payload; got {result.payload!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )

    # turns_completed: the implementation appends a producer turn entry before
    # returning on cycle, so turns_completed reflects turn index + 1
    # (turn 0 producer + turn 1 reviewer + turn 2 producer-cycle = 3 turns_completed)
    tc = result.transcript["turns_completed"]
    assert tc == 3, (
        f"Expected turns_completed=3 at cycle detection; got {tc!r}. "
        "spec: BACKEND_LLM.md §Cycle detection — cycle detected on Producer turn 2"
    )

    history = result.transcript["history"]
    # There must be at least 3 entries: P(0), R(1), P(2-cycle)
    assert len(history) >= 3, (
        f"Expected ≥3 history entries (P0, R1, P2-cycle); got {len(history)}. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )
    # The cycle-trigger turn must be recorded with the producer actor
    cycle_turn = history[-1]
    assert cycle_turn["actor"] == "producer", (
        f"Last history entry when cycle is detected must be actor='producer'; "
        f"got {cycle_turn['actor']!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )
    # candidate_hash must be present so the human reviewer can identify the cycle
    assert "candidate_hash" in cycle_turn, (
        "Cycle-trigger producer entry must carry candidate_hash. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )


@pytest.mark.asyncio
async def test_run_debate_cycle_detected_skip_one_pattern() -> None:
    """Cycle detection compares against ALL prior Producer hashes, not just the last.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Cycle detection
    — 'If the new hash matches any prior Producer turn's hash, the loop terminates.'
    The spec says "any prior", so a non-adjacent repeat must also trigger cycle_detected.

    Skip-one pattern:
      turn 0: Producer → P1
      turn 1: Reviewer → revise
      turn 2: Producer → P2  (different payload — no cycle yet)
      turn 3: Reviewer → revise
      turn 4: Producer → P1-again  (same hash as turn 0 — non-adjacent repeat)

    If the implementation used `candidate_hash == producer_hashes[-1]` (last only) instead
    of `candidate_hash in producer_hashes` (all prior), turn 4 would NOT be detected as a
    cycle because producer_hashes[-1] would be P2's hash, not P1's hash.
    This test would then fail with outcome != 'cycle_detected', exposing the regression.

    Expected: outcome='cycle_detected', turns_completed=5, payload=P1-again.
    """
    p1 = _producer_payload_1()
    p2 = _producer_payload_2()

    # Verify the two payloads actually have different hashes (precondition for the test)
    assert _canonical_hash(p1) != _canonical_hash(p2), (
        "Test precondition: P1 and P2 must have different canonical hashes. "
        "spec: BACKEND_LLM.md §Cycle detection — distinct payloads produce distinct hashes"
    )

    llm = FakeLLM([
        LoopResult(payload=p1, trace=_trace()),   # turn 0: Producer → P1
        _revise_result(),                          # turn 1: Reviewer → revise
        LoopResult(payload=p2, trace=_trace()),   # turn 2: Producer → P2 (different, no cycle)
        _revise_result(),                          # turn 3: Reviewer → revise
        LoopResult(payload=p1, trace=_trace()),   # turn 4: Producer → P1-again (non-adjacent cycle)
    ])

    result = await _run(llm, max_turns=8)

    # spec: §Cycle detection — non-adjacent repeat must still terminate cycle_detected
    assert result.outcome == "cycle_detected", (
        f"Expected outcome='cycle_detected' on non-adjacent P1→P2→P1 repeat; "
        f"got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §Cycle detection — 'any prior Producer turn's hash'"
    )

    # spec: §Cycle detection — 'last candidate is kept'
    assert result.payload == p1, (
        f"Expected cycle duplicate (P1) as payload; got {result.payload!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )

    # turns_completed: implementation appends the cycle-trigger producer turn before returning.
    # Pattern: P(0) R(1) P(2) R(3) P(4-cycle) = 5 turns_completed.
    tc = result.transcript["turns_completed"]
    assert tc == 5, (
        f"Expected turns_completed=5 at skip-one cycle detection; got {tc!r}. "
        "spec: BACKEND_LLM.md §Cycle detection — 5 turns consumed before cycle on turn 4"
    )

    history = result.transcript["history"]
    # Must have ≥ 5 entries: P(0), R(1), P(2), R(3), P(4-cycle)
    assert len(history) >= 5, (
        f"Expected ≥5 history entries (P0, R1, P2, R3, P4-cycle); got {len(history)}. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )
    # The cycle-trigger turn (last entry) must be actor='producer'
    assert history[-1]["actor"] == "producer", (
        f"Last history entry on skip-one cycle must be actor='producer'; "
        f"got {history[-1]['actor']!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group E: canonical_hash is key-order-independent
# ─────────────────────────────────────────────────────────────────────────────


def test_run_debate_canonical_hash_is_order_independent() -> None:
    """Two dicts with same content but different key insertion order produce the same hash.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Cycle detection
    — 'canonical: json.dumps(payload, sort_keys=True, separators=(",", ":"))'
    The SHA-256 hash must be identical regardless of Python dict key insertion order.
    """
    payload_a = {"nodes": [{"id": "book", "name": "Book"}], "edges": [], "triples": []}
    payload_b = {"triples": [], "nodes": [{"name": "Book", "id": "book"}], "edges": []}

    hash_a = _canonical_hash(payload_a)
    hash_b = _canonical_hash(payload_b)

    assert hash_a == hash_b, (
        f"Same-content payloads with different key order must hash identically; "
        f"hash_a={hash_a!r}, hash_b={hash_b!r}. "
        "spec: BACKEND_LLM.md §Cycle detection — sort_keys=True"
    )


def test_run_debate_canonical_hash_different_content_differs() -> None:
    """Two payloads with different content produce different hashes.

    Spec: BACKEND_LLM.md §Cycle detection — SHA-256 distinguishes distinct payloads.
    """
    p1 = {"nodes": [{"id": "book", "confidence_score": 0.95}]}
    p2 = {"nodes": [{"id": "book", "confidence_score": 0.70}]}
    assert _canonical_hash(p1) != _canonical_hash(p2), (
        "Payloads with different content must produce different hashes. "
        "spec: BACKEND_LLM.md §Cycle detection"
    )


def test_canonical_hash_uses_sha256_and_sort_keys() -> None:
    """_canonical_hash produces a specific, hardcoded SHA-256 for a known input.

    Spec: BACKEND_LLM.md §Cycle detection
    — 'canonicalise: json.dumps(payload, sort_keys=True, separators=(",", ":"))'
    The expected hex was computed once against the spec's canonical-form definition:
      canonical form: json.dumps({"b": 2, "a": 1}, sort_keys=True, separators=(",", ":"))
                    → '{"a":1,"b":2}'
      SHA-256 hex:   43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777
    (computed via: python3 -c
     "import hashlib; print(hashlib.sha256(b'{\"a\":1,\"b\":2}').hexdigest())")

    Pinning against a pre-computed constant (not against the impl algorithm) ensures that
    a refactor to a different hash or serialisation algorithm surfaces as a test failure
    rather than silently passing.
    """
    # Expected hex pinned against spec canonical form — not derived from impl.
    # Any change to hash algorithm or JSON serialisation must update this constant deliberately.
    EXPECTED_HEX = "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"

    payload = {"b": 2, "a": 1}
    actual_hash = _canonical_hash(payload)

    assert actual_hash == EXPECTED_HEX, (
        f"_canonical_hash must produce SHA-256 of sort_keys canonical JSON; "
        f"expected={EXPECTED_HEX!r}, actual={actual_hash!r}. "
        "spec: BACKEND_LLM.md §Cycle detection"
        " — canonical form: json.dumps(sort_keys=True, separators=(',', ':'))"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group F: empty RAG anchors (cold-start)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_empty_rag_anchors_cold_start() -> None:
    """When all three pgvector searches return empty lists, debate still proceeds.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §RAG anchors — 'Cold start.
    When approved-item sets are empty, the Reviewer simply runs without anchor grounding.
    No special fallback prompt — the Reviewer relies on its own training.'

    This test ensures no exception is raised and the debate terminates normally
    when no approved nodes/edges/triples exist yet.  The search helpers are already
    patched to return [] in _run(); this test just makes the happy-path explicit.
    """
    llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer
        _accept_result(),       # turn 1: Reviewer — accepts even without RAG
    ])

    result = await _run(llm)

    # spec: §Termination — accept terminates normally even without anchors
    assert result.outcome == "accept", (
        f"Expected outcome='accept' on cold start (no RAG anchors); got {result.outcome!r}. "
        "spec: BACKEND_LLM.md §RAG anchors — cold start"
    )
    # rag_anchors list must exist and be empty (no approved items to sample from)
    assert result.transcript["rag_anchors"] == [], (
        f"Expected empty rag_anchors list on cold start; got {result.transcript['rag_anchors']!r}. "
        "spec: BACKEND_LLM.md §Evidence shape"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group G: reviewer_model override wires make_llm
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_debate_reviewer_model_override() -> None:
    """When reviewer_model is set, make_llm(model_override=...) is called for the Reviewer.

    Spec: BACKEND_LLM.md §Settings Reference — DATASPOKE_ONTOGEN_DEBATE_REVIEWER_MODEL
    — 'When set, instantiate a second LLMClient with the override model for the Reviewer
    turns only.'
    Spec: debate.py docstring — 'Use make_llm so test-mode stubbing applies to the
    Reviewer regardless of whether a model override is set.'

    The test patches make_llm at its import site in debate.py and asserts it was called
    with model_override='some-other-model'.  A bare LLMClient() construction would bypass
    stubbing; make_llm() is the spec-mandated factory.
    """

    producer_llm = FakeLLM([
        _producer_result_1(),   # turn 0: Producer
    ])
    # Reviewer LLM also needs to return an accept result
    reviewer_fake_llm = FakeLLM([
        _accept_result(),       # turn 1: Reviewer
    ])

    db = MagicMock()
    vector = MagicMock()

    _empty: list[Any] = []
    with (
        patch(
            "src.backend.ontogen.debate._search_node_embeddings",
            new=AsyncMock(return_value=_empty),
        ),
        patch(
            "src.backend.ontogen.debate._search_edge_embeddings",
            new=AsyncMock(return_value=_empty),
        ),
        patch(
            "src.backend.ontogen.debate._search_triple_embeddings",
            new=AsyncMock(return_value=_empty),
        ),
        patch(
            "src.backend.ontogen.debate.make_llm",
            return_value=reviewer_fake_llm,
        ) as mock_make_llm,
    ):
        result = await run_debate(
            llm=producer_llm,  # type: ignore[arg-type]
            vector=vector,
            db=db,
            producer_prompt="produce ontology",
            validate_tool=_fake_validate_tool(),
            review_tool=_fake_review_tool(),
            in_scope_urns=frozenset(["urn:x"]),
            max_turns=4,
            rag_k=2,
            reviewer_model="some-other-model",
            llm_provider="openai",
            llm_base_model="gpt-4o",
            producer_schema=MagicMock(),
            producer_max_iterations=3,
            run_id="test-run-id",
        )

    # spec: debate.py wiring — make_llm must be called when reviewer_model is set
    assert mock_make_llm.called, "make_llm must be called when reviewer_model is set."
    call_kwargs = mock_make_llm.call_args.kwargs
    assert call_kwargs.get("model_override") == "some-other-model"
    assert result.outcome == "accept", (
        f"Debate with model override must still terminate on accept; got {result.outcome!r}."
    )
