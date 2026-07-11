"""Unit tests for the ontogen adversarial Reviewer / Producer-revision prompt builders.

These assert the Reviewer prompt encodes the spec'd debate *contract* — the canonical
ontogen issue taxonomy, the three overall verdicts, the tool the Reviewer must call, the
in-scope URN set, RAG anchors segmented by kind (node/edge/triple), cold-start behaviour,
and the composite triple item_id format — rather than incidental string layout.

Spec:
  spec/feature/BACKEND_LLM.md §Reviewer tool → Issue taxonomy — ontogen issue codes
    (naming_format, confidence_miscalibrated, duplicates_existing, weak_evidence,
    ontology_incoherent, out_of_scope); overall_verdict ∈ {accept, revise, reject};
    tool name ontogen_review.
  spec/feature/BACKEND_LLM.md §RAG anchors — anchors segmented per kind (node/edge/triple);
    Cold start — the Reviewer runs without grounding when the pool is empty.
  spec/feature/BACKEND_LLM.md §Producer revision (turn 2+) — apply suggested_revision,
    drop the item, or keep + rebut; then call ontogen_validate exactly as on turn 0.
  spec/feature/BACKEND.md §Metadata Generation Service (evidence) — untrusted content is
    "capped per the shared untrusted-content size limit".
"""

from src.backend.ontogen.debate_models import RAGAnchor
from src.backend.ontogen.prompts_reviewer import (
    build_producer_revision_prompt,
    build_reviewer_prompt,
)

_IN_SCOPE = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

# Canonical ontogen Reviewer issue taxonomy (BACKEND_LLM.md §Reviewer tool → Issue taxonomy).
_ONTOGEN_ISSUES = (
    "naming_format",
    "confidence_miscalibrated",
    "duplicates_existing",
    "weak_evidence",
    "ontology_incoherent",
    "out_of_scope",
)
# Metagen-only codes must NOT appear in an ontogen prompt — proves the per-service taxonomy.
_METAGEN_ONLY_ISSUES = ("value_too_generic", "value_factually_wrong", "style_inconsistent")


def _candidate() -> dict[str, object]:
    return {
        "nodes": [{"id": "book_title", "name": "Book Title", "confidence_score": 0.8}],
        "edges": [],
        "triples": [],
    }


def _reviewer_prompt_no_anchors() -> str:
    return build_reviewer_prompt(
        _candidate(), rag_anchors={}, in_scope_urns=frozenset([_IN_SCOPE])
    )


def test_reviewer_prompt_lists_full_ontogen_issue_taxonomy() -> None:
    """The Reviewer prompt enumerates exactly the ontogen issue codes, not metagen's.

    Spec: BACKEND_LLM.md §Reviewer tool → Issue taxonomy — ontogen issue codes.
    """
    prompt = _reviewer_prompt_no_anchors()

    for code in _ONTOGEN_ISSUES:
        assert code in prompt, f"ontogen issue code {code!r} must be in the Reviewer prompt"
    # Discriminating backstop: metagen-only codes must be absent (seed both sides).
    for code in _METAGEN_ONLY_ISSUES:
        assert code not in prompt, f"metagen-only code {code!r} must not appear in ontogen prompt"


def test_reviewer_prompt_offers_three_overall_verdicts_and_names_the_tool() -> None:
    """The Reviewer prompt offers accept/revise/reject and directs the ontogen_review tool.

    Spec: BACKEND_LLM.md §Reviewer tool — overall_verdict ∈ {accept, revise, reject};
    tool name ontogen_review.
    """
    prompt = _reviewer_prompt_no_anchors()

    for verdict in ("accept", "revise", "reject"):
        assert f"overall_verdict='{verdict}'" in prompt
    assert "ontogen_review" in prompt
    assert "metagen_review" not in prompt


def test_reviewer_prompt_carries_in_scope_urns() -> None:
    """The in-scope URN set is embedded so the Reviewer can flag out_of_scope items.

    Spec: BACKEND_LLM.md §Reviewer tool → Issue taxonomy — out_of_scope: dataset_urns
    outside the in-scope filter set.
    """
    prompt = _reviewer_prompt_no_anchors()
    assert _IN_SCOPE in prompt


def test_reviewer_prompt_segments_anchors_by_kind() -> None:
    """RAG anchors are rendered in node / edge / triple pools.

    Seeds one anchor per kind and asserts each appears; asserts the cold-start note is
    absent because anchors exist.

    Spec: BACKEND_LLM.md §RAG anchors — anchors segmented per kind (node/edge/triple).
    """
    node_anchor = RAGAnchor(
        kind="node", approved_id="node_book", similarity=0.9, name="APPROVED-NODE-ANCHOR"
    )
    edge_anchor = RAGAnchor(
        kind="edge", approved_id="edge_has", similarity=0.85, label="APPROVED-EDGE-ANCHOR"
    )
    triple_anchor = RAGAnchor(
        kind="triple",
        approved_id="t1",
        similarity=0.8,
        description="APPROVED-TRIPLE-ANCHOR",
    )
    prompt = build_reviewer_prompt(
        _candidate(),
        rag_anchors={"node": [node_anchor], "edge": [edge_anchor], "triple": [triple_anchor]},
        in_scope_urns=frozenset([_IN_SCOPE]),
    )

    assert "--- Nodes ---" in prompt
    assert "APPROVED-NODE-ANCHOR" in prompt
    assert "--- Edges ---" in prompt
    assert "APPROVED-EDGE-ANCHOR" in prompt
    assert "--- Triples ---" in prompt
    assert "APPROVED-TRIPLE-ANCHOR" in prompt
    # Backstop: with anchors present, the cold-start note must not appear.
    assert "no approved items yet" not in prompt


def test_reviewer_prompt_cold_start_when_no_anchors() -> None:
    """With an empty anchor pool the Reviewer runs without grounding (cold-start note).

    Spec: BACKEND_LLM.md §RAG anchors → Cold start — the Reviewer runs without anchor
    grounding when the anchor pool is empty.
    """
    prompt = _reviewer_prompt_no_anchors()
    assert "no approved items yet" in prompt


def test_reviewer_prompt_states_composite_triple_item_id_format() -> None:
    """The Reviewer prompt instructs the composite triple item_id format.

    Spec: BACKEND_LLM.md §Reviewer tool — triple item_id is the composite
    <subject>__<edge>__<object> (matches the persisted triple id format).
    """
    prompt = _reviewer_prompt_no_anchors()
    assert "<subject_node_id>__<edge_id>__<object_node_id>" in prompt


def test_reviewer_prompt_caps_untrusted_anchor_content() -> None:
    """An oversized untrusted anchor description is truncated with the shared cap marker.

    Spec: BACKEND.md §Metadata Generation Service — untrusted content is capped per the
    shared untrusted-content size limit.
    """
    tail = "TAIL-SENTINEL-MUST-BE-DROPPED"
    node_anchor = RAGAnchor(
        kind="node",
        approved_id="node_big",
        similarity=0.9,
        name="big",
        description="x" * 8000 + tail,
    )
    prompt = build_reviewer_prompt(
        _candidate(), rag_anchors={"node": [node_anchor]}, in_scope_urns=frozenset([_IN_SCOPE])
    )

    assert "...[truncated]" in prompt
    assert tail not in prompt


def test_producer_revision_prompt_embeds_verdict_and_offers_three_options() -> None:
    """The revision prompt shows the Reviewer verdict + prior candidate, the three Producer
    options (apply / drop / rebut), and directs the validate tool.

    Spec: BACKEND_LLM.md §Producer revision (turn 2+) — apply suggested_revision, drop the
    item, or keep + attach producer_rebuttal; then call ontogen_validate exactly as on turn 0.
    """
    reviewer_payload = {
        "overall_verdict": "revise",
        "summary": "REVIEWER-SUMMARY-SENTINEL",
        "item_verdicts": [
            {
                "item_kind": "node",
                "item_id": "book_title",
                "verdict": "revise",
                "issues": ["naming_format"],
                "comment": "rename",
            }
        ],
    }
    prompt = build_producer_revision_prompt(
        prior_candidate=_candidate(),
        reviewer_payload=reviewer_payload,
        original_prompt="ORIGINAL-PRODUCER-PROMPT-SENTINEL",
    )

    assert "ORIGINAL-PRODUCER-PROMPT-SENTINEL" in prompt
    assert "REVIEWER-SUMMARY-SENTINEL" in prompt
    assert "suggested_revision" in prompt
    assert "Drop the item" in prompt
    assert "producer_rebuttal" in prompt
    assert "ontogen_validate" in prompt
