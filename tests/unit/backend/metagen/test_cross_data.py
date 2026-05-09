"""Unit tests for src/backend/metagen/cross_data.py.

Spec: spec/feature/BACKEND.md §Metadata Generation Service §Cross-data MD action types
      spec/DATAHUB_INTEGRATION.md §Document Aspects
      spec/BACKEND_SCHEMA.md — proposals action shape {action: create|modify|delete}
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metagen.cross_data import apply_actions

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)"
_DOCUMENT_URN = "urn:li:document:abc123def456"


def _make_native_document_info(title: str = "Existing Title") -> MagicMock:
    """Minimal DocumentInfoClass mock with NATIVE sourceType."""
    source = MagicMock()
    source.sourceType = "NATIVE"
    info = MagicMock()
    info.title = title
    info.source = source
    info.created = MagicMock()
    info.relatedAssets = []
    return info


def _make_external_document_info(title: str = "External Title") -> MagicMock:
    """Minimal DocumentInfoClass mock with EXTERNAL sourceType."""
    source = MagicMock()
    source.sourceType = "EXTERNAL"
    info = MagicMock()
    info.title = title
    info.source = source
    return info


# ── create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_create_emits_documentInfo() -> None:
    """create action emits a DocumentInfoClass aspect with matching title, body,
    relatedAssets, NATIVE sourceType, and populated audit stamps.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — NATIVE source, title, body,
    relatedAssets, created, lastModified populated.
    """
    datahub = AsyncMock()
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "a1",
            "action": "create",
            "title": "How orders reference books",
            "body": "## Markdown\n\nContent here.",
            "related_assets": [_DATASET_URN],
        }
    ]
    await apply_actions(actions, datahub=datahub)

    assert datahub.emit_aspect.call_count == 1
    _emitted_urn, aspect = datahub.emit_aspect.call_args[0]

    # Title and body
    assert aspect.title == "How orders reference books"
    assert aspect.contents.text == "## Markdown\n\nContent here."

    # relatedAssets length
    assert len(aspect.relatedAssets) == 1

    # NATIVE source
    assert aspect.source.sourceType == "NATIVE"

    # Audit stamps populated (not None, not zero)
    assert aspect.created is not None
    assert aspect.lastModified is not None
    assert aspect.created.time > 0
    assert aspect.lastModified.time > 0

    # Spec: AuditStamp requires actor to be populated (both created and lastModified)
    assert aspect.created.actor, (
        "AuditStamp.created.actor must be non-empty (spec/DATAHUB_INTEGRATION.md §Document Aspects)"
    )
    assert aspect.lastModified.actor, (
        "AuditStamp.lastModified.actor must be non-empty (spec/DATAHUB_INTEGRATION.md §Document Aspects)"
    )


@pytest.mark.asyncio
async def test_create_returns_new_urn_in_outcome() -> None:
    """create outcome carries a 'urn' with the urn:li:document: prefix.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — each document entity is
    identified by a urn:li:document:<id> URN.
    """
    datahub = AsyncMock()
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "a2",
            "action": "create",
            "title": "T",
            "body": "B",
            "related_assets": [_DATASET_URN],
        }
    ]
    outcomes = await apply_actions(actions, datahub=datahub)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert "urn" in outcome
    _DOCUMENT_PREFIX = "urn:li:document:"
    assert outcome["urn"].startswith(_DOCUMENT_PREFIX) and len(outcome["urn"]) > len(
        _DOCUMENT_PREFIX
    ), f"Expected non-empty urn:li:document: URN, got {outcome['urn']!r}"


# ── modify ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_modify_keeps_urn_and_overwrites_body() -> None:
    """modify action emits to the existing document URN with new body; title is preserved.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — modify replaces body,
    preserves URN, title, source, and created audit stamp.
    """
    existing = _make_native_document_info(title="Original Title")
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=existing)
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "m1",
            "action": "modify",
            "document_urn": _DOCUMENT_URN,
            "body": "new body content",
        }
    ]
    await apply_actions(actions, datahub=datahub)

    assert datahub.emit_aspect.call_count == 1
    emitted_urn, aspect = datahub.emit_aspect.call_args[0]

    # Correct entity URN preserved
    assert emitted_urn == _DOCUMENT_URN

    # Body replaced
    assert aspect.contents.text == "new body content"

    # Title preserved from existing
    assert aspect.title == "Original Title"


@pytest.mark.asyncio
async def test_modify_refuses_non_native_document() -> None:
    """modify refuses a document with sourceType != 'NATIVE'; outcome has success=False.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke only modifies
    NATIVE documents; non-NATIVE documents are managed by external systems.
    """
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=_make_external_document_info())
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "m2",
            "action": "modify",
            "document_urn": _DOCUMENT_URN,
            "body": "attempted modification",
        }
    ]
    outcomes = await apply_actions(actions, datahub=datahub)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["success"] is False
    # Error must mention non-NATIVE (case-insensitive substring match)
    error_lower = outcome.get("error", "").lower()
    assert "native" in error_lower, f"Expected 'native' in error, got: {outcome.get('error')!r}"

    # emit_aspect must not have been called
    datahub.emit_aspect.assert_not_called()


@pytest.mark.asyncio
async def test_modify_refuses_missing_document() -> None:
    """modify refuses when get_aspect returns None (document does not exist).

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke must verify
    existence before modifying a document entity.
    """
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "m3",
            "action": "modify",
            "document_urn": _DOCUMENT_URN,
            "body": "will not be written",
        }
    ]
    outcomes = await apply_actions(actions, datahub=datahub)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["success"] is False
    # Error must mention missing / not-found (not a generic refusal message)
    error_lower = outcome.get("error", "").lower()
    assert any(kw in error_lower for kw in ("missing", "not found")), (
        f"Expected missing/not-found error, got: {outcome.get('error')!r}"
    )
    datahub.emit_aspect.assert_not_called()


# ── delete ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_delete_emits_status_removed() -> None:
    """delete action emits StatusClass(removed=True) to the existing document URN.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — soft-delete via
    Status.removed=true; entity URN is preserved.
    """
    from datahub.metadata.schema_classes import StatusClass

    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=_make_native_document_info())
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "d1",
            "action": "delete",
            "document_urn": _DOCUMENT_URN,
        }
    ]
    await apply_actions(actions, datahub=datahub)

    assert datahub.emit_aspect.call_count == 1
    emitted_urn, aspect = datahub.emit_aspect.call_args[0]

    # Correct entity URN
    assert emitted_urn == _DOCUMENT_URN

    # Aspect is StatusClass with removed=True
    assert isinstance(aspect, StatusClass)
    assert aspect.removed is True


@pytest.mark.asyncio
async def test_delete_refuses_non_native_document() -> None:
    """delete refuses a document with sourceType != 'NATIVE'; outcome has success=False.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke only soft-deletes
    NATIVE documents.
    """
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=_make_external_document_info())
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "d2",
            "action": "delete",
            "document_urn": _DOCUMENT_URN,
        }
    ]
    outcomes = await apply_actions(actions, datahub=datahub)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["success"] is False
    error_lower = outcome.get("error", "").lower()
    assert "native" in error_lower, f"Expected 'native' in error, got: {outcome.get('error')!r}"
    datahub.emit_aspect.assert_not_called()


@pytest.mark.asyncio
async def test_delete_refuses_missing_document() -> None:
    """delete refuses when get_aspect returns None (document does not exist).

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke must verify
    existence before soft-deleting a document entity.
    """
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.emit_aspect = AsyncMock()

    actions = [
        {
            "action_id": "d3",
            "action": "delete",
            "document_urn": _DOCUMENT_URN,
        }
    ]
    outcomes = await apply_actions(actions, datahub=datahub)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["success"] is False
    error_lower = outcome.get("error", "").lower()
    assert any(kw in error_lower for kw in ("missing", "not found")), (
        f"Expected missing/not-found error, got: {outcome.get('error')!r}"
    )
    datahub.emit_aspect.assert_not_called()


# ── batch / error-isolation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_actions_continues_after_one_failure() -> None:
    """apply_actions processes all actions even when one fails validation.

    Spec: BACKEND_SCHEMA.md — each action in proposals.cross_data.md is
    independently applied; a per-action failure must not abort the batch.
    """
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=_make_native_document_info())
    datahub.emit_aspect = AsyncMock()

    actions = [
        # valid create
        {
            "action_id": "ok-create",
            "action": "create",
            "title": "Valid Document",
            "body": "Valid body",
            "related_assets": [_DATASET_URN],
        },
        # invalid modify — missing document_urn (Pydantic will reject)
        {
            "action_id": "bad-modify",
            "action": "modify",
            "body": "Body but no document_urn",
        },
        # valid delete
        {
            "action_id": "ok-delete",
            "action": "delete",
            "document_urn": _DOCUMENT_URN,
        },
    ]
    outcomes = await apply_actions(actions, datahub=datahub)

    # All three produce an outcome entry
    assert len(outcomes) == 3

    outcome_ids = [o["action_id"] for o in outcomes]
    assert "ok-create" in outcome_ids
    assert "bad-modify" in outcome_ids
    assert "ok-delete" in outcome_ids

    # The invalid action is marked as failed
    bad = next(o for o in outcomes if o["action_id"] == "bad-modify")
    assert bad["success"] is False

    # The valid create emitted an aspect; delete emitted another
    # Total emit_aspect calls: 1 (create) + 1 (delete) = 2
    assert datahub.emit_aspect.call_count == 2
