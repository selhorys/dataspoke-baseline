"""Schema-layer ``dataset_filter`` validation.

A thin wrapper over the grammar in :mod:`src.shared.dataset_filter`, called from
the Pydantic model validators of every request body that writes a filter
(ontogen conf, metagen conf, governance metric).

Neither exception it raises is a ``ValueError``: Pydantic v2 re-raises
non-``ValueError`` exceptions out of a validator unchanged, so each reaches its
own FastAPI handler and keeps its own error code —
``422 INVALID_DATASET_FILTER`` (carrying the character position in ``detail``)
and ``422 INVALID_DATASET_URN`` — rather than collapsing into the generic
``INVALID_PARAMETER`` envelope.

Spec: spec/API.md §``dataset_filter`` grammar, §Error Catalogue.
"""

from src.shared.dataset_filter import (
    MAX_FILTER_CHARS,
    MAX_STRING_LITERALS,
    check_dataset_urn_literals,
    parse_filter,
)

__all__ = ["DATASET_FILTER_FIELD_DESCRIPTION", "validate_dataset_filter"]

#: OpenAPI description shared by every `dataset_filter` field (UC3/UC4/UC5).
DATASET_FILTER_FIELD_DESCRIPTION = (
    "SQL WHERE-clause over the dataset registry; the empty string matches every "
    "registered dataset. Columns: dataset_urn, origin, platform_urn (scalar, "
    "'=' and IN) and tag_urns, glossary_term_urns (array, \"'value' IN column\"). "
    f"AND/OR/IN are case-insensitive, values are case-sensitive. Max "
    f"{MAX_FILTER_CHARS:,} characters and {MAX_STRING_LITERALS:,} string literals."
)


def validate_dataset_filter(dataset_filter: str | None) -> None:
    """Parse *dataset_filter* and check its ``dataset_urn`` literals.

    Raises:
        DatasetFilterSyntaxError: malformed filter, unknown column, or a
            breached payload cap (422 ``INVALID_DATASET_FILTER``).
        InvalidDatasetUrnError: a ``dataset_urn`` literal that is not a
            well-formed ``urn:li:dataset:(…)`` URN (422 ``INVALID_DATASET_URN``).
    """
    check_dataset_urn_literals(parse_filter(dataset_filter))
