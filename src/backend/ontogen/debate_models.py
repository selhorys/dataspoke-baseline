"""Pydantic models for the adversarial debate framework.

Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework
"""

from typing import Any, Literal

from pydantic import BaseModel


class ReviewItemVerdict(BaseModel):
    item_kind: Literal["node", "edge", "triple"]
    item_id: str
    verdict: Literal["accept", "revise", "reject"]
    issues: list[
        Literal[
            "naming_format",
            "confidence_miscalibrated",
            "duplicates_existing",
            "weak_evidence",
            "ontology_incoherent",
            "out_of_scope",
        ]
    ]
    suggested_revision: dict[str, Any] | None = None
    comment: str


class ReviewOutput(BaseModel):
    overall_verdict: Literal["accept", "revise", "reject"]
    item_verdicts: list[ReviewItemVerdict]
    summary: str


class RAGAnchor(BaseModel):
    kind: Literal["node", "edge", "triple"]
    approved_id: str
    similarity: float
    # Descriptive fields for prompt rendering — populated based on kind
    name: str | None = None
    label: str | None = None
    description: str | None = None
    semantics: str | None = None


class DebateHistoryEntry(BaseModel):
    turn: int
    actor: Literal["producer", "reviewer"]
    candidate_hash: str | None = None
    verdict: str | None = None
    applied: list[str] | None = None
    rebuttals: list[str] | None = None
    issues: list[str] | None = None
    comment_summary: str | None = None
    item_verdicts_count: int | None = None


class DebateResult(BaseModel):
    payload: dict[str, Any]
    transcript: dict[str, Any]
    outcome: Literal["accept", "turns_exhausted", "cycle_detected"]
