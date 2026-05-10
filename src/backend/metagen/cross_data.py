"""Cross-data MD action resolution — create/modify/delete document entities.

Called by MetagenService.review_result() when cross_data.md field paths are approved.

Spec: spec/feature/BACKEND.md §Metadata Generation Service §Cross-data MD action types
      spec/DATAHUB_INTEGRATION.md §Document Aspects
"""

import logging
import time
from typing import Any
from uuid import uuid4

import datahub.emitter.mce_builder as _mce_builder
from pydantic import ValidationError

from src.api.schemas.metagen import CrossDataAction
from src.shared.datahub.client import DataHubClient
from src.shared.exceptions import DataHubUnavailableError

logger = logging.getLogger(__name__)

# DataSpoke actor URN used in AuditStamp fields
_DATASPOKE_ACTOR_URN = "urn:li:corpuser:dataspoke"

# Maximum number of documents fetched per dataset for evidence / evidence cap.
# Shared with ontogen/evidence.py (which imports this constant).
DOCUMENT_EVIDENCE_CAP_PER_DATASET: int = 10

# URN constraints
_DOCUMENT_URN_PREFIX = "urn:li:document:"
_URN_MAX_LEN = 500

# Use SDK-provided factory when available; fall back to a manual URN otherwise.
_sdk_make_document_urn = getattr(_mce_builder, "make_document_urn", None)


def _new_document_urn() -> str:
    if _sdk_make_document_urn is not None:
        result: str = _sdk_make_document_urn(uuid4().hex)
        return result
    return f"urn:li:document:{uuid4().hex}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_document_urn(urn: str) -> None:
    """Assert that *urn* looks like a valid, non-overlong document URN.

    Raises ValueError for empty, overlong, or wrong-prefix URNs.
    This is the first guard called by modify_document and delete_document.
    """
    if not urn:
        raise ValueError("document_urn must be non-empty")
    if len(urn) > _URN_MAX_LEN:
        raise ValueError(f"document_urn exceeds maximum length of {_URN_MAX_LEN} chars: {urn!r}")
    if not urn.startswith(_DOCUMENT_URN_PREFIX):
        raise ValueError(f"document_urn must start with {_DOCUMENT_URN_PREFIX!r} — got {urn!r}")


async def create_document(
    title: str,
    body_markdown: str,
    related_dataset_urns: list[str],
    *,
    datahub: DataHubClient,
) -> str:
    """Emit a new document entity (NATIVE source). Return the new urn:li:document:<id>."""
    from datahub.metadata.schema_classes import (
        AuditStampClass,
        DocumentContentsClass,
        DocumentInfoClass,
        DocumentSourceClass,
        DocumentStatusClass,
        RelatedAssetClass,
    )

    urn = _new_document_urn()
    now = _now_ms()
    audit = AuditStampClass(time=now, actor=_DATASPOKE_ACTOR_URN)

    info = DocumentInfoClass(
        title=title,
        contents=DocumentContentsClass(text=body_markdown),
        relatedAssets=[RelatedAssetClass(asset=u) for u in related_dataset_urns],
        source=DocumentSourceClass(sourceType="NATIVE"),
        status=DocumentStatusClass(state="PUBLISHED"),
        created=audit,
        lastModified=audit,
    )
    await datahub.emit_aspect(urn, info)
    logger.info("metagen_cross_data_create_document", extra={"urn": urn, "title": title})
    return urn


async def modify_document(
    document_urn: str,
    body_markdown: str,
    related_dataset_urns: list[str] | None,
    *,
    datahub: DataHubClient,
) -> None:
    """Replace documentInfo.contents.text. Preserve URN, title, source, created.

    If related_dataset_urns is not None, replace relatedAssets too.

    Raises ValueError when:
    - document_urn fails format/length validation
    - the target document does not exist in DataHub
    - the document's source.sourceType is not "NATIVE"
    """
    from datahub.metadata.schema_classes import (
        AuditStampClass,
        DocumentContentsClass,
        DocumentInfoClass,
        DocumentStatusClass,
        RelatedAssetClass,
    )

    _validate_document_urn(document_urn)

    existing = await datahub.get_aspect(document_urn, DocumentInfoClass)

    if existing is None:
        raise ValueError(f"Refusing to modify missing document {document_urn!r}")

    source = getattr(existing, "source", None)
    source_type: str = getattr(source, "sourceType", "") if source else ""
    if source_type != "NATIVE":
        raise ValueError(
            f"Refusing to modify non-NATIVE document {document_urn!r} (sourceType={source_type!r})"
        )

    now = _now_ms()
    audit_now = AuditStampClass(time=now, actor=_DATASPOKE_ACTOR_URN)

    # Preserve fields from existing aspect
    title: str | None = getattr(existing, "title", None)
    created = getattr(existing, "created", None) or audit_now

    new_related: list[RelatedAssetClass] | None
    if related_dataset_urns is not None:
        new_related = [RelatedAssetClass(asset=u) for u in related_dataset_urns]
    else:
        existing_related = getattr(existing, "relatedAssets", None)
        new_related = existing_related

    info = DocumentInfoClass(
        title=title,
        contents=DocumentContentsClass(text=body_markdown),
        relatedAssets=new_related,
        source=source,
        status=DocumentStatusClass(state="PUBLISHED"),
        created=created,
        lastModified=audit_now,
    )
    await datahub.emit_aspect(document_urn, info)
    logger.info("metagen_cross_data_modify_document", extra={"urn": document_urn})


async def delete_document(document_urn: str, *, datahub: DataHubClient) -> None:
    """Soft-delete via StatusClass(removed=True).

    Raises ValueError when:
    - document_urn fails format/length validation
    - the target document does not exist in DataHub
    - the document's source.sourceType is not "NATIVE"
    """
    from datahub.metadata.schema_classes import DocumentInfoClass, StatusClass

    _validate_document_urn(document_urn)

    existing = await datahub.get_aspect(document_urn, DocumentInfoClass)

    if existing is None:
        raise ValueError(f"Refusing to delete missing document {document_urn!r}")

    source = getattr(existing, "source", None)
    source_type: str = getattr(source, "sourceType", "") if source else ""
    if source_type != "NATIVE":
        raise ValueError(
            f"Refusing to delete non-NATIVE document {document_urn!r} (sourceType={source_type!r})"
        )

    await datahub.emit_aspect(document_urn, StatusClass(removed=True))
    logger.info("metagen_cross_data_delete_document", extra={"urn": document_urn})


async def fetch_related_documents(
    dataset_urn: str,
    datahub: DataHubClient,
) -> list[dict[str, Any]]:
    """Fetch document entities whose relatedAssets include *dataset_urn*.

    Uses GraphQL ``searchAcrossEntities`` with a ``relatedAssets`` filter.
    Results are sorted by lastModified descending and capped at
    ``DOCUMENT_EVIDENCE_CAP_PER_DATASET``.

    Each entry has: {urn, title, body, related_assets, last_modified}.

    Best-effort — returns [] on failure with a logged WARNING.
    """
    gql = """
    query searchDocumentsByRelatedAsset($input: SearchAcrossEntitiesInput!) {
        searchAcrossEntities(input: $input) {
            searchResults {
                entity {
                    urn
                    ... on Document {
                        info {
                            title
                            contents { text }
                            relatedAssets { asset { urn } }
                            lastModified { time }
                        }
                    }
                }
            }
        }
    }
    """
    variables: dict[str, Any] = {
        "input": {
            "types": ["DOCUMENT"],
            "query": "*",
            "start": 0,
            "count": DOCUMENT_EVIDENCE_CAP_PER_DATASET * 5,
            "orFilters": [{"and": [{"field": "relatedAssets", "values": [dataset_urn]}]}],
        }
    }

    try:
        result = await datahub._with_retry(
            datahub._graph.execute_graphql, gql, variables=variables
        )
        search_results = (result or {}).get("searchAcrossEntities", {}).get("searchResults", [])
        docs: list[dict[str, Any]] = []
        for item in search_results:
            entity = item.get("entity") or {}
            info = entity.get("info") or {}
            contents = info.get("contents") or {}
            related_raw = info.get("relatedAssets") or []
            related_assets = [
                r["asset"]["urn"]
                for r in related_raw
                if r.get("asset") and r["asset"].get("urn")
            ]
            last_modified_ms: int = (info.get("lastModified") or {}).get("time") or 0
            docs.append(
                {
                    "urn": entity.get("urn", ""),
                    "title": info.get("title", ""),
                    "body": contents.get("text", ""),
                    "related_assets": related_assets,
                    "last_modified": last_modified_ms,
                }
            )
        docs.sort(key=lambda d: -(d.get("last_modified") or 0))
        return docs[:DOCUMENT_EVIDENCE_CAP_PER_DATASET]
    except Exception:
        logger.warning(
            "fetch_related_documents_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        return []


async def apply_actions(
    actions: list[dict[str, Any]],
    *,
    datahub: DataHubClient,
) -> list[dict[str, Any]]:
    """Dispatch each action dict to the corresponding document helper.

    Each action dict is validated via CrossDataAction at the top of the
    dispatch loop; a ValidationError produces a per-action failed outcome
    rather than crashing the whole run.

    Returns a list of per-action outcome dicts:
      - create → {action_id, action, urn}
      - modify/delete → {action_id, action, success: bool, error?: str}
    """
    outcomes: list[dict[str, Any]] = []

    for raw in actions:
        # Defense-in-depth: re-validate through typed model before dispatching.
        # action_id may not be present on malformed dicts — extract before validation.
        action_id: str = raw.get("action_id", "") if isinstance(raw, dict) else ""

        try:
            action_spec = CrossDataAction.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "metagen_cross_data_apply_validation_failed",
                extra={"action_id": action_id, "error": str(exc)},
            )
            outcomes.append(
                {
                    "action_id": action_id,
                    "action": raw.get("action", "") if isinstance(raw, dict) else "",
                    "success": False,
                    "error": f"Schema validation failed: {exc}",
                }
            )
            continue

        if action_spec.action == "create":
            try:
                urn = await create_document(
                    title=action_spec.title or "Untitled Document",
                    body_markdown=action_spec.body or "",
                    related_dataset_urns=action_spec.related_assets or [],
                    datahub=datahub,
                )
                outcomes.append(
                    {"action_id": action_spec.action_id, "action": "create", "urn": urn}
                )
            except (DataHubUnavailableError, ValueError) as exc:
                logger.warning(
                    "metagen_cross_data_apply_create_failed",
                    extra={"action_id": action_spec.action_id},
                    exc_info=True,
                )
                outcomes.append(
                    {
                        "action_id": action_spec.action_id,
                        "action": "create",
                        "success": False,
                        "error": str(exc),
                    }
                )

        elif action_spec.action == "modify":
            try:
                await modify_document(
                    document_urn=action_spec.document_urn or "",
                    body_markdown=action_spec.body or "",
                    related_dataset_urns=action_spec.related_assets,
                    datahub=datahub,
                )
                outcomes.append(
                    {"action_id": action_spec.action_id, "action": "modify", "success": True}
                )
            except (DataHubUnavailableError, ValueError) as exc:
                logger.warning(
                    "metagen_cross_data_apply_modify_failed",
                    extra={
                        "action_id": action_spec.action_id,
                        "urn": action_spec.document_urn,
                    },
                    exc_info=True,
                )
                outcomes.append(
                    {
                        "action_id": action_spec.action_id,
                        "action": "modify",
                        "success": False,
                        "error": str(exc),
                    }
                )

        elif action_spec.action == "delete":
            try:
                await delete_document(
                    document_urn=action_spec.document_urn or "",
                    datahub=datahub,
                )
                outcomes.append(
                    {"action_id": action_spec.action_id, "action": "delete", "success": True}
                )
            except (DataHubUnavailableError, ValueError) as exc:
                logger.warning(
                    "metagen_cross_data_apply_delete_failed",
                    extra={
                        "action_id": action_spec.action_id,
                        "urn": action_spec.document_urn,
                    },
                    exc_info=True,
                )
                outcomes.append(
                    {
                        "action_id": action_spec.action_id,
                        "action": "delete",
                        "success": False,
                        "error": str(exc),
                    }
                )

        else:
            # CrossDataAction.action is a Literal["create","modify","delete"] so this
            # branch is unreachable after model_validate, but kept for safety.
            logger.warning(
                "metagen_cross_data_unknown_action",
                extra={"action": action_spec.action, "action_id": action_spec.action_id},
            )
            outcomes.append(
                {
                    "action_id": action_spec.action_id,
                    "action": action_spec.action,
                    "success": False,
                    "error": f"Unknown action type: {action_spec.action!r}",
                }
            )

    return outcomes
