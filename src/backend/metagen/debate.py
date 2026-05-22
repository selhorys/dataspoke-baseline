"""Producer/Reviewer adversarial debate loop for metagen.

Spec: spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate
"""

import hashlib
import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metagen.debate_models import (
    DebateHistoryEntry,
    DebateResult,
    MetagenRAGAnchor,
    MetagenReviewOutput,
)
from src.backend.metagen.embedding_search import search_candidate_embeddings
from src.backend.metagen.prompts_reviewer import (
    build_producer_revision_prompt,
    build_reviewer_prompt,
)
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager
from src.workflows._common import make_llm

logger = logging.getLogger(__name__)


def _canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def run_debate(
    *,
    llm: LLMClient,
    vector: PgVectorManager,
    db: AsyncSession,
    producer_prompt: str,
    validate_tool: StructuredTool,
    review_tool: StructuredTool,
    in_scope_urns: frozenset[str],
    max_turns: int,
    rag_k: int,
    reviewer_model: str | None,
    llm_provider: str,
    llm_base_model: str,
    producer_schema: type[BaseModel],
    producer_max_iterations: int,
    run_id: str,
) -> DebateResult:
    """Run the adversarial Producer/Reviewer debate loop for metagen.

    Termination conditions: Reviewer accept, max_turns exhausted, or cycle
    detected (Producer's revised payload duplicates a prior turn's hash).

    Unlike ontogen, metagen drops all candidates on turns_exhausted or
    cycle_detected — there is no llm_pending fallback.
    """
    reviewer_llm: LLMClient = (
        make_llm(provider=llm_provider, model=llm_base_model, model_override=reviewer_model)
        if reviewer_model
        else llm
    )

    producer_hashes: list[str] = []
    history: list[DebateHistoryEntry] = []
    current_prompt = producer_prompt
    last_producer_payload: dict[str, Any] = {}
    last_reviewer_payload: dict[str, Any] = {}
    producer_trace_iterations: int = 1
    producer_trace_errors: int = 0
    all_rag_anchors: list[dict[str, Any]] = []
    turns_completed: int = 0

    for turn_pair in range(max_turns // 2 + max_turns % 2):
        producer_turn = turn_pair * 2
        reviewer_turn = producer_turn + 1

        # ── Producer turn ──────────────────────────────────────────────────
        loop_result = await llm.complete_with_tools(
            current_prompt,
            tools=[validate_tool],
            success_tool_name="metagen_validate",
            schema=producer_schema,
            max_iterations=producer_max_iterations,
            session_id=run_id,
            metadata={"actor": "producer", "turn": producer_turn},
        )

        producer_trace_iterations = loop_result.trace.iterations
        producer_trace_errors = sum(len(errs) for errs in loop_result.trace.errors_per_iter)
        last_producer_payload = loop_result.payload

        candidate_hash = _canonical_hash(last_producer_payload)

        if candidate_hash in producer_hashes:
            history.append(
                DebateHistoryEntry(
                    turn=producer_turn,
                    actor="producer",
                    candidate_hash=candidate_hash[:16],
                )
            )
            turns_completed = producer_turn + 1
            return DebateResult(
                payload=last_producer_payload,
                transcript=_build_transcript(
                    turns_completed=turns_completed,
                    outcome="cycle_detected",
                    final_reviewer_verdict=last_reviewer_payload.get("overall_verdict", ""),
                    history=history,
                    rag_anchors=all_rag_anchors,
                    producer_iterations=producer_trace_iterations,
                    producer_errors_dropped=producer_trace_errors,
                ),
                outcome="cycle_detected",
            )

        producer_hashes.append(candidate_hash)

        history.append(
            DebateHistoryEntry(
                turn=producer_turn,
                actor="producer",
                candidate_hash=candidate_hash[:16],
            )
        )
        turns_completed = producer_turn + 1

        if turns_completed >= max_turns:
            return DebateResult(
                payload=last_producer_payload,
                transcript=_build_transcript(
                    turns_completed=turns_completed,
                    outcome="turns_exhausted",
                    final_reviewer_verdict=last_reviewer_payload.get("overall_verdict", ""),
                    history=history,
                    rag_anchors=all_rag_anchors,
                    producer_iterations=producer_trace_iterations,
                    producer_errors_dropped=producer_trace_errors,
                ),
                outcome="turns_exhausted",
            )

        # ── RAG anchor sampling ────────────────────────────────────────────
        rag_anchors_by_kind = await _sample_rag_anchors(
            llm=llm,
            vector=vector,
            payload=last_producer_payload,
            rag_k=rag_k,
        )
        all_rag_anchors = _flatten_anchors(rag_anchors_by_kind)

        # ── Reviewer turn ──────────────────────────────────────────────────
        reviewer_prompt = build_reviewer_prompt(
            candidate=last_producer_payload,
            rag_anchors=rag_anchors_by_kind,
            in_scope_urns=in_scope_urns,
            nonce=candidate_hash[:8],
        )

        reviewer_result = await reviewer_llm.complete_with_tools(
            reviewer_prompt,
            tools=[review_tool],
            success_tool_name="metagen_review",
            schema=MetagenReviewOutput,
            max_iterations=1,
            session_id=run_id,
            metadata={"actor": "reviewer", "turn": reviewer_turn},
        )

        last_reviewer_payload = reviewer_result.payload

        try:
            review_output = MetagenReviewOutput.model_validate(last_reviewer_payload)
        except Exception:
            logger.warning(
                "metagen_debate_reviewer_output_invalid",
                extra={"turn": reviewer_turn},
                exc_info=True,
            )
            review_output = MetagenReviewOutput(
                overall_verdict="revise",
                item_verdicts=[],
                summary="reviewer output could not be parsed",
            )

        issues_seen: list[str] = list(
            {
                str(issue)
                for iv in review_output.item_verdicts
                for issue in iv.issues
            }
        )
        history.append(
            DebateHistoryEntry(
                turn=reviewer_turn,
                actor="reviewer",
                verdict=review_output.overall_verdict,
                issues=issues_seen or None,
                comment_summary=review_output.summary[:120] if review_output.summary else None,
                item_verdicts_count=len(review_output.item_verdicts),
            )
        )
        turns_completed = reviewer_turn + 1

        last_item_verdicts = [iv.model_dump() for iv in review_output.item_verdicts]

        if review_output.overall_verdict == "accept":
            return DebateResult(
                payload=last_producer_payload,
                transcript=_build_transcript(
                    turns_completed=turns_completed,
                    outcome="accept",
                    final_reviewer_verdict="accept",
                    history=history,
                    rag_anchors=all_rag_anchors,
                    producer_iterations=producer_trace_iterations,
                    producer_errors_dropped=producer_trace_errors,
                    item_verdicts=last_item_verdicts,
                ),
                outcome="accept",
            )

        if turns_completed >= max_turns:
            return DebateResult(
                payload=last_producer_payload,
                transcript=_build_transcript(
                    turns_completed=turns_completed,
                    outcome="turns_exhausted",
                    final_reviewer_verdict=review_output.overall_verdict,
                    history=history,
                    rag_anchors=all_rag_anchors,
                    producer_iterations=producer_trace_iterations,
                    producer_errors_dropped=producer_trace_errors,
                    item_verdicts=last_item_verdicts,
                ),
                outcome="turns_exhausted",
            )

        current_prompt = build_producer_revision_prompt(
            prior_candidate=last_producer_payload,
            reviewer_payload=last_reviewer_payload,
            original_prompt=producer_prompt,
            nonce=candidate_hash[:8],
        )

    return DebateResult(
        payload=last_producer_payload,
        transcript=_build_transcript(
            turns_completed=turns_completed,
            outcome="turns_exhausted",
            final_reviewer_verdict=last_reviewer_payload.get("overall_verdict", ""),
            history=history,
            rag_anchors=all_rag_anchors,
            producer_iterations=producer_trace_iterations,
            producer_errors_dropped=producer_trace_errors,
        ),
        outcome="turns_exhausted",
    )


def _build_transcript(
    *,
    turns_completed: int,
    outcome: str,
    final_reviewer_verdict: str,
    history: list[DebateHistoryEntry],
    rag_anchors: list[dict[str, Any]],
    producer_iterations: int,
    producer_errors_dropped: int,
    item_verdicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    history_dicts: list[dict[str, Any]] = []
    for entry in history:
        d: dict[str, Any] = {"turn": entry.turn, "actor": entry.actor}
        if entry.candidate_hash is not None:
            d["candidate_hash"] = entry.candidate_hash
        if entry.verdict is not None:
            d["verdict"] = entry.verdict
        if entry.issues:
            d["issues_seen"] = entry.issues
        if entry.comment_summary is not None:
            d["comment_summary"] = entry.comment_summary
        if entry.item_verdicts_count is not None:
            d["item_verdicts_count"] = entry.item_verdicts_count
        history_dicts.append(d)

    return {
        "turns_completed": turns_completed,
        "outcome": outcome,
        "final_reviewer_verdict": final_reviewer_verdict,
        "rag_anchors": rag_anchors,
        "history": history_dicts,
        "producer_iterations": producer_iterations,
        "producer_errors_dropped": producer_errors_dropped,
        "item_verdicts": item_verdicts or [],
    }


def _flatten_anchors(
    rag_anchors_by_kind: dict[str, list[MetagenRAGAnchor]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for kind, anchors in rag_anchors_by_kind.items():
        for anchor in anchors:
            result.append(
                {
                    "kind": kind,
                    "dataset_urn": anchor.dataset_urn,
                    "item_id": anchor.item_id,
                    "similarity": anchor.similarity,
                }
            )
    return result


async def _sample_rag_anchors(
    *,
    llm: LLMClient,
    vector: PgVectorManager,
    payload: dict[str, Any],
    rag_k: int,
) -> dict[str, list[MetagenRAGAnchor]]:
    """Sample RAG anchors for a Producer candidate, grouped by kind."""
    result: dict[str, list[MetagenRAGAnchor]] = {
        "dataset.description": [],
        "column.description": [],
    }

    anchor_map: dict[str, dict[str, MetagenRAGAnchor]] = {
        "dataset.description": {},
        "column.description": {},
    }

    for cand in payload.get("candidates", []) or []:
        item_id = cand.get("item_id", "")
        value = (cand.get("value") or "").strip()
        dataset_urn = cand.get("dataset_urn", "")

        if item_id == "dataset.description":
            kind = "dataset.description"
        elif item_id.startswith("column.") and item_id.endswith(".description"):
            kind = "column.description"
        else:
            continue

        if not value:
            continue

        try:
            query_vec = await llm.embed(value)
            hits = await search_candidate_embeddings(
                vector, query_vec, kind=kind, top_k=rag_k, threshold=None
            )
            for hit in hits:
                cid = hit.dataset_urn  # candidate_id stored in dataset_urn field
                if cid not in anchor_map[kind]:
                    anchor_map[kind][cid] = MetagenRAGAnchor(
                        kind=kind,  # type: ignore[arg-type]
                        dataset_urn=hit.payload.get("dataset_urn", ""),
                        item_id=hit.payload.get("item_id", ""),
                        value=hit.payload.get("value", ""),
                        similarity=float(hit.score),
                    )
        except Exception:
            logger.warning(
                "metagen_debate_anchor_sampling_failed",
                extra={"dataset_urn": dataset_urn, "item_id": item_id},
                exc_info=True,
            )

    for kind in result:
        result[kind] = list(anchor_map[kind].values())[: rag_k * 2]

    return result
