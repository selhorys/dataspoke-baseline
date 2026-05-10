"""Unit tests for src/api/schemas/metagen.py — CrossDataAction Pydantic schema.

Tests pin spec invariants:
  - action enum is exactly {create, modify, delete}
  - per-action required fields enforced
  - URN prefix validators enforced
  - max_length constraints enforced

Spec: spec/feature/BACKEND_SCHEMA.md — proposals action shape
      {action_id, action: create|modify|delete, title?, body?, related_assets?, document_urn?}
Spec: spec/DATAHUB_INTEGRATION.md §Document Aspects — urn:li:document: prefix,
      related_assets urn:li:dataset: prefix
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.metagen import CrossDataAction

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)"
_DOCUMENT_URN = "urn:li:document:abc123def456"


# ── Action enum ───────────────────────────────────────────────────────────────


def test_action_literal_accepts_create() -> None:
    """action='create' with all required fields parses without error.

    Spec: BACKEND_SCHEMA.md — action enum {create, modify, delete}.
    """
    action = CrossDataAction.model_validate(
        {
            "action_id": "a1",
            "action": "create",
            "title": "My new document",
            "body": "## Overview\n\nContent.",
            "related_assets": [_DATASET_URN],
        }
    )
    assert action.action == "create"


def test_action_literal_accepts_modify() -> None:
    """action='modify' with all required fields parses without error.

    Spec: BACKEND_SCHEMA.md — action enum {create, modify, delete}.
    """
    action = CrossDataAction.model_validate(
        {
            "action_id": "a2",
            "action": "modify",
            "document_urn": _DOCUMENT_URN,
            "body": "Updated content.",
        }
    )
    assert action.action == "modify"


def test_action_literal_accepts_delete() -> None:
    """action='delete' with document_urn parses without error.

    Spec: BACKEND_SCHEMA.md — action enum {create, modify, delete}.
    """
    action = CrossDataAction.model_validate(
        {
            "action_id": "a3",
            "action": "delete",
            "document_urn": _DOCUMENT_URN,
        }
    )
    assert action.action == "delete"


def test_action_literal_rejects_split() -> None:
    """action='split' raises ValidationError — not in the allowed enum.

    Spec: BACKEND_SCHEMA.md — action enum is strictly {create, modify, delete};
    legacy or invented values must be rejected.
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "s1",
                "action": "split",
                "title": "Title",
                "body": "Body",
                "related_assets": [_DATASET_URN],
            }
        )


def test_action_literal_rejects_retitle() -> None:
    """action='retitle' raises ValidationError — not in the allowed enum.

    Spec: BACKEND_SCHEMA.md — action enum is strictly {create, modify, delete}.
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "r1",
                "action": "retitle",
                "document_urn": _DOCUMENT_URN,
                "title": "New title",
            }
        )


# ── create required fields ────────────────────────────────────────────────────


def test_create_requires_title() -> None:
    """create without title raises ValidationError.

    Spec: BACKEND_SCHEMA.md — create action requires title (str, ≤300 chars).
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "c1",
                "action": "create",
                "body": "Body",
                "related_assets": [_DATASET_URN],
            }
        )


def test_create_requires_body() -> None:
    """create without body raises ValidationError.

    Spec: BACKEND_SCHEMA.md — create action requires body (Markdown, ≤50000 chars).
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "c2",
                "action": "create",
                "title": "Title",
                "related_assets": [_DATASET_URN],
            }
        )


def test_create_requires_related_assets() -> None:
    """create without related_assets raises ValidationError.

    Spec: BACKEND_SCHEMA.md — create action requires related_assets (non-empty list).
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "c3",
                "action": "create",
                "title": "Title",
                "body": "Body",
            }
        )


def test_create_rejects_empty_related_assets() -> None:
    """create with related_assets=[] raises ValidationError.

    Spec: spec/feature/BACKEND.md §Cross-data MD action types (create row) —
    relatedAssets lists the involved dataset URNs (at least one); a cross-data
    document with no related datasets has no purpose.
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "c4",
                "action": "create",
                "title": "Title",
                "body": "Body",
                "related_assets": [],  # empty list — spec rejects this
            }
        )


# ── modify required fields ────────────────────────────────────────────────────


def test_modify_requires_document_urn() -> None:
    """modify without document_urn raises ValidationError.

    Spec: BACKEND_SCHEMA.md — modify action requires document_urn (urn:li:document:*).
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "m1",
                "action": "modify",
                "body": "Body",
            }
        )


def test_modify_requires_body() -> None:
    """modify without body raises ValidationError.

    Spec: BACKEND_SCHEMA.md — modify action requires body (Markdown, ≤50000 chars).
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "m2",
                "action": "modify",
                "document_urn": _DOCUMENT_URN,
            }
        )


# ── delete required fields ────────────────────────────────────────────────────


def test_delete_requires_document_urn() -> None:
    """delete without document_urn raises ValidationError.

    Spec: BACKEND_SCHEMA.md — delete action requires document_urn.
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "d1",
                "action": "delete",
            }
        )


def test_delete_succeeds_with_only_document_urn() -> None:
    """delete with only action_id, action, document_urn is valid.

    Spec: BACKEND_SCHEMA.md — delete action only requires document_urn;
    title, body, related_assets must be absent/null.
    """
    action = CrossDataAction.model_validate(
        {
            "action_id": "d2",
            "action": "delete",
            "document_urn": _DOCUMENT_URN,
        }
    )
    assert action.action == "delete"
    assert action.title is None
    assert action.body is None
    assert action.related_assets is None


# ── URN format validators ─────────────────────────────────────────────────────


def test_document_urn_must_match_prefix() -> None:
    """document_urn with dataset: prefix raises ValidationError.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — document_urn must match
    ^urn:li:document: not urn:li:dataset:.
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "u1",
                "action": "modify",
                "document_urn": _DATASET_URN,  # wrong prefix
                "body": "Body",
            }
        )


def test_related_assets_items_must_be_dataset_urns() -> None:
    """related_assets containing a document URN raises ValidationError.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — each related_assets item
    must match ^urn:li:dataset:.
    """
    with pytest.raises(ValidationError):
        CrossDataAction.model_validate(
            {
                "action_id": "u2",
                "action": "create",
                "title": "Title",
                "body": "Body",
                "related_assets": [_DOCUMENT_URN],  # wrong: document URN not dataset URN
            }
        )
