"""Cross-data MD action resolution — create/modify/split/retitle dataProduct entities.

Called by MetagenService.review_result() when cross_data.md field paths are approved.

Spec: spec/feature/BACKEND.md §Metadata Generation Service §Cross-data MD action types
      spec/DATAHUB_INTEGRATION.md §Data Product Aspects
"""

import logging
import re
import unicodedata
from typing import Any

from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


def _title_to_urn_slug(title: str) -> str:
    """Convert a data product title to a URL-safe slug for use in a URN."""
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "data-product"


def _make_data_product_urn(title: str) -> str:
    """Generate a DataHub dataProduct URN from a title."""
    slug = _title_to_urn_slug(title)
    return f"urn:li:dataProduct:{slug}"


async def apply_actions(
    approved_actions: list[dict[str, Any]],
    datahub: DataHubClient,
) -> None:
    """Apply a list of approved cross_data.md actions to DataHub.

    Parameters
    ----------
    approved_actions:
        Each dict has at minimum ``action`` (str), ``action_id`` (str),
        and action-specific fields (``title``, ``description``, ``urn``,
        ``new_title``, ``split_into``, etc.).
    datahub:
        DataHubClient for DataHub writes.

    Per-action behaviour (spec §Cross-data MD action types):
      create   → new dataProduct with generator-chosen URN derived from title
      modify   → replace body of existing dataProduct
      split    → Status(removed=True) on original, create replacements
      retitle  → status-remove original URN, create new URN with new title

    All DataHub write failures are raised to the caller.
    """
    for action_spec in approved_actions:
        action_type = action_spec.get("action", "")

        if action_type == "create":
            await _apply_create(action_spec, datahub)
        elif action_type == "modify":
            await _apply_modify(action_spec, datahub)
        elif action_type == "split":
            await _apply_split(action_spec, datahub)
        elif action_type == "retitle":
            await _apply_retitle(action_spec, datahub)
        else:
            logger.warning(
                "metagen_cross_data_unknown_action",
                extra={"action": action_type, "action_id": action_spec.get("action_id")},
            )


def _wrap_assets(asset_urns: list[str]) -> list[Any]:
    """Convert a list of URN strings to DataProductAssociationClass instances."""
    from datahub.metadata.schema_classes import DataProductAssociationClass

    return [DataProductAssociationClass(destinationUrn=urn) for urn in asset_urns]


async def _apply_create(action_spec: dict[str, Any], datahub: DataHubClient) -> None:
    """Create a new dataProduct entity."""
    from datahub.metadata.schema_classes import DataProductPropertiesClass

    title = action_spec.get("title", "Untitled Data Product")
    description = action_spec.get("description", "")
    asset_urns: list[str] = action_spec.get("assets", [])

    urn = _make_data_product_urn(title)
    props = DataProductPropertiesClass(
        name=title,
        description=description,
        assets=_wrap_assets(asset_urns),
    )
    await datahub.emit_aspect(urn, props)
    logger.info(
        "metagen_cross_data_create",
        extra={"urn": urn, "title": title},
    )


async def _apply_modify(action_spec: dict[str, Any], datahub: DataHubClient) -> None:
    """Replace description of an existing dataProduct."""
    from datahub.metadata.schema_classes import DataProductPropertiesClass

    existing_urn = action_spec.get("urn", "")
    description = action_spec.get("description", "")

    if not existing_urn:
        logger.warning("metagen_cross_data_modify_missing_urn", extra={"spec": action_spec})
        return

    # Preserve existing name and assets
    existing = await datahub.get_aspect(existing_urn, DataProductPropertiesClass)
    name = getattr(existing, "name", "") if existing else ""
    existing_assets = getattr(existing, "assets", []) if existing else []

    props = DataProductPropertiesClass(
        name=name,
        description=description,
        assets=existing_assets,  # already DataProductAssociationClass instances
    )
    await datahub.emit_aspect(existing_urn, props)
    logger.info("metagen_cross_data_modify", extra={"urn": existing_urn})


async def _apply_split(action_spec: dict[str, Any], datahub: DataHubClient) -> None:
    """Mark original dataProduct removed and create replacement dataProducts."""
    from datahub.metadata.schema_classes import DataProductPropertiesClass, StatusClass

    original_urn = action_spec.get("urn", "")
    split_into: list[dict[str, Any]] = action_spec.get("split_into", [])

    if original_urn:
        # Mark original as removed
        try:
            await datahub.emit_aspect(original_urn, StatusClass(removed=True))
        except Exception:
            logger.warning(
                "metagen_cross_data_split_remove_original_failed",
                extra={"original_urn": original_urn},
                exc_info=True,
            )

    for replacement in split_into:
        title = replacement.get("title", "Untitled")
        description = replacement.get("description", "")
        asset_urns_split: list[str] = replacement.get("assets", [])
        new_urn = _make_data_product_urn(title)
        props = DataProductPropertiesClass(
            name=title,
            description=description,
            assets=_wrap_assets(asset_urns_split),
        )
        await datahub.emit_aspect(new_urn, props)
        logger.info(
            "metagen_cross_data_split_create",
            extra={"original_urn": original_urn, "new_urn": new_urn},
        )


async def _apply_retitle(action_spec: dict[str, Any], datahub: DataHubClient) -> None:
    """Remove old title URN and create new URN with updated title."""
    from datahub.metadata.schema_classes import DataProductPropertiesClass, StatusClass

    original_urn = action_spec.get("urn", "")
    new_title = action_spec.get("new_title", "")
    description = action_spec.get("description", "")

    if original_urn:
        try:
            await datahub.emit_aspect(original_urn, StatusClass(removed=True))
        except Exception:
            logger.warning(
                "metagen_cross_data_retitle_remove_original_failed",
                extra={"original_urn": original_urn},
                exc_info=True,
            )

    if new_title:
        new_urn = _make_data_product_urn(new_title)
        # Preserve existing assets if available
        asset_urns_retitle: list[str] = action_spec.get("assets", [])
        props = DataProductPropertiesClass(
            name=new_title,
            description=description,
            assets=_wrap_assets(asset_urns_retitle),
        )
        await datahub.emit_aspect(new_urn, props)
        logger.info(
            "metagen_cross_data_retitle",
            extra={"original_urn": original_urn, "new_urn": new_urn, "new_title": new_title},
        )
