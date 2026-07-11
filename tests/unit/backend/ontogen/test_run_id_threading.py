"""Unit tests for run_id generation and threading in the ontogen pipeline.

Spec: spec/feature/BACKEND_LLM.md §Observability
      spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline

Groups:
  A – run_id is a valid UUID4 in ONTOGEN_RUN_COMPLETE event detail
  B – run_id is present in ONTOGEN_RUN_FAILED event detail on failure path
  C – run_debate receives session_id=run_id and correct actor/turn metadata
  D – StubLLMClient accepts session_id/metadata without raising (regression guard)
"""

import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ontogen.debate import run_debate
from src.backend.ontogen.debate_models import DebateResult
from src.backend.ontogen.service import OntogenRunSummary, OntogenService
from src.shared.db.models import Event
from src.shared.events import ONTOGEN_RUN_COMPLETE, ONTOGEN_RUN_FAILED
from src.shared.llm.loop_trace import LoopResult, LoopTrace
from tests.unit.conftest import route_db_execute

# UUID4 pattern (version=4, variant bits 8-b)
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ── Reusable service fixture builder ─────────────────────────────────────────


def _make_svc(
    datahub: AsyncMock,
    db: AsyncMock,
    cache: AsyncMock,
    llm: AsyncMock,
    vector: AsyncMock,
) -> OntogenService:
    return OntogenService(
        datahub=datahub,
        db=db,
        cache=cache,
        llm=llm,
        vector=vector,
    )


def _make_result(*, scalar_val=None, scalars_val=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar_val
    ms = MagicMock()
    ms.all.return_value = scalars_val or []
    m.scalars.return_value = ms
    m.scalar.return_value = 0
    return m


def _enabled_conf() -> MagicMock:
    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}
    return conf


def _debate_stub() -> DebateResult:
    return DebateResult(
        payload={"nodes": [], "edges": [], "triples": []},
        transcript={
            "turns_completed": 1,
            "outcome": "accept",
            "final_reviewer_verdict": "accept",
            "rag_anchors": [],
            "history": [],
            "producer_iterations": 1,
            "producer_errors_dropped": 0,
            "item_verdicts": [],
        },
        outcome="accept",
    )


def _setup_db_for_dry_run(db: AsyncMock, conf: MagicMock) -> None:
    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf
    # Route get_conf (ontogen_config); the seed + eligible node/edge/triple list queries
    # all return the same empty result via the default.
    route_db_execute(
        db, [("ontogen_config", conf_result)], default=_make_result(scalars_val=[])
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group A: run_id is a valid UUID4 in ONTOGEN_RUN_COMPLETE event detail
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_complete_event_detail_contains_uuid4_run_id(
    datahub: AsyncMock,
    db: AsyncMock,
    cache: AsyncMock,
    llm: AsyncMock,
    vector: AsyncMock,
) -> None:
    """OntogenService.run() generates a UUID4 run_id and records it in ONTOGEN_RUN_COMPLETE detail.

    Spec: BACKEND_LLM.md §Observability — 'generate run_id (uuid4) in service.run();
    record it in ONTOGEN_RUN_COMPLETE event detail so operators can pivot from
    DataSpoke event → Langfuse session URL.'
    """
    svc = _make_svc(datahub, db, cache, llm, vector)
    conf = _enabled_conf()

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)
    _setup_db_for_dry_run(db, conf)
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    with (
        patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"),
        patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=_debate_stub())),
    ):
        summary = await svc.run(dry_run=True)

    assert isinstance(summary, OntogenRunSummary), (
        f"run() must return OntogenRunSummary; got {type(summary)!r}."
    )

    added_args = [call.args[0] for call in db.add.call_args_list]
    event_rows = [a for a in added_args if isinstance(a, Event)]
    complete_events = [e for e in event_rows if e.event_type == ONTOGEN_RUN_COMPLETE]

    assert len(complete_events) == 1, (
        f"Expected exactly one ONTOGEN_RUN_COMPLETE event; got {len(complete_events)}. "
        "Spec: BACKEND.md §Event Catalogue (ONTOGEN.RUN_COMPLETE)"
    )

    detail = complete_events[0].detail
    assert "run_id" in detail, (
        f"ONTOGEN_RUN_COMPLETE detail must contain 'run_id'; got keys {list(detail.keys())!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )

    run_id = detail["run_id"]
    assert isinstance(run_id, str) and _UUID4_RE.match(run_id), (
        f"detail['run_id'] must match UUID4 pattern; got {run_id!r}. "
        "Spec: BACKEND_LLM.md §Observability — run_id is uuid4 generated in service.run()"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group B: run_id is present in ONTOGEN_RUN_FAILED event detail on failure path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_failed_event_detail_contains_uuid4_run_id(
    datahub: AsyncMock,
    db: AsyncMock,
    cache: AsyncMock,
    llm: AsyncMock,
    vector: AsyncMock,
) -> None:
    """When run_debate raises, ONTOGEN_RUN_FAILED detail must contain a valid UUID4 run_id.

    Spec: BACKEND_LLM.md §Observability — run_id is threaded into both RUN_COMPLETE
    and RUN_FAILED events so a failure can also be linked to any partial Langfuse trace.
    Spec: BACKEND.md §Ontology Generation Service — failure path emits ONTOGEN_RUN_FAILED
    with run_id in detail.
    """
    svc = _make_svc(datahub, db, cache, llm, vector)
    conf = _enabled_conf()

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)
    _setup_db_for_dry_run(db, conf)
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    # Route get_conf (ontogen_config); the seed + eligible list queries and the
    # post-failure _record_ontogen_event query all return the empty default.
    route_db_execute(
        db,
        [("ontogen_config", MagicMock(**{"scalar_one_or_none.return_value": conf}))],
        default=_make_result(scalars_val=[]),
    )

    with (
        patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"),
        patch(
            "src.backend.ontogen.service.run_debate",
            new=AsyncMock(side_effect=RuntimeError("simulated LLM failure")),
        ),
    ):
        with pytest.raises(RuntimeError, match="simulated LLM failure"):
            await svc.run(dry_run=True)

    added_args = [call.args[0] for call in db.add.call_args_list]
    event_rows = [a for a in added_args if isinstance(a, Event)]
    failed_events = [e for e in event_rows if e.event_type == ONTOGEN_RUN_FAILED]

    assert len(failed_events) == 1, (
        f"Expected exactly one ONTOGEN_RUN_FAILED event; got {len(failed_events)}. "
        "Spec: BACKEND.md §Ontology Generation Service — failure path emits RUN_FAILED"
    )

    detail = failed_events[0].detail
    assert "run_id" in detail, (
        f"ONTOGEN_RUN_FAILED detail must contain 'run_id'; got keys {list(detail.keys())!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )

    run_id = detail["run_id"]
    assert isinstance(run_id, str) and _UUID4_RE.match(run_id), (
        f"detail['run_id'] in RUN_FAILED must match UUID4 pattern; got {run_id!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group C: run_debate receives session_id=run_id and actor/turn metadata
# ─────────────────────────────────────────────────────────────────────────────


class _SpyLLM:
    """Spy LLMClient that captures session_id and metadata from complete_with_tools calls."""

    def __init__(self, producer_result: LoopResult, reviewer_result: LoopResult) -> None:
        self._script = [producer_result, reviewer_result]
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
        self.calls.append(
            {
                "session_id": session_id,
                "metadata": dict(metadata) if metadata else None,
                "success_tool_name": success_tool_name,
            }
        )
        if not self._script:
            raise RuntimeError("_SpyLLM script exhausted")
        return self._script.pop(0)

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1536


@pytest.mark.asyncio
async def test_run_debate_passes_run_id_as_session_id_to_llm() -> None:
    """run_debate passes session_id=run_id and correct actor/turn metadata to LLMClient.

    Spec: BACKEND_LLM.md §Observability — 'Producer turn calls LLMClient with
    session_id=run_id, metadata={"actor": "producer", "turn": producer_turn}.
    Reviewer turn likewise with "reviewer".'
    """
    producer_result = LoopResult(
        payload={
            "nodes": [
                {"name": "Book", "id": "book", "confidence_score": 0.9, "dataset_urns": ["urn:x"]}
            ],
            "edges": [],
            "triples": [],
        },
        trace=LoopTrace(iterations=1, errors_per_iter=[], final_errors=[]),
    )
    reviewer_result = LoopResult(
        payload={"overall_verdict": "accept", "item_verdicts": [], "summary": "ok"},
        trace=LoopTrace(iterations=1, errors_per_iter=[], final_errors=[]),
    )

    spy = _SpyLLM(producer_result, reviewer_result)
    the_run_id = str(uuid.uuid4())

    fake_validate_tool = MagicMock()
    fake_validate_tool.name = "ontogen_validate"
    fake_review_tool = MagicMock()
    fake_review_tool.name = "ontogen_review"

    db = MagicMock()
    vector = MagicMock()

    with (
        patch("src.backend.ontogen.debate._search_node_embeddings", new=AsyncMock(return_value=[])),
        patch("src.backend.ontogen.debate._search_edge_embeddings", new=AsyncMock(return_value=[])),
        patch(
            "src.backend.ontogen.debate._search_triple_embeddings",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.backend.ontogen.debate.make_llm_client",
            return_value=spy,
        ),
    ):
        result = await run_debate(
            llm=spy,  # type: ignore[arg-type]
            vector=vector,
            db=db,
            producer_prompt="produce ontology",
            validate_tool=fake_validate_tool,
            review_tool=fake_review_tool,
            in_scope_urns=frozenset(["urn:x"]),
            max_turns=4,
            rag_k=2,
            reviewer_model=None,
            llm_provider="openai",
            llm_base_model="gpt-4o",
            producer_schema=MagicMock(),
            producer_max_iterations=3,
            run_id=the_run_id,
        )

    assert result.outcome == "accept", (
        f"Expected accept outcome in spy debate; got {result.outcome!r}."
    )

    assert len(spy.calls) >= 2, (
        f"Expected at least 2 LLM calls (producer + reviewer); got {len(spy.calls)}."
    )

    # Producer turn (calls[0])
    producer_call = spy.calls[0]
    assert producer_call["session_id"] == the_run_id, (
        f"Producer call must have session_id=run_id={the_run_id!r}; "
        f"got {producer_call['session_id']!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    assert producer_call["metadata"] is not None, (
        "Producer call must have metadata; got None. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    assert producer_call["metadata"].get("actor") == "producer", (
        f"Producer call metadata must have actor='producer'; "
        f"got {producer_call['metadata']!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    assert producer_call["metadata"].get("turn") == 0, (
        f"Producer call metadata must have turn=0; "
        f"got {producer_call['metadata'].get('turn')!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )

    # Reviewer turn (calls[1])
    reviewer_call = spy.calls[1]
    assert reviewer_call["session_id"] == the_run_id, (
        f"Reviewer call must have session_id=run_id={the_run_id!r}; "
        f"got {reviewer_call['session_id']!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    assert reviewer_call["metadata"] is not None, (
        "Reviewer call must have metadata; got None. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    assert reviewer_call["metadata"].get("actor") == "reviewer", (
        f"Reviewer call metadata must have actor='reviewer'; "
        f"got {reviewer_call['metadata']!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )
    assert reviewer_call["metadata"].get("turn") == 1, (
        f"Reviewer call metadata must have turn=1; "
        f"got {reviewer_call['metadata'].get('turn')!r}. "
        "Spec: BACKEND_LLM.md §Observability"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group D: StubLLMClient accepts session_id/metadata without raising (regression guard)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stub_llm_client_accepts_session_id_and_metadata_without_raising() -> None:
    """StubLLMClient.complete_with_tools must accept session_id/metadata kwargs without error.

    Regression for the test-mode crash: before the fix, StubLLMClient.complete_with_tools
    did not declare session_id/metadata as keyword args and raised TypeError when the
    debate code passed them.

    Spec: src/workflows/_stubs.py — StubLLMClient is the test-mode drop-in;
    it must accept all kwargs that the real LLMClient accepts.
    BACKEND_LLM.md §Observability — session_id/metadata are now part of the call signature.
    """
    from src.workflows._stubs import StubLLMClient

    class _DummySchema(MagicMock):
        pass

    stub = StubLLMClient()

    # Must not raise TypeError even though session_id/metadata are present
    loop_result = await stub.complete_with_tools(
        prompt="test prompt",
        tools=[],
        success_tool_name="ontogen_review",  # special-cased in StubLLMClient
        schema=MagicMock(),
        session_id="test-session-id",
        metadata={"actor": "producer", "turn": 0},
    )

    assert loop_result is not None, (
        "StubLLMClient.complete_with_tools must return a LoopResult without raising. "
        "Spec: src/workflows/_stubs.py — StubLLMClient is the test-mode drop-in."
    )
    # The ontogen_review branch returns overall_verdict='accept'
    assert loop_result.payload.get("overall_verdict") == "accept", (
        f"StubLLMClient ontogen_review canned result must have overall_verdict='accept'; "
        f"got {loop_result.payload!r}. "
        "Spec: src/workflows/_stubs.py — StubLLMClient canned ontogen_review result."
    )
