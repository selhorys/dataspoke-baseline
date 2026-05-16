"""Reviewer tool builder for the metagen adversarial debate framework.

Spec: spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate
"""

from typing import Any

from langchain_core.tools import StructuredTool

from src.backend.metagen.debate_models import MetagenReviewOutput


def build_metagen_review_tool() -> StructuredTool:
    """Return a LangChain StructuredTool for the metagen Reviewer's structured-output gate.

    The LLM's verdict travels through the tool-call arguments (captured as
    LoopResult.payload by complete_with_tools), not through the tool's return value.
    """

    async def _metagen_review(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    return StructuredTool.from_function(
        coroutine=_metagen_review,
        name="metagen_review",
        description=(
            "Submit your adversarial review of the Producer's proposed metadata candidates. "
            "Provide an overall_verdict ('accept', 'revise', or 'reject'), "
            "per-item verdicts addressed by dataset_urn + item_id, and a summary. "
            "item_kind must be 'dataset_description' or 'column_description'. "
            "Call this tool exactly once to submit your review."
        ),
        args_schema=MetagenReviewOutput,
    )
