"""Shared mapper helpers for metagen routers.

Used by both src/api/routers/spoke/metagen.py (cross-dataset routes)
and src/api/routers/spoke/common/data/metagen.py (per-dataset routes).

DB CHECK constraints ck_metagen_items_kind and ck_metagen_candidates_status
(migrations/versions/001_initial_schema.py) enforce valid enum values at the
storage layer, so cast() is safe here.
"""

from typing import Any, Literal, cast

from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.metagen import (
    MetagenCandidate,
    MetagenDatasetSummary,
    MetagenItemDetailResponse,
    MetagenItemSummary,
)

_ItemStatus = Literal["pending", "llm_approved", "approved"]
_CandStatus = Literal["llm_approved", "approved", "rejected"]
_KindLiteral = Literal["dataset.description", "column.description"]


def item_status(has_approved: bool, non_rejected_count: int) -> _ItemStatus:
    """Derive item status over NON-rejected candidates (BACKEND.md §Item status).

    `non_rejected_count` is the number of candidates whose status is not
    `rejected`; an item whose only candidates are rejected is `pending`.
    """
    if has_approved:
        return "approved"
    if non_rejected_count > 0:
        return "llm_approved"
    return "pending"


def to_candidate(c: Any) -> MetagenCandidate:
    return MetagenCandidate(
        candidate_id=c.candidate_id,
        conf_id=c.conf_id,
        conf_name=c.conf_name,
        item_id=c.item_id,
        dataset_urn=c.dataset_urn,
        run_id=c.run_id,
        value=c.value,
        confidence_score=c.confidence_score,
        status=cast(_CandStatus, c.status),
        evidence=c.evidence,
        created_at=c.created_at,
        reviewed_at=c.reviewed_at,
        reviewer_id=c.reviewer_id,
    )


def to_item_summary(dto: Any) -> MetagenItemSummary:
    return MetagenItemSummary(
        dataset_urn=dto.dataset_urn,
        item_id=dto.item_id,
        kind=cast(_KindLiteral, dto.kind),
        field_path=dto.field_path,
        status=item_status(dto.has_approved, dto.non_rejected_count),
        candidate_count=dto.candidate_count,
        created_at=dto.created_at,
        composite_id=f"{dto.dataset_urn}::{dto.item_id}",
    )


def to_item_detail(dto: Any) -> MetagenItemDetailResponse:
    cands = [to_candidate(c) for c in dto.candidates]
    has_approved = any(c.status == "approved" for c in cands)
    non_rejected_count = sum(1 for c in cands if c.status != "rejected")
    return MetagenItemDetailResponse(
        dataset_urn=dto.dataset_urn,
        item_id=dto.item_id,
        kind=cast(_KindLiteral, dto.kind),
        field_path=dto.field_path,
        status=item_status(has_approved, non_rejected_count),
        candidate_count=len(cands),
        created_at=dto.created_at,
        composite_id=f"{dto.dataset_urn}::{dto.item_id}",
        candidates=cands,
    )


def to_dataset_summary(dto: Any) -> MetagenDatasetSummary:
    return MetagenDatasetSummary(
        dataset_urn=dto.dataset_urn,
        is_enabled=dto.is_enabled,
        allowed=[cast(_KindLiteral, k) for k in dto.allowed],
        item_count=dto.item_count,
        approved_count=dto.approved_count,
        rejected_count=dto.rejected_count,
        candidate_count=dto.candidate_count,
        last_modified_at=dto.last_modified_at,
    )


def event_list(
    events: list[dict[str, Any]], total_count: int, offset: int, limit: int
) -> EventListResponse:
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=str(e["id"]),
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e.get("detail", {}),
                occurred_at=e["occurred_at"],
            )
            for e in events
        ],
    )
