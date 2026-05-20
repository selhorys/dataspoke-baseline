"""Shared dataset_filter validation helpers for schema-layer validators.

Spec references:
  - spec/API.md §Payload caps
  - spec/feature/BACKEND.md §dataset_filter

All three UC configs (ontogen, metagen, governance metrics) accept the same
four dimensions:
  origin         — DataHub FabricType, AND-ed with the OR-group; optional str
  tags           — list[str], OR-ed; capped at DATASET_FILTER_LIST_CAP
  glossary_terms — list[str], OR-ed; capped at DATASET_FILTER_LIST_CAP
  dataset_urns   — list[str], OR-ed; capped at DATASET_FILTER_LIST_CAP;
                   each entry must match DATASET_URN_RE
"""

import re
from collections.abc import Mapping
from typing import Any

from src.shared.exceptions import InvalidDatasetUrnError

DATASET_FILTER_LIST_CAP: int = 1000
DATASET_URN_RE: re.Pattern[str] = re.compile(r"^urn:li:dataset:\(.+\)$")


def check_dataset_filter_bounds(dataset_filter: Mapping[str, Any]) -> None:
    """Raise ValueError when any list dimension exceeds DATASET_FILTER_LIST_CAP."""
    for key in ("tags", "glossary_terms", "dataset_urns"):
        val = dataset_filter.get(key)
        if val is not None and len(val) > DATASET_FILTER_LIST_CAP:
            raise ValueError(
                f"dataset_filter.{key} may not exceed {DATASET_FILTER_LIST_CAP} entries"
            )


def check_dataset_urn_format(dataset_filter: Mapping[str, Any]) -> None:
    """Raise InvalidDatasetUrnError for malformed URNs in dataset_filter.dataset_urns.

    Pydantic v2 re-raises non-ValueError exceptions, so this propagates to the
    FastAPI handler registered for InvalidDatasetUrnError, yielding a 422 with
    error_code='INVALID_DATASET_URN' (spec/API.md §Error Catalogue).
    """
    for urn in dataset_filter.get("dataset_urns", []) or []:
        if not DATASET_URN_RE.match(str(urn)):
            raise InvalidDatasetUrnError(str(urn))


def check_origin(dataset_filter: Mapping[str, Any]) -> None:
    """Raise ValueError when dataset_filter.origin is present but empty/whitespace."""
    origin = dataset_filter.get("origin")
    if origin is not None and not str(origin).strip():
        raise ValueError("dataset_filter.origin must not be empty")


def validate_dataset_filter(dataset_filter: Mapping[str, Any]) -> None:
    """Compose bounds, URN-format, and origin checks for a dataset_filter dict."""
    check_dataset_filter_bounds(dataset_filter)
    check_dataset_urn_format(dataset_filter)
    check_origin(dataset_filter)
