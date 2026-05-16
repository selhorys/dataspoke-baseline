"""Pydantic models for the metagen adversarial debate framework.

Spec: spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MetagenLLMCandidate(BaseModel):
    dataset_urn: str
    item_id: str
    value: str
    confidence_score: float = Field(ge=0.0, le=1.0)


class MetagenLLMOutput(BaseModel):
    candidates: list[MetagenLLMCandidate]


class MetagenReviewItemVerdict(BaseModel):
    item_kind: Literal["dataset_description", "column_description"]
    dataset_urn: str
    item_id: str
    verdict: Literal["accept", "revise", "reject"]
    issues: list[
        Literal[
            "value_too_generic",
            "value_factually_wrong",
            "value_redundant_with_approved",
            "confidence_miscalibrated",
            "style_inconsistent",
            "out_of_scope",
        ]
    ]
    suggested_revision: dict[str, Any] | None = None
    comment: str


class MetagenReviewOutput(BaseModel):
    overall_verdict: Literal["accept", "revise", "reject"]
    item_verdicts: list[MetagenReviewItemVerdict]
    summary: str


class MetagenRAGAnchor(BaseModel):
    kind: Literal["dataset.description", "column.description"]
    dataset_urn: str
    item_id: str
    value: str
    similarity: float


class DebateHistoryEntry(BaseModel):
    turn: int
    actor: Literal["producer", "reviewer"]
    candidate_hash: str | None = None
    verdict: str | None = None
    issues: list[str] | None = None
    comment_summary: str | None = None
    item_verdicts_count: int | None = None


class DebateResult(BaseModel):
    payload: dict[str, Any]
    transcript: dict[str, Any]
    outcome: Literal["accept", "turns_exhausted", "cycle_detected"]
