"""Shared dataset_filter resolution and validation for backend services.

Spec references:
  - spec/feature/BACKEND.md §dataset_filter
  - spec/DATAHUB_INTEGRATION.md §Dataset Resolution

All three UC services (ontogen, metagen, governance metrics) share the same
four-step resolution pattern: read dims → empty-filter enumerate-all branch →
tag/glossary enumerate branch → explicit-URN probe with optional origin
mismatch check.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.api.schemas._dataset_filter import validate_dataset_filter
from src.shared.exceptions import InvalidDatasetUrnError, PreconditionFailedError

logger = logging.getLogger(__name__)


@dataclass
class ResolvedDatasetScope:
    resolved_urns: list[str] = field(default_factory=list)
    unresolved_urns: list[str] = field(default_factory=list)


async def resolve_dataset_scope(
    datahub: Any,
    dataset_filter: Mapping[str, Any],
    *,
    explicit_urns_override: list[str] | None = None,
    swallow_enumerate_errors: bool = False,
) -> ResolvedDatasetScope:
    """Resolve a dataset_filter dict to a ResolvedDatasetScope.

    Steps:
      1. Read origin, tags, glossary_terms, dataset_urns (use
         explicit_urns_override instead of dataset_urns when provided).
      2. When no tags/glossary_terms/explicit urns: enumerate all datasets,
         optionally filtered by origin.
      3. When tags or glossary_terms present: enumerate with tag/glossary filter
         AND-ed with origin.
      4. For each explicit URN: check origin segment match (if origin set),
         then probe DataHub for dataset existence.

    Returns ResolvedDatasetScope with resolved_urns sorted and deduplicated.
    When swallow_enumerate_errors=True, DataHub enumeration errors are logged
    and return empty rather than propagating (UC3/UC4 semantics). UC5 uses
    swallow_enumerate_errors=False (default) and lets errors propagate.
    """
    dataset_filter = dict(dataset_filter) if dataset_filter else {}

    origin: str | None = dataset_filter.get("origin") or None
    tags: list[str] = dataset_filter.get("tags") or []
    glossary_terms: list[str] = dataset_filter.get("glossary_terms") or []

    if explicit_urns_override is not None:
        explicit_urns: list[str] = explicit_urns_override
    else:
        explicit_urns = dataset_filter.get("dataset_urns") or []

    resolved_urn_set: set[str] = set()
    unresolved_urns: list[str] = []

    if not tags and not glossary_terms and not explicit_urns:
        try:
            all_urns = await datahub.enumerate_datasets(origin=origin)
            resolved_urn_set.update(all_urns)
        except Exception:
            if swallow_enumerate_errors:
                logger.warning("dataset_scope_enumerate_all_failed", exc_info=True)
                return ResolvedDatasetScope()
            raise
    else:
        if tags or glossary_terms:
            try:
                matched = await datahub.enumerate_datasets(
                    tags=tags if tags else None,
                    glossary_terms=glossary_terms if glossary_terms else None,
                    origin=origin,
                )
                resolved_urn_set.update(matched)
            except Exception:
                if swallow_enumerate_errors:
                    logger.warning("dataset_scope_enumerate_filtered_failed", exc_info=True)
                else:
                    raise

        for urn in explicit_urns:
            if origin is not None:
                urn_origin = datahub.origin_from_dataset_urn(urn)
                if urn_origin != origin:
                    logger.debug(
                        "dataset_scope_explicit_urn_origin_mismatch",
                        extra={
                            "urn": urn,
                            "urn_origin": urn_origin,
                            "requested_origin": origin,
                        },
                    )
                    unresolved_urns.append(urn)
                    continue
            try:
                from datahub.metadata.schema_classes import DatasetPropertiesClass

                props = await datahub.get_aspect(urn, DatasetPropertiesClass)
                if props is not None:
                    resolved_urn_set.add(urn)
                else:
                    unresolved_urns.append(urn)
            except Exception:
                logger.warning(
                    "dataset_scope_explicit_urn_check_failed",
                    extra={"urn": urn},
                    exc_info=True,
                )
                unresolved_urns.append(urn)

    return ResolvedDatasetScope(
        resolved_urns=sorted(resolved_urn_set),
        unresolved_urns=unresolved_urns,
    )


def validate_dataset_filter_service(dataset_filter: Mapping[str, Any]) -> None:
    """Validate a dataset_filter at the service layer.

    Converts ValueError from the schema-layer validator to PreconditionFailedError
    with error_code='INVALID_PARAMETER' so it maps to the correct HTTP 422 envelope.
    InvalidDatasetUrnError propagates unchanged (already carries error_code=INVALID_DATASET_URN).
    """
    try:
        validate_dataset_filter(dataset_filter)
    except InvalidDatasetUrnError:
        raise
    except ValueError as exc:
        raise PreconditionFailedError("INVALID_PARAMETER", str(exc)) from exc
