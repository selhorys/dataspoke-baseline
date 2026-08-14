"""Unit tests for src/api/schemas/_dataset_filter.py — the schema-layer wrapper.

The grammar itself is covered by tests/unit/shared/test_dataset_filter.py. What
matters here is the *boundary* behaviour the API contract depends on: which
exception each class of bad filter raises, since that is what decides whether a
request comes back as `422 INVALID_DATASET_FILTER` (with a character position) or
`422 INVALID_DATASET_URN` rather than a generic envelope.

Spec traceability:
- spec/API.md §Error Catalogue — INVALID_DATASET_FILTER (422, `detail` carries
  the character position) and INVALID_DATASET_URN (422)
- spec/API.md §`dataset_filter` grammar — accepted forms, caps
"""

import pytest

from src.api.schemas._dataset_filter import (
    DATASET_FILTER_FIELD_DESCRIPTION,
    validate_dataset_filter,
)
from src.shared.dataset_filter import (
    MAX_FILTER_CHARS,
    MAX_STRING_LITERALS,
    DatasetFilterSyntaxError,
)
from src.shared.exceptions import InvalidDatasetUrnError

_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,PROD)"


class TestAcceptedFilters:
    @pytest.mark.parametrize(
        "dataset_filter",
        [
            "",
            "origin = 'PROD'",
            "origin IN ('PROD', 'DEV')",
            "platform_urn = 'urn:li:dataPlatform:postgres'",
            "'urn:li:tag:area:catalog' IN tag_urns",
            "'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns",
            "is_primary = true",
            "is_primary = FALSE",
            f"dataset_urn = '{_URN}'",
            (
                "origin = 'PROD' AND is_primary = true"
                " AND ('urn:li:tag:area:catalog' IN tag_urns"
                " OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)"
            ),
        ],
    )
    def test_grammar_forms_pass_the_write_boundary(self, dataset_filter: str) -> None:
        """Every production of spec/API.md §`dataset_filter` grammar is writable."""
        validate_dataset_filter(dataset_filter)

    def test_none_is_treated_as_the_empty_filter(self) -> None:
        """An absent filter is the empty filter — 'the empty string matches every
        registered dataset' (spec/API.md §`dataset_filter` grammar)."""
        validate_dataset_filter(None)


class TestSyntaxErrorsCarryTheFilterCode:
    """spec/API.md §Error Catalogue: 'INVALID_DATASET_FILTER | 422 | A
    `dataset_filter` string does not parse under the filter grammar, names an
    unknown column, or exceeds a payload cap. `detail` carries the character
    position of the error.'"""

    @pytest.mark.parametrize(
        "dataset_filter",
        [
            "origin = ",
            "origin = PROD",
            'origin = "PROD"',
            "owner = 'alice'",
            "tag_urns = 'urn:li:tag:pii'",
            "origin = 'A' AND origin = 'B' OR origin = 'C'",
            "origin = 'A' AND (origin = 'B' OR (origin = 'C' AND (origin = 'D' OR origin = 'E')))",
            "origin = 'PROD'; DROP TABLE dataset_registry",
            # spec/API.md §`dataset_filter` grammar — "`is_primary = 'true'` is a
            # syntax error (`422 INVALID_DATASET_FILTER`), as is using a boolean
            # column with `IN` or a scalar/array column with a bare word".
            "is_primary = 'true'",
            "is_primary IN ('true')",
            "'true' IN is_primary",
            "origin = TRUE",
        ],
    )
    def test_malformed_filter_raises_the_filter_syntax_error(self, dataset_filter: str) -> None:
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            validate_dataset_filter(dataset_filter)
        assert excinfo.value.error_code == "INVALID_DATASET_FILTER"
        assert excinfo.value.detail == {"position": excinfo.value.position}

    def test_a_quoted_boolean_reports_the_position_of_its_opening_quote(self) -> None:
        """The 422 body must point the editor at the quote it has to delete.

        A position that merely existed (0, or the end of the string) would satisfy the
        envelope check above while telling the operator nothing, so the exact index is
        asserted here.

        spec: API.md §Error Catalogue — "INVALID_DATASET_FILTER | 422 | […] `detail`
            carries the character position of the error";
        spec: API.md §`dataset_filter` grammar — "`is_primary = 'true'` is a syntax
            error".
        """
        dataset_filter = "origin = 'DEV' AND is_primary = 'true'"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            validate_dataset_filter(dataset_filter)

        assert excinfo.value.error_code == "INVALID_DATASET_FILTER"
        assert excinfo.value.detail == {"position": dataset_filter.index("'true'")}
        assert dataset_filter[excinfo.value.position] == "'"

    def test_the_syntax_error_is_not_a_value_error(self) -> None:
        """Pydantic v2 re-raises non-ValueError exceptions out of a validator
        unchanged, which is what keeps the code and position intact instead of
        collapsing into the generic INVALID_PARAMETER envelope."""
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            validate_dataset_filter("origin = ")
        assert not isinstance(excinfo.value, ValueError)

    def test_over_character_cap_is_rejected(self) -> None:
        prefix = "origin = '"
        over_cap = prefix + "x" * (MAX_FILTER_CHARS - len(prefix)) + "'"
        assert len(over_cap) == MAX_FILTER_CHARS + 1
        with pytest.raises(DatasetFilterSyntaxError):
            validate_dataset_filter(over_cap)

    def test_over_literal_cap_is_rejected(self) -> None:
        values = ", ".join(f"'v{i}'" for i in range(MAX_STRING_LITERALS + 1))
        with pytest.raises(DatasetFilterSyntaxError):
            validate_dataset_filter(f"origin IN ({values})")

    def test_at_the_literal_cap_is_accepted(self) -> None:
        """The cap is inclusive — 1,000 literals is admissible, so the rejection
        above is a cap check and not a blanket refusal of large filters."""
        values = ", ".join(f"'v{i}'" for i in range(MAX_STRING_LITERALS))
        validate_dataset_filter(f"origin IN ({values})")


class TestUrnErrorsCarryTheUrnCode:
    """spec/API.md §Error Catalogue: 'INVALID_DATASET_URN | 422 | A `dataset_urn`
    literal inside a `dataset_filter` is not a well-formed `urn:li:dataset:(…)`
    URN.'"""

    @pytest.mark.parametrize(
        "bad", ["not-a-urn", "urn:li:dataset:postgres", "", "urn:li:dataset"]
    )
    def test_malformed_dataset_urn_literal_raises_the_urn_error(self, bad: str) -> None:
        with pytest.raises(InvalidDatasetUrnError) as excinfo:
            validate_dataset_filter(f"dataset_urn = '{bad}'")
        assert excinfo.value.error_code == "INVALID_DATASET_URN"

    def test_malformed_literal_in_an_in_list_raises_the_urn_error(self) -> None:
        with pytest.raises(InvalidDatasetUrnError):
            validate_dataset_filter(f"dataset_urn IN ('{_URN}', 'not-a-urn')")

    def test_non_urn_values_on_other_columns_are_not_urn_checked(self) -> None:
        """Only `dataset_urn` literals carry a URN shape (spec/API.md
        §`dataset_filter` grammar — column table)."""
        validate_dataset_filter("origin = 'not-a-urn' AND 'not-a-urn' IN tag_urns")


def test_field_description_states_the_caps_and_columns() -> None:
    """The OpenAPI description is the only place a client sees the grammar, so it
    must name the columns and both caps (spec/API.md §`dataset_filter` grammar)."""
    for column in [
        "dataset_urn",
        "origin",
        "platform_urn",
        "tag_urns",
        "glossary_term_urns",
        "is_primary",
    ]:
        assert column in DATASET_FILTER_FIELD_DESCRIPTION
    # The boolean column's literal form is the one a client cannot guess from the
    # column name: spec/API.md §`dataset_filter` grammar makes `is_primary = 'true'`
    # an error, so the description has to show the predicate's written form and say
    # the value is unquoted. Asserting the words TRUE/FALSE alone would be satisfied
    # by the unrelated "AND/OR/IN/TRUE/FALSE are case-insensitive" sentence.
    # spec/API.md §`dataset_filter` grammar — `bool := TRUE | FALSE -- bare word,
    # never quoted`.
    assert "column = TRUE" in DATASET_FILTER_FIELD_DESCRIPTION
    assert "column = FALSE" in DATASET_FILTER_FIELD_DESCRIPTION
    assert "never quoted" in DATASET_FILTER_FIELD_DESCRIPTION
    assert f"{MAX_FILTER_CHARS:,}" in DATASET_FILTER_FIELD_DESCRIPTION
    assert f"{MAX_STRING_LITERALS:,}" in DATASET_FILTER_FIELD_DESCRIPTION
