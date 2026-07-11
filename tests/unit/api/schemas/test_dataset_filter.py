"""Unit tests for src/api/schemas/_dataset_filter.py — shared dataset_filter validators.

Spec traceability:
- spec/API.md §Governance — Metric (Payload caps) — dataset_filter list dimensions capped at 1,000
- spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs
- spec/API.md §Governance — Metric (Definition body) — origin: str | None;
  forwarded verbatim to DataHub
"""

import pytest

from src.api.schemas._dataset_filter import (
    DATASET_FILTER_LIST_CAP,
    check_dataset_filter_bounds,
    check_dataset_urn_format,
    check_origin,
    validate_dataset_filter,
)
from src.shared.exceptions import InvalidDatasetUrnError

# ── DATASET_FILTER_LIST_CAP constant ──────────────────────────────────────────


def test_dataset_filter_list_cap_is_1000() -> None:
    """DATASET_FILTER_LIST_CAP is exactly 1000.

    Spec: spec/API.md §Payload caps — dataset_filter list dimensions capped at 1,000.
    """
    assert DATASET_FILTER_LIST_CAP == 1000


# ── check_dataset_filter_bounds ───────────────────────────────────────────────


class TestCheckDatasetFilterBounds:
    def test_exactly_1000_entries_does_not_raise(self) -> None:
        """dataset_filter.{tags,glossary_terms,dataset_urns} with exactly 1,000 entries is accepted.

        Spec: spec/API.md §Payload caps — 1,000 entries is the inclusive maximum.
        """
        at_cap_urns = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},DEV)" for i in range(1000)
        ]
        at_cap_tags = [f"urn:li:tag:t{i}" for i in range(1000)]
        at_cap_terms = [f"urn:li:glossaryTerm:t{i}" for i in range(1000)]
        check_dataset_filter_bounds({"dataset_urns": at_cap_urns})
        check_dataset_filter_bounds({"tags": at_cap_tags})
        check_dataset_filter_bounds({"glossary_terms": at_cap_terms})

    def test_1001_dataset_urns_raises_value_error(self) -> None:
        """dataset_filter.dataset_urns with 1,001 entries raises ValueError.

        Spec: spec/API.md §Payload caps — 1,001 entries exceeds cap; ValueError propagates to 422.
        """
        over_cap = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},DEV)" for i in range(1001)
        ]
        with pytest.raises(ValueError, match="dataset_urns"):
            check_dataset_filter_bounds({"dataset_urns": over_cap})

    def test_1001_tags_raises_value_error(self) -> None:
        """dataset_filter.tags with 1,001 entries raises ValueError.

        Spec: spec/API.md §Payload caps — tags list capped at 1,000.
        """
        over_cap = [f"urn:li:tag:t{i}" for i in range(1001)]
        with pytest.raises(ValueError, match="tags"):
            check_dataset_filter_bounds({"tags": over_cap})

    def test_1001_glossary_terms_raises_value_error(self) -> None:
        """dataset_filter.glossary_terms with 1,001 entries raises ValueError.

        Spec: spec/API.md §Payload caps — glossary_terms list capped at 1,000.
        """
        over_cap = [f"urn:li:glossaryTerm:t{i}" for i in range(1001)]
        with pytest.raises(ValueError, match="glossary_terms"):
            check_dataset_filter_bounds({"glossary_terms": over_cap})

    def test_missing_dimension_key_does_not_raise(self) -> None:
        """dataset_filter with a missing dimension key is accepted (uses dict.get semantics).

        Spec: spec/API.md §Payload caps — missing key is the same as empty/null; no cap applies.
        """
        check_dataset_filter_bounds({})
        check_dataset_filter_bounds({"origin": "DEV"})

    def test_none_value_for_dimension_does_not_raise(self) -> None:
        """dataset_filter with dimension=None is accepted (null clears the filter).

        Spec: spec/API.md §Payload caps — None value is treated as absent.
        """
        check_dataset_filter_bounds({"tags": None, "glossary_terms": None, "dataset_urns": None})

    def test_origin_is_not_subject_to_list_cap(self) -> None:
        """dataset_filter.origin is a scalar str and is not subject to the list cap.

        Spec: spec/API.md §Payload caps — only list dimensions are capped; origin is a scalar.
        """
        # origin is a str, not a list — check_dataset_filter_bounds skips it entirely
        check_dataset_filter_bounds({"origin": "DEV"})
        check_dataset_filter_bounds({"origin": "PROD"})


# ── check_dataset_urn_format ──────────────────────────────────────────────────


class TestCheckDatasetUrnFormat:
    _WELL_FORMED = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,DEV)"

    def test_well_formed_urn_does_not_raise(self) -> None:
        """A well-formed dataset URN passes without raising.

        Spec: spec/API.md §Error Catalogue — INVALID_DATASET_URN raised for malformed URNs only.
        """
        check_dataset_urn_format({"dataset_urns": [self._WELL_FORMED]})

    def test_not_a_urn_raises_invalid_dataset_urn_error(self) -> None:
        """A plain string that is not a URN raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
        """
        with pytest.raises(InvalidDatasetUrnError):
            check_dataset_urn_format({"dataset_urns": ["not-a-urn"]})

    def test_missing_parens_raises_invalid_dataset_urn_error(self) -> None:
        """A URN missing the parenthesized platform tuple raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — URN must match urn:li:dataset:(...) pattern.
        """
        with pytest.raises(InvalidDatasetUrnError):
            check_dataset_urn_format({"dataset_urns": ["urn:li:dataset:postgres"]})

    def test_empty_string_raises_invalid_dataset_urn_error(self) -> None:
        """An empty string in dataset_urns raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for empty string.
        """
        with pytest.raises(InvalidDatasetUrnError):
            check_dataset_urn_format({"dataset_urns": [""]})


# ── check_origin ──────────────────────────────────────────────────────────────


class TestCheckOrigin:
    def test_none_origin_does_not_raise(self) -> None:
        """origin=None is accepted; null means no origin filter is applied.

        Spec: spec/API.md §Governance — Metric (Definition body) — origin is optional;
              null means all origins.
        """
        check_origin({"origin": None})

    def test_prod_origin_does_not_raise(self) -> None:
        """origin='PROD' is accepted; forwarded verbatim to DataHub.

        Spec: spec/API.md §Governance — Metric (Definition body) — non-empty origin forwarded
              verbatim; DataHub validates enum.
        """
        check_origin({"origin": "PROD"})

    def test_lowercase_origin_accepted_verbatim(self) -> None:
        """origin='dev' (lowercase) is accepted and forwarded verbatim.

        Spec: spec/API.md §Governance — Metric (Definition body) — case is forwarded to
              DataHub, not validated here.
        """
        check_origin({"origin": "dev"})

    def test_empty_string_origin_raises_value_error(self) -> None:
        """origin='' raises ValueError — empty strings are rejected after strip().

        Spec: spec/API.md §Governance — Metric (Definition body) — empty-or-whitespace origin
              rejected at PUT/PATCH with 422 INVALID_PARAMETER.
        """
        with pytest.raises(ValueError, match="origin"):
            check_origin({"origin": ""})

    def test_whitespace_only_origin_raises_value_error(self) -> None:
        """origin='   ' (whitespace only) raises ValueError after trim.

        Spec: spec/API.md §Governance — Metric (Definition body) — empty-or-whitespace origin
              rejected at PUT/PATCH with 422 INVALID_PARAMETER.
        """
        with pytest.raises(ValueError, match="origin"):
            check_origin({"origin": "   "})

    def test_missing_origin_key_does_not_raise(self) -> None:
        """dataset_filter without origin key is accepted (same as origin=None).

        Spec: spec/API.md §Governance — Metric (Definition body) — origin key is optional.
        """
        check_origin({})
        check_origin({"tags": ["urn:li:tag:foo"]})


# ── validate_dataset_filter (composed validator) ──────────────────────────────


class TestValidateDatasetFilter:
    _FULL_SHAPE = {
        "origin": "DEV",
        "tags": ["urn:li:tag:area:catalog"],
        "glossary_terms": ["urn:li:glossaryTerm:entity:Book"],
        "dataset_urns": [
            "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
        ],
    }

    def test_full_shape_accepted(self) -> None:
        """A dataset_filter with all four dimensions populated is accepted.

        Spec: spec/API.md §Governance — Metric (Definition body) — {origin, tags,
              glossary_terms, dataset_urns} is the unified four-dimension shape
              across UC3, UC4, UC5.
        """
        validate_dataset_filter(self._FULL_SHAPE)

    def test_empty_dict_accepted(self) -> None:
        """An empty dataset_filter ({}) is accepted; all dimensions are optional.

        Spec: spec/API.md §Governance — Metric (Definition body) — empty filter
              enumerates all datasets.
        """
        validate_dataset_filter({})

    def test_origin_only_accepted(self) -> None:
        """dataset_filter with only origin is accepted.

        Spec: spec/API.md §Governance — Metric (Definition body) — partial filter
              with origin=DEV is valid.
        """
        validate_dataset_filter({"origin": "DEV"})

    def test_over_cap_tags_raises(self) -> None:
        """dataset_filter.tags over 1,000 raises ValueError via bounds check.

        Spec: spec/API.md §Payload caps — tags list capped at 1,000.
        """
        over_cap_tags = [f"urn:li:tag:t{i}" for i in range(1001)]
        with pytest.raises(ValueError):
            validate_dataset_filter({"tags": over_cap_tags})

    def test_malformed_urn_raises_invalid_dataset_urn_error(self) -> None:
        """Malformed URN in dataset_urns raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
        """
        with pytest.raises(InvalidDatasetUrnError):
            validate_dataset_filter({"dataset_urns": ["not-a-urn"]})

    def test_empty_origin_raises(self) -> None:
        """origin='' raises ValueError after trim check.

        Spec: spec/API.md §Governance — Metric (Definition body) — origin must be
              non-empty if provided.
        """
        with pytest.raises(ValueError):
            validate_dataset_filter({"origin": ""})
