"""Producer/Reviewer adversarial debate loop for ontogen.

Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework
"""

import hashlib
import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ontogen.debate_models import (
    DebateHistoryEntry,
    DebateResult,
    RAGAnchor,
    ReviewOutput,
)
from src.backend.ontogen.embedding_search import (
    search_edge_embeddings as _search_edge_embeddings,
)
from src.backend.ontogen.embedding_search import (
    search_node_embeddings as _search_node_embeddings,
)
from src.backend.ontogen.embedding_search import (
    search_triple_embeddings as _search_triple_embeddings,
)
from src.backend.ontogen.prompts_reviewer import (
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
    """Run the adversarial Producer/Reviewer debate loop.

    Terminates on: Reviewer accept, max_turns exhausted, or cycle detected
    (Producer's revised payload duplicates a prior turn's hash).
    """
    # Use make_llm so test-mode stubbing applies to the Reviewer regardless of
    # whether a model override is set (direct LLMClient construction bypasses stubs).
    # Langfuse tracing is not wired to the reviewer; it uses the same provider/key
    # as the producer but with a different model.
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
            success_tool_name="ontogen_validate",
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
            success_tool_name="ontogen_review",
            schema=ReviewOutput,
            max_iterations=1,
            session_id=run_id,
            metadata={"actor": "reviewer", "turn": reviewer_turn},
        )

        last_reviewer_payload = reviewer_result.payload

        try:
            review_output = ReviewOutput.model_validate(last_reviewer_payload)
        except Exception:
            logger.warning(
                "ontogen_debate_reviewer_output_invalid",
                extra={"turn": reviewer_turn},
                exc_info=True,
            )
            review_output = ReviewOutput(
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

        # Build revision prompt for next Producer turn.
        # Pass the same nonce that was used to fence the Reviewer prompt so the
        # untrusted-data markers are consistent across turns.
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
        if entry.applied:
            d["applied"] = entry.applied
        if entry.rebuttals:
            d["rebuttals"] = entry.rebuttals
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


def _flatten_anchors(rag_anchors_by_kind: dict[str, list[RAGAnchor]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for kind, anchors in rag_anchors_by_kind.items():
        for anchor in anchors:
            entry: dict[str, Any] = {
                "kind": kind,
                "approved_id": anchor.approved_id,
                "similarity": anchor.similarity,
            }
            result.append(entry)
    return result


async def _sample_rag_anchors(
    *,
    llm: LLMClient,
    vector: PgVectorManager,
    payload: dict[str, Any],
    rag_k: int,
) -> dict[str, list[RAGAnchor]]:
    """Sample RAG anchors for a Producer candidate across all three kinds."""
    result: dict[str, list[RAGAnchor]] = {"node": [], "edge": [], "triple": []}

    # ── Nodes ──────────────────────────────────────────────────────────────
    node_anchor_map: dict[str, RAGAnchor] = {}
    for node in payload.get("nodes", []) or []:
        name = (node.get("name") or "").strip()
        description = (node.get("description") or "").strip()
        if not name:
            continue
        try:
            query_vec = await llm.embed(f"{name} {description}")
            # threshold=None: RAG context wants all top-k hits, not just reuse-guard filtered ones.
            hits = await _search_node_embeddings(vector, query_vec, top_k=rag_k, threshold=None)
            for hit in hits:
                aid = hit.dataset_urn
                if aid not in node_anchor_map:
                    node_anchor_map[aid] = RAGAnchor(
                        kind="node",
                        approved_id=aid,
                        similarity=float(hit.score),
                        name=hit.payload.get("name") or None,
                    )
        except Exception:
            logger.warning(
                "ontogen_debate_node_anchor_sampling_failed",
                extra={"node_name": name},
                exc_info=True,
            )

    result["node"] = list(node_anchor_map.values())[: rag_k * 2]

    # ── Edges ──────────────────────────────────────────────────────────────
    edge_anchor_map: dict[str, RAGAnchor] = {}
    for edge in payload.get("edges", []) or []:
        label = (edge.get("label") or "").strip()
        sem = (edge.get("semantics") or "").strip()
        if not label:
            continue
        try:
            query_vec = await llm.embed(f"{label} {sem}")
            hits = await _search_edge_embeddings(vector, query_vec, top_k=rag_k, threshold=None)
            for hit in hits:
                aid = hit.dataset_urn
                if aid not in edge_anchor_map:
                    edge_anchor_map[aid] = RAGAnchor(
                        kind="edge",
                        approved_id=aid,
                        similarity=float(hit.score),
                        label=label,
                        semantics=sem or None,
                    )
        except Exception:
            logger.warning(
                "ontogen_debate_edge_anchor_sampling_failed",
                extra={"edge_label": label},
                exc_info=True,
            )

    result["edge"] = list(edge_anchor_map.values())[: rag_k * 2]

    # ── Triples ────────────────────────────────────────────────────────────
    node_by_id: dict[str, dict[str, Any]] = {
        n["id"]: n for n in (payload.get("nodes", []) or []) if n.get("id")
    }
    edge_by_id: dict[str, dict[str, Any]] = {
        e["id"]: e for e in (payload.get("edges", []) or []) if e.get("id")
    }

    triple_anchor_map: dict[str, RAGAnchor] = {}
    for triple in payload.get("triples", []) or []:
        subj_id = triple.get("subject_node_id", "")
        edge_id = triple.get("edge_id", "")
        obj_id = triple.get("object_node_id", "")
        subj = node_by_id.get(subj_id, {})
        edge_obj = edge_by_id.get(edge_id, {})
        obj = node_by_id.get(obj_id, {})

        subj_name = (subj.get("name") or "").strip()
        subj_desc = (subj.get("description") or "").strip()
        edge_label = (edge_obj.get("label") or "").strip()
        edge_sem = (edge_obj.get("semantics") or "").strip()
        obj_name = (obj.get("name") or "").strip()
        obj_desc = (obj.get("description") or "").strip()

        composite = f"{subj_name} {subj_desc} {edge_label} {edge_sem} {obj_name} {obj_desc}".strip()
        if not composite:
            continue

        try:
            query_vec = await llm.embed(composite)
            hits = await _search_triple_embeddings(vector, query_vec, top_k=rag_k, threshold=None)
            for hit in hits:
                aid = hit.dataset_urn
                if aid not in triple_anchor_map:
                    triple_anchor_map[aid] = RAGAnchor(
                        kind="triple",
                        approved_id=aid,
                        similarity=float(hit.score),
                        description=composite[:200],
                    )
        except Exception:
            logger.warning(
                "ontogen_debate_triple_anchor_sampling_failed",
                extra={"triple_subj": subj_id, "triple_obj": obj_id},
                exc_info=True,
            )

    result["triple"] = list(triple_anchor_map.values())[: rag_k * 2]

    return result
