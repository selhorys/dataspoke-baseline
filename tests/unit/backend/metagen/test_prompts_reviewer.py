"""Unit tests for the metagen adversarial Reviewer / Producer-revision prompt builders.

These assert the Reviewer prompt encodes the spec'd debate *contract* — the canonical
metagen issue taxonomy, the three overall verdicts, the tool the Reviewer must call, the
in-scope URN set, RAG anchors segmented by kind, cold-start behaviour when no anchors
exist, and the shared untrusted-content size cap — rather than incidental string layout.

Spec:
  spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate — metagen issue taxonomy
    (value_too_generic, value_factually_wrong, value_redundant_with_approved,
    confidence_miscalibrated, style_inconsistent, out_of_scope); RAG anchors are
    "approved candidate values grouped by kind (dataset descriptions in one pool,
    column descriptions in another)".
  spec/feature/BACKEND_LLM.md §Reviewer tool — overall_verdict ∈ {accept, revise, reject}.
  spec/feature/BACKEND_LLM.md §RAG anchors → Cold start — "When the anchor pool is empty
    ... the Reviewer simply runs without anchor grounding."
  spec/feature/BACKEND_LLM.md §Producer revision (turn 2+) — Producer must apply the
    suggested_revision, drop the item, or keep + rebut, then call the validate tool.
  spec/feature/BACKEND.md §Metadata Generation Service (evidence) — untrusted content is
    "capped per the shared untrusted-content size limit".
"""

from src.backend.metagen.debate_models import MetagenRAGAnchor
from src.backend.metagen.prompts_reviewer import (
    build_producer_revision_prompt,
    build_reviewer_prompt,
)

_IN_SCOPE = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

# Canonical metagen Reviewer issue taxonomy (BACKEND_LLM.md §Metagen Adversarial Debate).
_METAGEN_ISSUES = (
    "value_too_generic",
    "value_factually_wrong",
    "value_redundant_with_approved",
    "confidence_miscalibrated",
    "style_inconsistent",
    "out_of_scope",
)
# Ontogen-only codes must NOT appear in a metagen prompt — proves the per-service taxonomy.
_ONTOGEN_ONLY_ISSUES = ("naming_format", "duplicates_existing", "ontology_incoherent")


def _reviewer_prompt_no_anchors() -> str:
    return build_reviewer_prompt(
        _candidate(), rag_anchors={}, in_scope_urns=frozenset([_IN_SCOPE])
    )


def _candidate() -> dict[str, object]:
    return {
        "candidates": [
            {
                "dataset_urn": _IN_SCOPE,
                "item_id": "dataset.description",
                "value": "Master catalogue of Imazon book titles keyed by ISBN.",
                "confidence_score": 0.82,
            }
        ]
    }


def test_reviewer_prompt_lists_full_metagen_issue_taxonomy() -> None:
    """The Reviewer prompt enumerates exactly the metagen issue codes, not ontogen's.

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — metagen issue taxonomy.
    """
    prompt = _reviewer_prompt_no_anchors()

    for code in _METAGEN_ISSUES:
        assert code in prompt, f"metagen issue code {code!r} must be in the Reviewer prompt"
    # Discriminating backstop: ontogen-only codes must be absent (seed both sides).
    for code in _ONTOGEN_ONLY_ISSUES:
        assert code not in prompt, f"ontogen-only code {code!r} must not leak into a metagen prompt"


def test_reviewer_prompt_offers_three_overall_verdicts_and_names_the_tool() -> None:
    """The Reviewer prompt offers accept/revise/reject and directs the metagen_review tool.

    Spec: BACKEND_LLM.md §Reviewer tool — overall_verdict ∈ {accept, revise, reject};
    §Metagen Adversarial Debate — the metagen Reviewer tool is metagen_review.
    """
    prompt = _reviewer_prompt_no_anchors()

    for verdict in ("accept", "revise", "reject"):
        assert f"overall_verdict='{verdict}'" in prompt
    assert "metagen_review" in prompt
    # It must not instruct calling the ontogen tool.
    assert "ontogen_review" not in prompt


def test_reviewer_prompt_carries_in_scope_urns() -> None:
    """The in-scope URN set is embedded so the Reviewer can flag out_of_scope items.

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — out_of_scope: dataset_urn not in
    the in-scope filter set (the Reviewer needs the set to apply the rule).
    """
    prompt = _reviewer_prompt_no_anchors()
    assert _IN_SCOPE in prompt


def test_reviewer_prompt_segments_anchors_by_kind() -> None:
    """RAG anchors are rendered in two pools — dataset-description and column-description.

    Seeds both an in-pool dataset anchor and a column anchor and asserts each value
    appears under its pool; asserts the cold-start note is absent because anchors exist.

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — approved candidate values grouped
    by kind (dataset descriptions in one pool, column descriptions in another).
    """
    dataset_anchor = MetagenRAGAnchor(
        kind="dataset.description",
        dataset_urn=_IN_SCOPE,
        item_id="dataset.description",
        value="APPROVED-DATASET-ANCHOR-VALUE",
        similarity=0.91,
    )
    column_anchor = MetagenRAGAnchor(
        kind="column.description",
        dataset_urn=_IN_SCOPE,
        item_id="isbn",
        value="APPROVED-COLUMN-ANCHOR-VALUE",
        similarity=0.88,
    )
    prompt = build_reviewer_prompt(
        _candidate(),
        rag_anchors={
            "dataset.description": [dataset_anchor],
            "column.description": [column_anchor],
        },
        in_scope_urns=frozenset([_IN_SCOPE]),
    )

    assert "Dataset description anchors" in prompt
    assert "APPROVED-DATASET-ANCHOR-VALUE" in prompt
    assert "Column description anchors" in prompt
    assert "APPROVED-COLUMN-ANCHOR-VALUE" in prompt
    # Backstop: with anchors present, the cold-start note must not appear.
    assert "No approved anchors" not in prompt


def test_reviewer_prompt_cold_start_when_no_anchors() -> None:
    """With an empty anchor pool the Reviewer runs without grounding (cold-start note).

    Spec: BACKEND_LLM.md §RAG anchors → Cold start — the Reviewer simply runs without
    anchor grounding when the anchor pool is empty.
    """
    prompt = _reviewer_prompt_no_anchors()
    assert "No approved anchors" in prompt


def test_reviewer_prompt_caps_untrusted_candidate_content() -> None:
    """An oversized untrusted candidate value is truncated with the shared cap marker.

    Seeds a candidate value far larger than the untrusted-content byte cap and asserts the
    prompt carries the truncation marker and drops the tail (so a compromised candidate
    cannot bloat the prompt unbounded).

    Spec: BACKEND.md §Metadata Generation Service — untrusted content is capped per the
    shared untrusted-content size limit.
    """
    tail = "TAIL-SENTINEL-MUST-BE-DROPPED"
    oversized = {
        "candidates": [
            {
                "dataset_urn": _IN_SCOPE,
                "item_id": "dataset.description",
                "value": "x" * 8000 + tail,
                "confidence_score": 0.5,
            }
        ]
    }
    prompt = build_reviewer_prompt(oversized, rag_anchors={}, in_scope_urns=frozenset([_IN_SCOPE]))

    assert "...[truncated]" in prompt
    assert tail not in prompt


def test_producer_revision_prompt_embeds_verdict_and_offers_three_options() -> None:
    """The revision prompt shows the prior candidate + Reviewer verdict and the three
    Producer options, then directs the validate tool.

    Spec: BACKEND_LLM.md §Producer revision (turn 2+) — apply suggested_revision, drop the
    item, or keep + rebut; then call the validate tool exactly as on the first turn.
    """
    prior = _candidate()
    reviewer_payload = {
        "overall_verdict": "revise",
        "summary": "REVIEWER-SUMMARY-SENTINEL",
        "item_verdicts": [
            {
                "item_kind": "dataset_description",
                "dataset_urn": _IN_SCOPE,
                "item_id": "dataset.description",
                "verdict": "revise",
                "issues": ["value_too_generic"],
                "comment": "too vague",
            }
        ],
    }
    prompt = build_producer_revision_prompt(
        prior_candidate=prior,
        reviewer_payload=reviewer_payload,
        original_prompt="ORIGINAL-PRODUCER-PROMPT-SENTINEL",
    )

    # The original producer prompt is preserved (the Producer re-emits against it).
    assert "ORIGINAL-PRODUCER-PROMPT-SENTINEL" in prompt
    # The Reviewer verdict is fed back to the Producer.
    assert "REVIEWER-SUMMARY-SENTINEL" in prompt
    # The three per-verdict producer actions are stated (the spec'd options, not the
    # bare verdict labels): apply the suggested_revision, rewrite-or-drop the item, or
    # keep as-is. spec: BACKEND_LLM.md §Producer revision (turn 2+).
    assert "suggested_revision" in prompt
    assert "drop the item" in prompt.lower()
    assert "keep as-is" in prompt.lower()
    # The Producer must re-validate its revised candidates.
    assert "metagen_validate" in prompt
