"""Unit tests for src/backend/ontogen/reviewer.py and debate_models.ReviewOutput.

Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework §Reviewer tool
      spec/feature/BACKEND_LLM.md §Issue taxonomy

Groups:
  A – ReviewOutput schema validation (valid, invalid verdict, invalid issue code)
  B – ReviewItemVerdict per-kind validation
  C – build_ontogen_review_tool shape and coroutine behaviour
"""

import pytest
from pydantic import ValidationError

from src.backend.ontogen.debate_models import ReviewItemVerdict, ReviewOutput
from src.backend.ontogen.reviewer import build_ontogen_review_tool

# ─────────────────────────────────────────────────────────────────────────────
# Group A: ReviewOutput schema validation
# ─────────────────────────────────────────────────────────────────────────────


def test_review_output_accepts_valid_payload() -> None:
    """ReviewOutput validates when all required fields are populated with legal values.

    Spec: BACKEND_LLM.md §Reviewer tool — required fields:
    overall_verdict ∈ {accept, revise, reject}, item_verdicts (list), summary (str).
    """
    rv = ReviewOutput(
        overall_verdict="accept",
        item_verdicts=[
            ReviewItemVerdict(
                item_kind="node",
                item_id="book",
                verdict="accept",
                issues=[],
                suggested_revision=None,
                comment="looks good",
            )
        ],
        summary="all items acceptable",
    )
    assert rv.overall_verdict == "accept", (
        "overall_verdict='accept' must round-trip unchanged. "
        "spec: BACKEND_LLM.md §Reviewer tool §overall_verdict"
    )
    assert len(rv.item_verdicts) == 1
    assert rv.summary == "all items acceptable"


def test_review_output_rejects_unknown_verdict() -> None:
    """overall_verdict='maybe' is not in the canonical enum; Pydantic must reject it.

    Spec: BACKEND_LLM.md §Reviewer tool — overall_verdict enum: {accept, revise, reject}.
    No other values are permitted.
    """
    with pytest.raises(ValidationError) as exc_info:
        ReviewOutput(
            overall_verdict="maybe",  # type: ignore[arg-type]
            item_verdicts=[],
            summary="not sure",
        )
    error_str = str(exc_info.value)
    assert "overall_verdict" in error_str or "maybe" in error_str, (
        "ValidationError must reference overall_verdict or the invalid value 'maybe'. "
        "spec: BACKEND_LLM.md §Reviewer tool — overall_verdict Literal"
    )


def test_review_output_rejects_unknown_issue() -> None:
    """ReviewItemVerdict with issues=['nonsense'] must raise ValidationError.

    Spec: BACKEND_LLM.md §Issue taxonomy — canonical codes:
    naming_format, confidence_miscalibrated, duplicates_existing, weak_evidence,
    ontology_incoherent, out_of_scope.
    Any other string is not allowed; adding a new code is a spec change.
    """
    with pytest.raises(ValidationError) as exc_info:
        ReviewItemVerdict(
            item_kind="node",
            item_id="book",
            verdict="revise",
            issues=["nonsense"],  # type: ignore[list-item]
            comment="unknown issue",
        )
    error_str = str(exc_info.value)
    assert "issues" in error_str or "nonsense" in error_str, (
        "ValidationError must reference the issues field or the unknown code 'nonsense'. "
        "spec: BACKEND_LLM.md §Issue taxonomy — only canonical issue codes permitted"
    )


def test_review_output_empty_item_verdicts_allowed() -> None:
    """item_verdicts=[] is valid — matches the stub-accept shape emitted by StubLLMClient.

    Spec: BACKEND_LLM.md §Test Mode — stub Reviewer returns empty item_verdicts on turn 1.
    An empty list must pass model_validate without raising.
    """
    rv = ReviewOutput(
        overall_verdict="accept",
        item_verdicts=[],
        summary="stub-accept",
    )
    assert rv.item_verdicts == [], (
        "item_verdicts=[] must be stored as-is. "
        "spec: BACKEND_LLM.md §Test Mode §stub Reviewer"
    )


def test_review_output_all_three_verdicts_valid() -> None:
    """overall_verdict in {accept, revise, reject} all pass validation.

    Spec: BACKEND_LLM.md §Reviewer tool — overall_verdict enum = {accept, revise, reject}.
    """
    for verdict in ("accept", "revise", "reject"):
        rv = ReviewOutput.model_validate({
            "overall_verdict": verdict,
            "item_verdicts": [],
            "summary": f"test {verdict}",
        })
        assert rv.overall_verdict == verdict, (
            f"overall_verdict={verdict!r} must round-trip unchanged. "
            "spec: BACKEND_LLM.md §Reviewer tool"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group B: ReviewItemVerdict per-kind validation
# ─────────────────────────────────────────────────────────────────────────────


def test_review_item_verdict_per_kind() -> None:
    """item_kind ∈ {node, edge, triple} all pass validation.

    Spec: BACKEND_LLM.md §Reviewer tool — item_kind Literal['node', 'edge', 'triple'].
    """
    for kind in ("node", "edge", "triple"):
        iv = ReviewItemVerdict.model_validate({
            "item_kind": kind,
            "item_id": f"test-{kind}",
            "verdict": "accept",
            "issues": [],
            "comment": "ok",
        })
        assert iv.item_kind == kind, (
            f"item_kind={kind!r} must round-trip unchanged. "
            "spec: BACKEND_LLM.md §Reviewer tool"
        )


def test_review_item_verdict_all_canonical_issues_valid() -> None:
    """Every issue code from the canonical taxonomy is accepted by ReviewItemVerdict.

    Spec: BACKEND_LLM.md §Issue taxonomy — 6 codes:
    naming_format, confidence_miscalibrated, duplicates_existing, weak_evidence,
    ontology_incoherent, out_of_scope.
    """
    canonical_issues = [
        "naming_format",
        "confidence_miscalibrated",
        "duplicates_existing",
        "weak_evidence",
        "ontology_incoherent",
        "out_of_scope",
    ]
    iv = ReviewItemVerdict(
        item_kind="node",
        item_id="book",
        verdict="revise",
        issues=canonical_issues,  # type: ignore[arg-type]
        comment="multiple issues",
    )
    assert len(iv.issues) == 6, (
        f"Expected 6 canonical issues; got {len(iv.issues)}. "
        "spec: BACKEND_LLM.md §Issue taxonomy"
    )


@pytest.mark.parametrize("issue_code", [
    "naming_format",
    "confidence_miscalibrated",
    "duplicates_existing",
    "weak_evidence",
    "ontology_incoherent",
    "out_of_scope",
])
def test_review_item_verdict_each_canonical_issue_alone(issue_code: str) -> None:
    """Each canonical issue code is accepted by ReviewItemVerdict when used in isolation.

    Spec: BACKEND_LLM.md §Issue taxonomy — 6 codes:
    naming_format, confidence_miscalibrated, duplicates_existing, weak_evidence,
    ontology_incoherent, out_of_scope.

    test_review_item_verdict_all_canonical_issues_valid verifies all 6 together; this
    test verifies each code independently so that a regression where a single code
    breaks in isolation (but works in a list) would surface.
    """
    iv = ReviewItemVerdict(
        item_kind="node",
        item_id="book",
        verdict="revise",
        issues=[issue_code],  # type: ignore[list-item]
        comment=f"single issue: {issue_code}",
    )
    assert len(iv.issues) == 1, (
        f"Expected exactly 1 issue; got {len(iv.issues)}. "
        "spec: BACKEND_LLM.md §Issue taxonomy"
    )
    assert str(iv.issues[0]) == issue_code, (
        f"Issue code {issue_code!r} must round-trip unchanged; got {iv.issues[0]!r}. "
        "spec: BACKEND_LLM.md §Issue taxonomy"
    )


def test_review_item_verdict_reject_with_suggested_revision() -> None:
    """suggested_revision dict is accepted as an optional field on ReviewItemVerdict.

    Spec: BACKEND_LLM.md §Reviewer tool — suggested_revision: optional object.
    When present, carries the Producer's correction target.
    """
    iv = ReviewItemVerdict(
        item_kind="node",
        item_id="book",
        verdict="reject",
        issues=["confidence_miscalibrated"],
        suggested_revision={"confidence_score": 0.6},
        comment="score too high",
    )
    assert iv.suggested_revision == {"confidence_score": 0.6}, (
        "suggested_revision dict must be stored as-is. "
        "spec: BACKEND_LLM.md §Reviewer tool"
    )


def test_review_item_verdict_unknown_kind_rejected() -> None:
    """item_kind='dataset' is not in the Literal enum; must raise ValidationError.

    Spec: BACKEND_LLM.md §Reviewer tool — item_kind Literal['node', 'edge', 'triple'] only.
    """
    with pytest.raises(ValidationError):
        ReviewItemVerdict(
            item_kind="dataset",  # type: ignore[arg-type]
            item_id="some-id",
            verdict="accept",
            issues=[],
            comment="wrong kind",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group C: build_ontogen_review_tool shape and coroutine
# ─────────────────────────────────────────────────────────────────────────────


def test_build_ontogen_review_tool_name() -> None:
    """build_ontogen_review_tool() returns a StructuredTool named 'ontogen_review'.

    Spec: BACKEND_LLM.md §Reviewer tool — tool name='ontogen_review'.
    The name is used by complete_with_tools to identify the success tool call.
    """
    tool = build_ontogen_review_tool()
    assert tool.name == "ontogen_review", (
        f"Tool name must be 'ontogen_review'; got {tool.name!r}. "
        "spec: BACKEND_LLM.md §Reviewer tool"
    )


def test_build_ontogen_review_tool_args_schema() -> None:
    """The returned StructuredTool's args_schema is ReviewOutput.

    Spec: BACKEND_LLM.md §Reviewer tool — structured-output schema matches the
    ontogen_review input_schema (overall_verdict, item_verdicts, summary fields).
    ReviewOutput is the Pydantic model that enforces the schema contract.
    """
    tool = build_ontogen_review_tool()
    assert tool.args_schema is ReviewOutput, (
        f"args_schema must be ReviewOutput; got {tool.args_schema!r}. "
        "spec: BACKEND_LLM.md §Reviewer tool"
    )


def test_build_ontogen_review_tool_has_description() -> None:
    """The review tool must have a non-empty description for the LLM prompt.

    Spec: BACKEND_LLM.md §Reviewer tool — structured-output gate with description
    that instructs the model to call this tool exactly once.
    """
    tool = build_ontogen_review_tool()
    assert isinstance(tool.description, str) and len(tool.description) > 0, (
        "build_ontogen_review_tool() must attach a non-empty description. "
        "spec: BACKEND_LLM.md §Reviewer tool"
    )


@pytest.mark.asyncio
async def test_build_ontogen_review_tool_coroutine_returns_ok_true() -> None:
    """The tool coroutine, when invoked with a valid ReviewOutput payload, returns {'ok': True}.

    Spec: BACKEND_LLM.md §Reviewer tool — 'This is a structured-output gate only —
    the LLM's verdict travels through the tool-call arguments (captured as
    LoopResult.payload by complete_with_tools), not through the tool's return value.
    The return value is intentionally minimal.'
    """
    tool = build_ontogen_review_tool()
    result = await tool.ainvoke({
        "overall_verdict": "accept",
        "item_verdicts": [],
        "summary": "all good",
    })
    assert result == {"ok": True}, (
        f"Tool coroutine must return {{'ok': True}}; got {result!r}. "
        "spec: BACKEND_LLM.md §Reviewer tool — minimal return value"
    )
