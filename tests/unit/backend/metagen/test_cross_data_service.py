"""Unit tests for MetagenService cross_data-specific methods.

Covers:
  _find_related_documents — GraphQL filter shape (orFilters, types=DOCUMENT, count cap)
  _propose_cross_data — LLM output validation; invalid actions dropped with WARNING
  _apply_approved_fields — re-validates proposals from JSONB before dispatching

Spec: spec/feature/BACKEND.md §Metadata Generation Service §Cross-data MD action types
      spec/DATAHUB_INTEGRATION.md §Document Aspects — orFilters, types=DOCUMENT
      spec/BACKEND_SCHEMA.md — proposals action shape {action: create|modify|delete}
      spec/USE_CASE_en.md §UC4 — approval re-validates at trust boundary
"""

import logging
from unittest.mock import AsyncMock

import pytest

from src.backend.metagen.cross_data import DOCUMENT_EVIDENCE_CAP_PER_DATASET
from src.backend.metagen.service import MetagenService

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)"
_DOCUMENT_URN = "urn:li:document:aabbcc112233"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(datahub: AsyncMock, db: AsyncMock, cache: AsyncMock, llm: AsyncMock) -> MetagenService:
    return MetagenService(datahub=datahub, db=db, llm=llm, cache=cache)


# ── _find_related_documents ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_related_documents_uses_document_type_filter(
    svc: MetagenService,
    datahub: AsyncMock,
) -> None:
    """_find_related_documents sends types=['DOCUMENT'] and orFilters with relatedAssets.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — searchAcrossEntities must
    filter by types=['DOCUMENT'] and use orFilters containing relatedAssets field
    with the dataset URN as value.
    """
    # Return empty results — we are only inspecting the query shape.
    datahub._with_retry = AsyncMock(return_value={"searchAcrossEntities": {"searchResults": []}})

    await svc._find_related_documents(_DATASET_URN)

    assert datahub._with_retry.call_count == 1
    # The variables dict is passed as a keyword arg to _with_retry
    _, call_kwargs = datahub._with_retry.call_args
    variables = call_kwargs.get("variables") or datahub._with_retry.call_args[0][2]

    query_input = variables["input"]

    # Spec: types must include DOCUMENT
    assert "DOCUMENT" in query_input["types"], (
        f"Expected 'DOCUMENT' in types, got {query_input['types']}"
    )

    # Spec: orFilters must contain a filter on relatedAssets with the dataset URN
    or_filters = query_input.get("orFilters", [])
    assert or_filters, "Expected non-empty orFilters"

    all_filter_fields = [f.get("field", "") for clause in or_filters for f in clause.get("and", [])]
    assert any("relatedAssets" in field for field in all_filter_fields), (
        f"Expected 'relatedAssets' field in orFilters, found: {all_filter_fields}"
    )

    # Values must include the dataset URN
    all_filter_values = [
        v for clause in or_filters for f in clause.get("and", []) for v in f.get("values", [])
    ]
    assert _DATASET_URN in all_filter_values, (
        f"Expected {_DATASET_URN!r} in filter values, found: {all_filter_values}"
    )

    # count must be at least the cap constant so the post-sort cap is meaningful
    assert query_input["count"] >= DOCUMENT_EVIDENCE_CAP_PER_DATASET, (
        f"Expected count >= {DOCUMENT_EVIDENCE_CAP_PER_DATASET}, got {query_input['count']}"
    )


# ── _propose_cross_data ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_cross_data_validates_llm_output(
    svc: MetagenService,
    llm: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_propose_cross_data drops malformed LLM actions and warns; valid ones survive.

    Spec: BACKEND.md §Metadata Generation Service — invalid individual actions
    are dropped with WARNING rather than failing the entire proposal.
    Spec: BACKEND_SCHEMA.md — action shape requires action: create|modify|delete
    with per-action required fields.
    """
    # LLM returns one valid create and one malformed create (missing body)
    llm.complete_json = AsyncMock(
        return_value=[
            {
                "action_id": "good-001",
                "action": "create",
                "title": "Valid document title",
                "body": "## Valid Markdown Body",
                "related_assets": [_DATASET_URN],
            },
            {
                "action_id": "bad-001",
                "action": "create",
                "title": "Missing body",
                # body is absent — Pydantic should reject this
                "related_assets": [_DATASET_URN],
            },
        ]
    )

    evidence = {"related_documents": [], "dataset_name": "catalog.books"}
    with caplog.at_level(logging.WARNING, logger="src.backend.metagen.service"):
        result = await svc._propose_cross_data(_DATASET_URN, evidence)

    # Only the valid action survives
    assert len(result) == 1
    assert result[0]["action_id"] == "good-001"

    # At least one warning must be logged for the malformed action
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "Expected at least one WARNING-level log record for the invalid action"
    )
    # The warning must carry structured extra= data identifying the dropped action
    assert any(
        getattr(r, "action_id", None) == "bad-001" or getattr(r, "index", None) == 1
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), (
        "Expected a WARNING with action_id='bad-001' or index=1 in structured extra= data. "
        f"Records: {[(r.levelno, r.getMessage(), r.__dict__) for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_propose_cross_data_action_enum_is_create_modify_delete(
    svc: MetagenService,
    llm: AsyncMock,
) -> None:
    """_propose_cross_data passes all three valid action types (create, modify, delete).

    Spec: BACKEND_SCHEMA.md — action field is enum {create, modify, delete}.
    """
    llm.complete_json = AsyncMock(
        return_value=[
            {
                "action_id": "c1",
                "action": "create",
                "title": "New doc",
                "body": "Body",
                "related_assets": [_DATASET_URN],
            },
            {
                "action_id": "m1",
                "action": "modify",
                "document_urn": _DOCUMENT_URN,
                "body": "Updated body",
            },
            {
                "action_id": "d1",
                "action": "delete",
                "document_urn": _DOCUMENT_URN,
            },
        ]
    )

    evidence = {"related_documents": [], "dataset_name": "catalog.books"}
    result = await svc._propose_cross_data(_DATASET_URN, evidence)

    assert len(result) == 3
    actions_seen = {r["action"] for r in result}
    assert actions_seen == {"create", "modify", "delete"}


@pytest.mark.asyncio
async def test_propose_cross_data_rejects_split_or_retitle_action(
    svc: MetagenService,
    llm: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_propose_cross_data drops non-enum action values like 'split' and 'retitle'.

    Spec: BACKEND_SCHEMA.md — action enum is strictly {create, modify, delete};
    legacy or invented values must be rejected at the validation boundary.
    """
    llm.complete_json = AsyncMock(
        return_value=[
            {
                "action_id": "split-001",
                "action": "split",
                "title": "Split doc",
                "body": "Body",
                "related_assets": [_DATASET_URN],
            },
            {
                "action_id": "retitle-001",
                "action": "retitle",
                "document_urn": _DOCUMENT_URN,
                "title": "New title",
            },
        ]
    )

    evidence = {"related_documents": [], "dataset_name": "catalog.books"}
    with caplog.at_level(logging.WARNING, logger="src.backend.metagen.service"):
        result = await svc._propose_cross_data(_DATASET_URN, evidence)

    # Both actions must be dropped — result list must be empty
    assert result == [], f"Expected no valid actions, got: {result}"


# ── _apply_approved_fields — cross_data.md re-validation ─────────────────────


@pytest.mark.asyncio
async def test_apply_approved_fields_revalidates_proposals_before_dispatch(
    svc: MetagenService,
    datahub: AsyncMock,
) -> None:
    """_apply_approved_fields drops cross_data proposals with invalid action types.

    This is the trust-boundary invariant: even if the JSONB row in metagen_results
    has been tampered (e.g. action='split'), the re-validation at dispatch must
    reject it and NOT call apply_actions with invalid data.

    Spec: BACKEND.md §Metadata Generation Service — defense-in-depth re-validates
    proposals from mutable JSONB before dispatch.
    Spec: USE_CASE_en.md §UC4 — approval must not propagate invalid actions to DataHub.
    """
    # Proposals JSONB has been tampered: action='split' is not a valid enum value
    tampered_proposals = {
        "cross_data.md": [
            {
                "action_id": "tampered-001",
                "action": "split",  # Invalid — should be rejected at re-validation
                "title": "Tampered doc",
                "body": "Body",
                "related_assets": [_DATASET_URN],
            }
        ]
    }
    approved_fields = ["cross_data.md.tampered-001"]

    datahub.emit_aspect = AsyncMock()

    # _apply_approved_fields must not raise, but must NOT emit the invalid action
    await svc._apply_approved_fields(_DATASET_URN, tampered_proposals, approved_fields)

    # emit_aspect must not have been called because the action failed re-validation
    datahub.emit_aspect.assert_not_called()
