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
            # Negated productions — spec/API.md §`dataset_filter` grammar §Negation.
            "origin != 'PROD'",
            "origin NOT IN ('PROD', 'DEV')",
            f"dataset_urn != '{_URN}'",
            "platform_urn NOT IN ('urn:li:dataPlatform:kafka')",
            "'urn:li:tag:lifecycle:deprecated' NOT IN tag_urns",
            "'urn:li:glossaryTerm:pii.gdpr' NOT IN glossary_term_urns",
        ],
    )
    def test_grammar_forms_pass_the_write_boundary(self, dataset_filter: str) -> None:
        """Every production of spec/API.md §`dataset_filter` grammar is writable."""
        validate_dataset_filter(dataset_filter)

    def test_a_composed_negated_filter_passes_the_write_boundary(self) -> None:
        """The negated worked example reaches the API through the same entry point.

        The parametrized cases above each exercise one negated production; this is the
        composition a caller actually writes — a scalar `!=` AND-ed with an array
        `NOT IN` — proving the write boundary accepts the grammar as a whole and not
        merely predicate by predicate.

        spec: API.md §`dataset_filter` grammar — Negation: "`origin != 'DEV' AND
            platform_urn NOT IN ('urn:li:dataPlatform:kafka') AND
            'urn:li:tag:lifecycle:deprecated' NOT IN tag_urns`".
        """
        validate_dataset_filter(
            "origin != 'DEV'"
            " AND platform_urn NOT IN ('urn:li:dataPlatform:kafka')"
            " AND 'urn:li:tag:lifecycle:deprecated' NOT IN tag_urns"
        )

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


def test_field_description_states_the_negated_operators() -> None:
    """The description is the only place a client learns negation exists.

    The parser accepts `!=` / `NOT IN` on the scalar and array columns, and this string
    is the OpenAPI text every `dataset_filter` field (ontogen, metagen, metrics) carries.
    An affirmative-only description would publish a narrower grammar than the API accepts,
    with nothing else in the suite noticing — the acceptance tests above pass either way.

    spec: API.md §`dataset_filter` grammar — `predicate := scalar_col ('=' | '!=') string
        | scalar_col [NOT] IN '(' string {',' string} ')' | string [NOT] IN array_col`;
        §Negation — "Negation is available on the scalar and array columns only — the
        boolean column takes `=` alone".
    """
    assert "!=" in DATASET_FILTER_FIELD_DESCRIPTION, (
        "the scalar not-equals operator must be named; without it the published grammar "
        f"is affirmative-only. Got: {DATASET_FILTER_FIELD_DESCRIPTION}"
    )
    assert "NOT IN" in DATASET_FILTER_FIELD_DESCRIPTION, (
        "the negated membership spelling must be named for both the scalar list form and "
        f"the array form. Got: {DATASET_FILTER_FIELD_DESCRIPTION}"
    )
    assert "'value' NOT IN column" in DATASET_FILTER_FIELD_DESCRIPTION, (
        "the array form's negated spelling puts the value first, which a client cannot "
        f"guess from the scalar form. Got: {DATASET_FILTER_FIELD_DESCRIPTION}"
    )
    # spec: API.md §`dataset_filter` grammar — "Keywords (`AND`, `OR`, `NOT`, `IN`) and
    # column names are case-insensitive". `NOT` belongs in that list alongside the other
    # three, so a reader learns it is a keyword and not part of a column name.
    keyword_sentence = next(
        sentence
        for sentence in DATASET_FILTER_FIELD_DESCRIPTION.split(". ")
        if "case-insensitive" in sentence
    )
    for keyword in ("AND", "OR", "NOT", "IN"):
        assert keyword in keyword_sentence, (
            f"{keyword} must appear in the case-insensitivity sentence; got "
            f"{keyword_sentence!r}"
        )
