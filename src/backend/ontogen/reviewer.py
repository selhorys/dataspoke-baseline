"""Reviewer tool builder for the adversarial debate framework.

Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework §Reviewer tool
"""

from typing import Any

from langchain_core.tools import StructuredTool

from src.backend.ontogen.debate_models import ReviewOutput


def build_ontogen_review_tool() -> StructuredTool:
    """Return a LangChain StructuredTool for the Reviewer's structured-output gate.

    This is a structured-output gate only — the LLM's verdict travels through
    the tool-call *arguments* (captured as LoopResult.payload by complete_with_tools),
    not through the tool's return value.  The return value is intentionally minimal.
    """

    async def _ontogen_review(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    return StructuredTool.from_function(
        coroutine=_ontogen_review,
        name="ontogen_review",
        description=(
            "Submit your adversarial review of the Producer's proposed ontology. "
            "Provide an overall_verdict ('accept', 'revise', or 'reject'), "
            "per-item verdicts with issue codes, and a summary. "
            "Call this tool exactly once to submit your review."
        ),
        args_schema=ReviewOutput,
    )
