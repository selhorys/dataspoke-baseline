"""Tests for src/shared/dataset_filter.py — the ``dataset_filter`` grammar.

One grammar serves UC3 ontogen, UC4 metagen and UC5 governance metrics, and it is
the only thing standing between operator-supplied text and a database query, so
this suite is written against the grammar as spec/API.md states it rather than
against the parser's internals.

Spec traceability:
- spec/API.md §``dataset_filter`` grammar — productions, column table, keyword
  case rules, AND/OR mixing, caps, nesting depth, error positions
- spec/feature/BACKEND.md §Dataset resolution — "Every literal compiles to a
  bound parameter" and "the column set is the grammar's own whitelist"
- spec/feature/FRONTEND_BASIC.md §Shared Component Notes — DatasetFilterEditor's
  Auto-indent layout, of which ``format_filter`` is the executable reference
"""

import pytest
from sqlalchemy.dialects import postgresql

from src.shared.dataset_filter import (
    MAX_FILTER_CHARS,
    MAX_STRING_LITERALS,
    ArrayContains,
    BoolEquals,
    DatasetFilterSyntaxError,
    Equals,
    InList,
    check_dataset_urn_literals,
    filter_clause,
    format_filter,
    literal_dataset_urns,
    parse_filter,
)
from src.shared.exceptions import InvalidDatasetUrnError

_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,PROD)"
_URN_2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.author_master,PROD)"


def _compile(filter_text: str) -> tuple[str, dict[str, object]]:
    """Compile a filter to (SQL text, bound parameters) — no literal inlining.

    The compile deliberately does NOT use ``literal_binds``: the whole point of
    the security invariant is that values travel out of band, so the rendered SQL
    text is what a reader must inspect for leaked user input, and ``.params`` is
    where every value must instead be found.
    """
    compiled = filter_clause(parse_filter(filter_text)).compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


# ── Empty filter ─────────────────────────────────────────────────────────────


class TestEmptyFilter:
    """spec/API.md §``dataset_filter`` grammar: ``filter := ε | expr`` — "the empty
    string matches every registered dataset"."""

    @pytest.mark.parametrize("text", ["", "   ", "\n\t ", None])
    def test_empty_text_parses_to_the_match_everything_filter(self, text: str | None) -> None:
        assert parse_filter(text).is_empty is True

    def test_empty_filter_compiles_to_an_unconditional_true(self) -> None:
        sql, params = _compile("")
        assert sql.strip().lower() == "true"
        assert params == {}

    def test_empty_filter_names_no_dataset_urn_literals(self) -> None:
        assert literal_dataset_urns(parse_filter("")) == []


# ── Predicates ───────────────────────────────────────────────────────────────


class TestScalarEquality:
    """``predicate := scalar_col '=' string``; scalar columns are
    ``dataset_urn`` / ``origin`` / ``platform_urn`` (spec/API.md §``dataset_filter``
    grammar — column table)."""

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("dataset_urn", _URN),
            ("origin", "PROD"),
            ("platform_urn", "urn:li:dataPlatform:postgres"),
        ],
    )
    def test_every_scalar_column_accepts_equality(self, column: str, value: str) -> None:
        ast = parse_filter(f"{column} = '{value}'")
        assert ast.root == Equals(column=column, value=value)

    def test_equality_compiles_to_an_equality_on_the_named_registry_column(self) -> None:
        """The predicate is ``scalar_col '=' string`` — an inequality names the same
        column and binds the same value while selecting the exact complement of the
        requested scope, so the rendered operator is asserted, not just the column."""
        sql, params = _compile("origin = 'PROD'")
        (name,) = params
        assert f"dataset_registry.origin = %({name})s" in sql, (
            f"expected an equality on origin; got:\n{sql}"
        )
        assert " != " not in sql and "<>" not in sql, f"the predicate is negated:\n{sql}"
        assert list(params.values()) == ["PROD"]

    def test_an_array_column_cannot_be_compared_with_equals(self) -> None:
        """``tag_urns`` is an array column, so ``= 'x'`` is not in the grammar."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("tag_urns = 'urn:li:tag:pii'")

    def test_an_unknown_column_is_rejected(self) -> None:
        """The column set is a closed whitelist (spec/feature/BACKEND.md §Dataset
        resolution — "the column set is the grammar's own whitelist")."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("owner = 'alice'")

    def test_a_scalar_column_requires_a_quoted_value(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = PROD")


class TestScalarInList:
    """``predicate := scalar_col IN '(' string {',' string} ')'``."""

    def test_in_list_parses_every_value_in_source_order(self) -> None:
        ast = parse_filter("origin IN ('PROD', 'DEV', 'STG')")
        assert ast.root == InList(column="origin", values=("PROD", "DEV", "STG"))

    def test_single_element_in_list_is_accepted(self) -> None:
        assert parse_filter("origin IN ('PROD')").root == InList(column="origin", values=("PROD",))

    def test_in_list_compiles_to_membership_and_binds_the_values_it_names(self) -> None:
        """``IN`` selects the rows whose column is one of the named values. Rendered
        as ``!= ALL(...)`` it would name the same column and bind the same list while
        returning every *other* row, so the operator itself is under test."""
        sql, params = _compile("origin IN ('PROD', 'DEV')")
        (name,) = params
        assert f"dataset_registry.origin = any (%({name})s" in sql.lower(), (
            f"expected `origin = ANY(...)` membership; got:\n{sql}"
        )
        assert " != " not in sql and "<>" not in sql, f"the predicate is negated:\n{sql}"
        assert [value for value in params.values()] == [["PROD", "DEV"]]

    def test_empty_in_list_is_rejected(self) -> None:
        """The production requires at least one ``string``."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin IN ()")

    def test_unterminated_in_list_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin IN ('PROD'")


class TestArrayContains:
    """``predicate := string IN array_col``; array columns are ``tag_urns`` /
    ``glossary_term_urns`` (spec/API.md §``dataset_filter`` grammar)."""

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("tag_urns", "urn:li:tag:area:catalog"),
            ("glossary_term_urns", "urn:li:glossaryTerm:pii.gdpr"),
        ],
    )
    def test_every_array_column_accepts_membership(self, column: str, value: str) -> None:
        ast = parse_filter(f"'{value}' IN {column}")
        assert ast.root == ArrayContains(column=column, value=value)

    def test_membership_compiles_to_containment_on_the_named_registry_column(self) -> None:
        """``string IN array_col`` keeps the rows whose array *holds* the value; the
        complement (rows lacking it) is the failure this asserts against, so the
        containment operator is asserted alongside the column."""
        sql, params = _compile("'urn:li:tag:area:catalog' IN tag_urns")
        (name,) = params
        assert f"dataset_registry.tag_urns @> %({name})s" in sql, (
            f"expected `tag_urns @> ARRAY[value]` containment; got:\n{sql}"
        )
        assert " not " not in f" {sql.lower()} ", f"the predicate is negated:\n{sql}"
        assert [value for value in params.values()] == [["urn:li:tag:area:catalog"]]

    def test_a_scalar_column_cannot_be_used_as_a_membership_target(self) -> None:
        """``'PROD' IN origin`` inverts the grammar's two IN forms."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("'PROD' IN origin")

    def test_an_unknown_array_column_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("'x' IN owner_urns")

    def test_a_bare_string_without_in_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("'urn:li:tag:pii'")


class TestBooleanPredicate:
    """``predicate := bool_col '=' bool``; the sole boolean column is
    ``is_primary`` and ``bool := TRUE | FALSE`` is "a bare word, never quoted"
    (spec/API.md §``dataset_filter`` grammar — grammar block and column table)."""

    def test_true_parses_to_a_boolean_predicate(self) -> None:
        assert parse_filter("is_primary = true").root == BoolEquals(
            column="is_primary", value=True
        )

    def test_false_parses_to_the_opposite_boolean_predicate(self) -> None:
        """The two words select disjoint sets, so the parsed value is asserted —
        a parser that mapped both words to ``True`` would scope every filter to
        the whole registry."""
        assert parse_filter("is_primary = false").root == BoolEquals(
            column="is_primary", value=False
        )

    @pytest.mark.parametrize("word", ["TRUE", "True", "tRuE"])
    def test_the_bare_word_is_case_insensitive(self, word: str) -> None:
        """spec/API.md §``dataset_filter`` grammar: "``TRUE``/``FALSE`` are
        case-insensitive bare words"."""
        assert parse_filter(f"is_primary = {word}").root == BoolEquals(
            column="is_primary", value=True
        )

    @pytest.mark.parametrize("word", ["FALSE", "False", "fAlSe"])
    def test_the_false_word_is_case_insensitive_too(self, word: str) -> None:
        assert parse_filter(f"is_primary = {word}").root == BoolEquals(
            column="is_primary", value=False
        )

    @pytest.mark.parametrize("column", ["is_primary", "IS_PRIMARY", "Is_Primary"])
    def test_the_boolean_column_name_is_case_insensitive(self, column: str) -> None:
        """"column names are case-insensitive" is not qualified by column kind
        (spec/API.md §``dataset_filter`` grammar)."""
        assert parse_filter(f"{column} = TRUE").root == BoolEquals(
            column="is_primary", value=True
        )

    def test_true_compiles_to_the_constant_predicate_on_the_registry_column(self) -> None:
        """``is_primary = true`` renders as the SQL constant comparison.

        The spelling is asserted, not merely the column. It is the one carve-out
        from the bound-parameter rule and the spec states it as such:

        spec/feature/BACKEND.md §Dataset resolution — "**Every user-supplied literal
            compiles to a bound parameter** — the ``is_primary`` boolean is the one
            exception: it is never user text but one of two parser-selected Python
            constants, and it renders inline (``is_primary = true`` / ``= false``) so
            that the partial index ``WHERE NOT is_primary`` stays reachable, which
            neither a bound parameter nor an ``IS false`` boolean test achieves";
        spec/feature/BACKEND_SCHEMA.md §Indexes — the index that carve-out serves:
            "``ix_dataset_registry_not_primary``: ``(is_primary) WHERE NOT
            is_primary`` | ``is_primary = false`` predicates in ``dataset_filter``".
        """
        sql, params = _compile("is_primary = true")
        assert "dataset_registry.is_primary = true" in sql, (
            f"expected the constant boolean comparison; got:\n{sql}"
        )
        assert " not " not in f" {sql.lower()} ", f"the predicate is negated:\n{sql}"
        assert params == {}, (
            f"a boolean predicate carries no user-supplied literal, so it binds no "
            f"parameter; got {params!r}"
        )

    def test_false_compiles_to_the_complementary_constant(self) -> None:
        sql, params = _compile("is_primary = false")
        assert "dataset_registry.is_primary = false" in sql, (
            f"expected the false-side constant comparison; got:\n{sql}"
        )
        assert params == {}

    def test_a_quoted_boolean_is_a_syntax_error_at_the_opening_quote(self) -> None:
        """spec/API.md §``dataset_filter`` grammar: "``is_primary = 'true'`` is a
        syntax error (``422 INVALID_DATASET_FILTER``)".

        Silently coercing the string would be the dangerous outcome: the filter
        would read as a boolean to its author while matching a different set.
        """
        text = "is_primary = 'true'"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("'")

    def test_a_boolean_column_with_in_is_a_syntax_error_at_the_keyword(self) -> None:
        """spec/API.md §``dataset_filter`` grammar: "using a boolean column with
        ``IN``" is a syntax error."""
        text = "is_primary IN ('true')"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("IN")

    def test_a_boolean_column_as_a_membership_target_names_its_kind(self) -> None:
        """``'x' IN is_primary`` inverts the grammar: ``is_primary`` is neither an
        array column nor a scalar one, and the error names the kind it actually is.

        spec/feature/BACKEND.md §Dataset resolution — "the column set is the
            grammar's own whitelist, partitioned by kind (scalar, array, boolean) so
            that a column used with the wrong operator is a parse error naming the
            kind it actually is". The assertion stays a substring check: the spec
            fixes the kind word, not the sentence around it.
        """
        text = "'x' IN is_primary"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("is_primary")
        assert "boolean" in str(excinfo.value).lower(), (
            f"the message must name the column's actual kind; got {excinfo.value}"
        )

    def test_a_scalar_column_with_a_bare_word_is_a_syntax_error(self) -> None:
        """The complement of the quoted-boolean rule: spec/API.md §``dataset_filter``
        grammar rejects "a scalar/array column with a bare word"."""
        text = "origin = TRUE"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("TRUE")

    @pytest.mark.parametrize("text", ["TRUE IN tag_urns", "true IN tag_urns"])
    def test_an_array_column_with_a_bare_word_is_a_syntax_error(self, text: str) -> None:
        """The array half of the same rule — spec/API.md §``dataset_filter`` grammar
        rejects "a scalar/**array** column with a bare word".

        The array production is ``string IN array_col``, so the bare word sits where a
        predicate's opening token goes and is read as a column name: the rejection
        arrives as an unknown column at position 0, not as a membership-operand kind
        error. The two membership-operand kind errors are covered next door
        (``is_primary IN ('true')`` and ``'x' IN is_primary``).

        spec/API.md §Error Catalogue — ``INVALID_DATASET_FILTER`` covers a filter that
            "does not parse under the filter grammar, **names an unknown column**, or
            exceeds a payload cap. `detail` carries the character position of the
            error".
        """
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == 0, (
            f"the error must point at the bare word that opens the predicate; got "
            f"position {excinfo.value.position} for {text!r}"
        )
        assert "unknown column" in str(excinfo.value), (
            f"a bare word where a predicate opens is read as a column name, so the "
            f"rejection has to name it as an unknown column; got {excinfo.value}"
        )

    @pytest.mark.parametrize("value", ["1", "0", "yes", "maybe", "null"])
    def test_only_the_two_documented_words_are_accepted(self, value: str) -> None:
        """``bool := TRUE | FALSE`` admits exactly two words — no numeric or
        truthy-looking stand-ins (spec/API.md §``dataset_filter`` grammar)."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter(f"is_primary = {value}")

    def test_a_boolean_column_without_a_value_is_rejected(self) -> None:
        """The production requires the ``=`` and the word — a bare column name is
        not a predicate."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("is_primary")

    def test_a_boolean_predicate_composes_with_and(self) -> None:
        ast = parse_filter("origin = 'DEV' AND is_primary = true")
        assert ast.root is not None
        assert getattr(ast.root, "op", None) == "AND"
        assert getattr(ast.root, "children", ()) == (
            Equals(column="origin", value="DEV"),
            BoolEquals(column="is_primary", value=True),
        )

    def test_a_boolean_predicate_composes_with_or(self) -> None:
        ast = parse_filter("is_primary = false OR 'urn:li:tag:pii' IN tag_urns")
        assert getattr(ast.root, "op", None) == "OR"
        assert getattr(ast.root, "children", ()) == (
            BoolEquals(column="is_primary", value=False),
            ArrayContains(column="tag_urns", value="urn:li:tag:pii"),
        )

    def test_a_boolean_predicate_nests_inside_parentheses(self) -> None:
        """Nothing about the new production is confined to the top level — it is a
        ``predicate``, so it appears wherever a ``term`` may (spec/API.md
        §``dataset_filter`` grammar)."""
        ast = parse_filter(
            "origin = 'DEV' AND (is_primary = true OR (is_primary = false "
            "AND 'urn:li:tag:pii' IN tag_urns))"
        )
        assert ast.is_empty is False
        sql, params = _compile(
            "origin = 'DEV' AND (is_primary = true OR (is_primary = false "
            "AND 'urn:li:tag:pii' IN tag_urns))"
        )
        assert "dataset_registry.is_primary = true" in sql
        assert "dataset_registry.is_primary = false" in sql
        assert sorted(str(value) for value in params.values()) == sorted(
            ["DEV", str(["urn:li:tag:pii"])]
        )

    def test_the_composite_spec_example_parses_and_compiles(self) -> None:
        """spec/API.md §``dataset_filter`` grammar prints this as its worked
        example: ``origin = 'PROD' AND is_primary = true AND ('urn:li:tag:area:catalog'
        IN tag_urns OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)``."""
        sql, params = _compile(
            "origin = 'PROD' AND is_primary = true "
            "AND ('urn:li:tag:area:catalog' IN tag_urns "
            "OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)"
        )
        assert "dataset_registry.is_primary = true" in sql
        assert sorted(str(value) for value in params.values()) == sorted(
            ["PROD", str(["urn:li:tag:area:catalog"]), str(["urn:li:glossaryTerm:pii.gdpr"])]
        )

    def test_a_boolean_predicate_names_no_dataset_urn_literal(self) -> None:
        """``literal_dataset_urns`` walks every node kind; a boolean predicate
        carries no string literal at all, so the walk must neither raise nor invent
        one (spec/API.md §``dataset_filter`` grammar — ``unresolved_urns`` reports
        ``dataset_urn`` literals)."""
        ast = parse_filter(f"dataset_urn = '{_URN}' AND is_primary = true")
        assert literal_dataset_urns(ast) == [_URN]
        check_dataset_urn_literals(ast)


# ── Case sensitivity ─────────────────────────────────────────────────────────


class TestCaseRules:
    """spec/API.md §``dataset_filter`` grammar: "Keywords (``AND``, ``OR``, ``IN``)
    and column names are case-insensitive; values are case-sensitive"."""

    @pytest.mark.parametrize(
        "text",
        [
            "origin = 'PROD' and platform_urn = 'urn:li:dataPlatform:postgres'",
            "origin = 'PROD' AND platform_urn = 'urn:li:dataPlatform:postgres'",
            "origin = 'PROD' And platform_urn = 'urn:li:dataPlatform:postgres'",
        ],
    )
    def test_and_is_case_insensitive(self, text: str) -> None:
        ast = parse_filter(text)
        assert ast.root is not None
        assert getattr(ast.root, "op", None) == "AND"

    @pytest.mark.parametrize("keyword", ["or", "OR", "oR"])
    def test_or_is_case_insensitive(self, keyword: str) -> None:
        ast = parse_filter(f"origin = 'PROD' {keyword} origin = 'DEV'")
        assert ast.root is not None
        assert getattr(ast.root, "op", None) == "OR"

    @pytest.mark.parametrize("keyword", ["in", "IN", "In"])
    def test_in_is_case_insensitive_in_both_forms(self, keyword: str) -> None:
        assert parse_filter(f"origin {keyword} ('PROD')").root == InList(
            column="origin", values=("PROD",)
        )
        assert parse_filter(f"'urn:li:tag:pii' {keyword} tag_urns").root == ArrayContains(
            column="tag_urns", value="urn:li:tag:pii"
        )

    @pytest.mark.parametrize("column", ["origin", "ORIGIN", "Origin"])
    def test_column_names_are_case_insensitive(self, column: str) -> None:
        assert parse_filter(f"{column} = 'PROD'").root == Equals(column="origin", value="PROD")

    @pytest.mark.parametrize("column", ["tag_urns", "TAG_URNS", "Tag_Urns"])
    def test_array_column_names_are_case_insensitive_too(self, column: str) -> None:
        """"column names are case-insensitive" is not qualified by column kind."""
        assert parse_filter(f"'urn:li:tag:pii' IN {column}").root == ArrayContains(
            column="tag_urns", value="urn:li:tag:pii"
        )

    def test_values_keep_their_case_verbatim(self) -> None:
        """A value is data, not syntax: ``'prod'`` and ``'PROD'`` are different
        filters and neither is normalised."""
        assert parse_filter("origin = 'prod'").root == Equals(column="origin", value="prod")
        _, params = _compile("origin = 'PrOd'")
        assert list(params.values()) == ["PrOd"]


# ── Quoting ──────────────────────────────────────────────────────────────────


class TestQuoting:
    """spec/API.md §``dataset_filter`` grammar: ``string := '...'`` — "single
    quotes only; ``''`` escapes a quote"."""

    def test_doubled_quote_yields_one_literal_quote(self) -> None:
        assert parse_filter("origin = 'O''Brien'").root == Equals(
            column="origin", value="O'Brien"
        )

    def test_a_value_that_is_only_an_escaped_quote(self) -> None:
        assert parse_filter("origin = ''''").root == Equals(column="origin", value="'")

    def test_empty_string_value_is_a_value_not_an_empty_filter(self) -> None:
        ast = parse_filter("origin = ''")
        assert ast.is_empty is False
        assert ast.root == Equals(column="origin", value="")

    def test_escaped_quotes_survive_a_format_round_trip(self) -> None:
        formatted = format_filter("origin = 'O''Brien'")
        assert parse_filter(formatted).root == Equals(column="origin", value="O'Brien")

    def test_double_quotes_are_not_a_string_delimiter(self) -> None:
        """Only single quotes open a string, so a double-quoted token is not a value."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter('origin = "PROD"')

    def test_unterminated_string_is_rejected_at_its_opening_quote(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter("origin = 'PROD")
        assert excinfo.value.position == len("origin = ")


# ── Boolean composition and nesting ──────────────────────────────────────────


class TestBooleanComposition:
    """spec/API.md §``dataset_filter`` grammar: ``expr := term {(AND|OR) term}`` —
    "one operator kind per level"; "Mixing ``AND`` and ``OR`` at one level requires
    parentheses"."""

    def test_and_chain_at_one_level_is_accepted(self) -> None:
        ast = parse_filter("origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns AND origin = 'PROD'")
        assert ast.root is not None
        assert getattr(ast.root, "op", None) == "AND"
        assert len(getattr(ast.root, "children", ())) == 3

    def test_mixed_and_or_at_one_level_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError, match="parenthes"):
            parse_filter("origin = 'PROD' AND origin = 'DEV' OR origin = 'STG'")

    def test_mixed_or_then_and_at_one_level_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError, match="parenthes"):
            parse_filter("origin = 'PROD' OR origin = 'DEV' AND origin = 'STG'")

    def test_the_same_mix_is_accepted_once_parenthesised(self) -> None:
        ast = parse_filter("origin = 'PROD' AND (origin = 'DEV' OR origin = 'STG')")
        assert ast.root is not None
        assert getattr(ast.root, "op", None) == "AND"

    def test_and_compiles_to_a_conjunction_of_both_operands(self) -> None:
        sql, params = _compile("origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns")
        assert " and " in sql.lower()
        assert sorted(str(value) for value in params.values()) == sorted(
            ["PROD", str(["urn:li:tag:pii"])]
        )

    def test_or_compiles_to_a_disjunction(self) -> None:
        sql, _ = _compile("origin = 'PROD' OR origin = 'DEV'")
        assert " or " in sql.lower()

    def test_a_trailing_operator_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = 'PROD' AND")

    def test_two_predicates_without_an_operator_are_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = 'PROD' origin = 'DEV'")


class TestNestingDepth:
    """spec/API.md §``dataset_filter`` grammar — Nesting depth: "counts
    parenthesised groups, with the unparenthesised top level as depth 0.
    ``a AND (b OR c)`` is depth 1 and ``a AND (b OR (c AND d))`` is depth 2 — both
    accepted; a third parenthesised level [...] is rejected"."""

    def test_depth_0_unparenthesised_top_level_is_accepted(self) -> None:
        assert parse_filter("origin = 'PROD' AND origin = 'DEV'").is_empty is False

    def test_depth_1_is_accepted(self) -> None:
        ast = parse_filter("origin = 'A' AND (origin = 'B' OR origin = 'C')")
        assert ast.is_empty is False

    def test_depth_2_is_accepted(self) -> None:
        """The spec's own depth-2 example: ``a AND (b OR (c AND d))``."""
        ast = parse_filter(
            "origin = 'A' AND (origin = 'B' OR (origin = 'C' AND origin = 'D'))"
        )
        assert ast.is_empty is False

    def test_depth_3_is_rejected(self) -> None:
        """The spec's own rejected example: ``a AND (b OR (c AND (d OR e)))``."""
        with pytest.raises(DatasetFilterSyntaxError, match="nest"):
            parse_filter(
                "origin = 'A' AND (origin = 'B' OR "
                "(origin = 'C' AND (origin = 'D' OR origin = 'E')))"
            )

    def test_depth_3_is_rejected_at_the_third_opening_parenthesis(self) -> None:
        text = (
            "origin = 'A' AND (origin = 'B' OR "
            "(origin = 'C' AND (origin = 'D' OR origin = 'E')))"
        )
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert text[excinfo.value.position] == "("
        assert text.count("(", 0, excinfo.value.position) == 2

    def test_sibling_groups_at_one_level_do_not_accumulate_depth(self) -> None:
        """Depth is *nesting*, not a count of groups: two groups side by side are
        both depth 1, however many of them there are."""
        ast = parse_filter(
            "(origin = 'A' OR origin = 'B') AND (origin = 'C' OR origin = 'D') "
            "AND (origin = 'E' OR origin = 'F')"
        )
        assert ast.is_empty is False
        assert getattr(ast.root, "op", None) == "AND"
        assert len(getattr(ast.root, "children", ())) == 3

    def test_redundant_parentheses_still_count_toward_depth(self) -> None:
        """Depth counts parenthesised groups, so three nested groups are three
        levels even when each wraps a single predicate."""
        with pytest.raises(DatasetFilterSyntaxError, match="nest"):
            parse_filter("(((origin = 'A')))")

    def test_an_unbalanced_opening_parenthesis_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("(origin = 'PROD'")

    def test_an_unbalanced_closing_parenthesis_is_rejected(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = 'PROD')")


# ── Caps ─────────────────────────────────────────────────────────────────────


class TestCaps:
    """spec/API.md §``dataset_filter`` grammar — Caps: "filter text ≤ 8,000
    characters and ≤ 1,000 string literals"."""

    def test_the_documented_cap_values(self) -> None:
        assert MAX_FILTER_CHARS == 8_000
        assert MAX_STRING_LITERALS == 1_000

    def test_text_exactly_at_the_character_cap_is_accepted(self) -> None:
        """The cap is inclusive — 8,000 characters is admissible."""
        prefix = "origin = '"
        text = prefix + "x" * (MAX_FILTER_CHARS - len(prefix) - 1) + "'"
        assert len(text) == MAX_FILTER_CHARS
        assert parse_filter(text).is_empty is False

    def test_text_one_character_over_the_cap_is_rejected(self) -> None:
        prefix = "origin = '"
        text = prefix + "x" * (MAX_FILTER_CHARS - len(prefix)) + "'"
        assert len(text) == MAX_FILTER_CHARS + 1
        with pytest.raises(DatasetFilterSyntaxError, match=str(MAX_FILTER_CHARS)):
            parse_filter(text)

    def test_exactly_the_literal_cap_is_accepted(self) -> None:
        values = ", ".join(f"'v{i}'" for i in range(MAX_STRING_LITERALS))
        ast = parse_filter(f"origin IN ({values})")
        assert isinstance(ast.root, InList)
        assert len(ast.root.values) == MAX_STRING_LITERALS

    def test_one_literal_over_the_cap_is_rejected(self) -> None:
        values = ", ".join(f"'v{i}'" for i in range(MAX_STRING_LITERALS + 1))
        with pytest.raises(DatasetFilterSyntaxError, match=str(MAX_STRING_LITERALS)):
            parse_filter(f"origin IN ({values})")

    def test_the_character_cap_is_checked_before_parsing(self) -> None:
        """An over-long filter is refused on size, not on whatever syntax error a
        parse of it would happen to hit first — so an over-cap payload never
        drives the parser at all."""
        with pytest.raises(DatasetFilterSyntaxError, match=str(MAX_FILTER_CHARS)):
            parse_filter("!" * (MAX_FILTER_CHARS + 1))


# ── Error positions ──────────────────────────────────────────────────────────


class TestErrorPositions:
    """spec/API.md §``dataset_filter`` grammar: "A malformed filter returns
    ``422 INVALID_DATASET_FILTER`` carrying the character position of the error"."""

    def test_the_error_carries_the_offending_character_index(self) -> None:
        text = "origin = 'PROD' AND ~"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("~")

    def test_an_unknown_column_reports_the_column_position(self) -> None:
        text = "origin = 'PROD' AND owner = 'alice'"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("owner")

    def test_a_mixed_operator_reports_the_offending_operator_position(self) -> None:
        text = "origin = 'A' AND origin = 'B' OR origin = 'C'"
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter(text)
        assert excinfo.value.position == text.index("OR")

    def test_the_position_is_carried_in_detail_for_the_error_envelope(self) -> None:
        """The 422 body carries the position, so it lives in ``detail`` and not
        only on the exception object (spec/API.md §Error Catalogue)."""
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter("origin ~ 'PROD'")
        assert excinfo.value.detail == {"position": excinfo.value.position}
        assert excinfo.value.error_code == "INVALID_DATASET_FILTER"

    def test_the_message_does_not_echo_an_unbounded_fragment(self) -> None:
        """A 422 body's size must not be dictated by the request that caused it."""
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter("z" * 500 + " = 'x'")
        assert len(str(excinfo.value)) < 200

    def test_control_characters_are_scrubbed_from_the_message(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            parse_filter("origin = 'PROD' AND \x00")
        assert "\x00" not in str(excinfo.value)


# ── dataset_urn literals ─────────────────────────────────────────────────────


class TestDatasetUrnLiterals:
    """spec/API.md §``dataset_filter`` grammar: "A ``dataset_urn`` literal that is
    not a well-formed URN returns ``422 INVALID_DATASET_URN``; ``dataset_urn``
    literals that match no registered dataset at run time are reported in the
    ``METRIC.RUN_COMPLETE`` event's ``unresolved_urns`` field"."""

    def test_equality_literal_is_reported(self) -> None:
        assert literal_dataset_urns(parse_filter(f"dataset_urn = '{_URN}'")) == [_URN]

    def test_in_list_literals_are_reported_in_source_order(self) -> None:
        ast = parse_filter(f"dataset_urn IN ('{_URN}', '{_URN_2}')")
        assert literal_dataset_urns(ast) == [_URN, _URN_2]

    def test_literals_nested_under_parentheses_are_reported(self) -> None:
        ast = parse_filter(f"origin = 'PROD' AND (dataset_urn = '{_URN}' OR origin = 'DEV')")
        assert literal_dataset_urns(ast) == [_URN]

    def test_a_repeated_literal_is_reported_once(self) -> None:
        ast = parse_filter(f"dataset_urn = '{_URN}' OR dataset_urn = '{_URN}'")
        assert literal_dataset_urns(ast) == [_URN]

    def test_literals_of_other_columns_are_not_reported(self) -> None:
        ast = parse_filter("origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns")
        assert literal_dataset_urns(ast) == []

    def test_a_well_formed_urn_literal_passes_the_urn_check(self) -> None:
        check_dataset_urn_literals(parse_filter(f"dataset_urn = '{_URN}'"))

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-urn",
            "urn:li:dataset:postgres",
            "",
            "urn:li:chart:(urn:li:dataPlatform:postgres,db.t,DEV)",
        ],
    )
    def test_a_malformed_urn_literal_raises_invalid_dataset_urn(self, bad: str) -> None:
        with pytest.raises(InvalidDatasetUrnError):
            check_dataset_urn_literals(parse_filter(f"dataset_urn = '{bad}'"))

    def test_a_malformed_urn_inside_an_in_list_is_caught(self) -> None:
        with pytest.raises(InvalidDatasetUrnError):
            check_dataset_urn_literals(parse_filter(f"dataset_urn IN ('{_URN}', 'not-a-urn')"))

    def test_a_malformed_value_on_another_column_is_not_a_urn_error(self) -> None:
        """Only ``dataset_urn`` literals carry a URN shape — ``origin`` values are
        free text (spec/API.md §``dataset_filter`` grammar — column table)."""
        check_dataset_urn_literals(parse_filter("origin = 'not-a-urn'"))


# ── Injection battery ────────────────────────────────────────────────────────


class TestInjectionBattery:
    """spec/feature/BACKEND.md §Dataset resolution: "Every literal compiles to a
    bound parameter and the column set is the grammar's own whitelist, so user
    filter text never reaches the database as SQL text".

    Each case feeds a hostile payload as a *value* and asserts the payload is
    absent from the rendered SQL and present among the bound parameters. The
    absence check alone would pass vacuously if the value silently vanished, so
    every case also asserts the payload is bound.
    """

    _PAYLOADS = [
        pytest.param("' OR 1=1 --", id="quote-break-or-true"),
        pytest.param("'; DROP TABLE dataset_registry; --", id="stacked-drop-table"),
        pytest.param("PROD'; DELETE FROM metric_definitions WHERE '1'='1", id="stacked-delete"),
        pytest.param("PROD -- trailing comment", id="line-comment"),
        pytest.param("PROD /* block comment */ DEV", id="block-comment"),
        pytest.param("PROD\x00DEV", id="null-byte"),
        pytest.param("PROD\nUNION SELECT password FROM users", id="newline-union"),
        pytest.param("%(evil)s", id="pyformat-placeholder"),
        pytest.param("$1", id="numeric-placeholder"),
        pytest.param("\\'; SELECT 1; --", id="backslash-escape"),
    ]

    @staticmethod
    def _escaped(payload: str) -> str:
        return payload.replace("'", "''")

    @pytest.mark.parametrize("payload", _PAYLOADS)
    def test_equality_payload_is_bound_never_rendered(self, payload: str) -> None:
        sql, params = _compile(f"origin = '{self._escaped(payload)}'")
        assert payload in params.values(), "payload must survive as a bound value"
        assert payload not in sql
        assert "drop" not in sql.lower()
        assert "delete" not in sql.lower()

    @pytest.mark.parametrize("payload", _PAYLOADS)
    def test_in_list_payload_is_bound_never_rendered(self, payload: str) -> None:
        sql, params = _compile(f"origin IN ('PROD', '{self._escaped(payload)}')")
        assert [payload_list for payload_list in params.values()] == [["PROD", payload]]
        assert payload not in sql

    @pytest.mark.parametrize("payload", _PAYLOADS)
    def test_array_membership_payload_is_bound_never_rendered(self, payload: str) -> None:
        sql, params = _compile(f"'{self._escaped(payload)}' IN tag_urns")
        assert [payload_list for payload_list in params.values()] == [[payload]]
        assert payload not in sql

    def test_a_payload_shaped_like_a_bind_placeholder_adds_no_parameter(self) -> None:
        """A value spelled exactly like the driver's own placeholder must not be
        re-interpreted as one: the statement still carries a single parameter,
        holding the payload verbatim."""
        _, params = _compile("origin = '%(param_1)s'")
        assert list(params.values()) == ["%(param_1)s"]

    def test_a_payload_shaped_like_a_column_name_stays_a_value(self) -> None:
        """A value that reads like an identifier is still data — it must not be
        able to redirect the predicate onto another column."""
        sql, params = _compile("origin = 'dataset_registry.tag_urns'")
        assert list(params.values()) == ["dataset_registry.tag_urns"]
        assert "dataset_registry.tag_urns" not in sql
        assert "dataset_registry.origin" in sql

    @pytest.mark.parametrize("payload", _PAYLOADS)
    def test_the_boolean_production_admits_no_payload_at_all(self, payload: str) -> None:
        """The value side of ``bool_col '=' bool`` is the one place the grammar
        accepts an unquoted token, so it is the one place a payload could reach the
        statement as text. It admits exactly two words: anything else — quoted or
        bare — fails to parse, and the two words that do parse are Python constants
        the parser selected rather than anything the request supplied.

        spec/feature/BACKEND.md §Dataset resolution — "user filter text […] never
            reaches the database as SQL text", and the boolean carve-out that makes
            the inline rendering below correct rather than a leak: "**Every
            user-supplied literal compiles to a bound parameter** — the
            ``is_primary`` boolean is the one exception: it is never user text but
            one of two parser-selected Python constants, and it renders inline";
        spec/API.md §``dataset_filter`` grammar — ``bool := TRUE | FALSE``,
            "bare word, never quoted".
        """
        for text in (
            f"is_primary = {payload}",
            f"is_primary = '{payload.replace(chr(39), chr(39) * 2)}'",
        ):
            with pytest.raises(DatasetFilterSyntaxError):
                parse_filter(text)

        sql, params = _compile("is_primary = true")
        assert params == {}, f"the boolean predicate binds nothing; got {params!r}"
        assert sql.strip() == "dataspoke.dataset_registry.is_primary = true", (
            f"the whole rendered predicate must be the column and one constant; got:\n{sql}"
        )

    def test_an_injected_predicate_outside_quotes_is_a_syntax_error(self) -> None:
        """The unquoted form of the same attack does not slip through as text —
        it fails to parse."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = 'PROD' OR 1=1")

    def test_a_semicolon_outside_a_literal_is_a_syntax_error(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = 'PROD'; DROP TABLE dataset_registry")

    def test_a_sql_comment_outside_a_literal_is_a_syntax_error(self) -> None:
        """The grammar has no comment production, so ``--`` is not skipped — it is
        rejected rather than silently truncating the rest of the clause."""
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = 'PROD' -- AND origin = 'DEV'")

    def test_a_block_comment_outside_a_literal_is_a_syntax_error(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            parse_filter("origin = /* sneaky */ 'PROD'")

    def test_sql_keywords_outside_the_grammar_are_rejected(self) -> None:
        for text in [
            "origin = 'PROD' UNION SELECT 1",
            "SELECT * FROM dataset_registry",
            "origin = 'PROD' AND NOT origin = 'DEV'",
        ]:
            with pytest.raises(DatasetFilterSyntaxError):
                parse_filter(text)

    def test_every_literal_of_a_composite_filter_is_bound(self) -> None:
        """A composite clause in the grammar's shape — an origin equality AND-ed with a
        parenthesised tag/glossary-term OR — with no fragment of it reaching the SQL
        text. (The grammar's printed worked example carries a third conjunct,
        ``is_primary = true``; that whole clause is compiled in
        ``test_the_composite_spec_example_parses_and_compiles``.)"""
        sql, params = _compile(
            "origin = 'PROD' AND ('urn:li:tag:area:catalog' IN tag_urns"
            " OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)"
        )
        assert sorted(str(value) for value in params.values()) == sorted(
            ["PROD", str(["urn:li:tag:area:catalog"]), str(["urn:li:glossaryTerm:pii.gdpr"])]
        )
        for literal in ["PROD", "urn:li:tag:area:catalog", "urn:li:glossaryTerm:pii.gdpr"]:
            assert literal not in sql


# ── Canonical formatter ──────────────────────────────────────────────────────


class TestFormatFilter:
    """``format_filter`` is the executable reference for the Auto-indent layout
    spec/feature/FRONTEND_BASIC.md §Shared Component Notes describes: "newline before
    each top-level ``AND`` / ``OR``, indent inside parentheses"."""

    def test_the_empty_filter_formats_to_the_empty_string(self) -> None:
        assert format_filter("") == ""

    def test_a_single_predicate_is_left_on_one_line(self) -> None:
        assert format_filter("origin='PROD'") == "origin = 'PROD'"

    def test_each_top_level_operand_gets_its_own_line_led_by_the_operator(self) -> None:
        formatted = format_filter("origin = 'A' AND origin = 'B' AND origin = 'C'")
        assert formatted.splitlines() == [
            "origin = 'A'",
            "AND origin = 'B'",
            "AND origin = 'C'",
        ]

    def test_a_parenthesised_group_is_indented_one_level(self) -> None:
        formatted = format_filter("origin = 'A' AND (origin = 'B' OR origin = 'C')")
        assert formatted.splitlines() == [
            "origin = 'A'",
            "AND (",
            "    origin = 'B'",
            "    OR origin = 'C'",
            ")",
        ]

    def test_a_boolean_predicate_renders_with_its_bare_lowercase_word(self) -> None:
        """The canonical form keeps the boolean unquoted — a formatter that emitted
        ``is_primary = 'true'`` would produce output the grammar rejects — and prints
        it lowercase.

        Two separate spec facts, since the grammar states ``TRUE``/``FALSE`` are
        case-insensitive and so fixes neither the case of the input nor, by itself,
        the case of the output:

        spec/API.md §``dataset_filter`` grammar — ``bool := TRUE | FALSE``, "bare
            word, never quoted" (the quoting);
        spec/API.md §``dataset_filter`` grammar, worked example — the clause it prints
            reads ``origin = 'PROD' AND is_primary = true AND (…)``, i.e. the
            documented rendering of a boolean predicate is the lowercase word.
        """
        assert format_filter("IS_PRIMARY=TRUE") == "is_primary = true"
        assert format_filter("is_primary =  FALSE") == "is_primary = false"

    def test_a_boolean_predicate_takes_its_own_line_in_a_composition(self) -> None:
        formatted = format_filter(
            "origin = 'PROD' AND is_primary = true AND ('urn:li:tag:pii' IN tag_urns"
            " OR origin = 'DEV')"
        )
        assert formatted.splitlines() == [
            "origin = 'PROD'",
            "AND is_primary = true",
            "AND (",
            "    'urn:li:tag:pii' IN tag_urns",
            "    OR origin = 'DEV'",
            ")",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "origin = 'PROD'",
            "origin IN ('PROD', 'DEV')",
            "'urn:li:tag:area:catalog' IN tag_urns",
            "is_primary = true",
            "is_primary = false",
            "origin = 'PROD' AND origin = 'DEV'",
            "origin = 'A' AND (origin = 'B' OR (origin = 'C' AND origin = 'D'))",
            "origin = 'PROD' AND is_primary = true AND (origin = 'A' OR origin = 'B')",
            "origin = 'O''Brien'",
        ],
    )
    def test_formatting_is_idempotent(self, text: str) -> None:
        once = format_filter(text)
        assert format_filter(once) == once

    @pytest.mark.parametrize(
        "text",
        [
            "origin = 'PROD'",
            "origin IN ('PROD', 'DEV')",
            "'urn:li:tag:area:catalog' IN tag_urns",
            "IS_PRIMARY = FALSE",
            "origin = 'A' AND (origin = 'B' OR (origin = 'C' AND origin = 'D'))",
            "origin = 'PROD' AND is_primary = true",
            "origin = 'O''Brien'",
        ],
    )
    def test_formatting_preserves_meaning(self, text: str) -> None:
        assert parse_filter(format_filter(text)).root == parse_filter(text).root

    def test_formatting_normalises_whitespace_and_keyword_case(self) -> None:
        assert format_filter("ORIGIN   =    'PROD'   and   origin='DEV'").splitlines() == [
            "origin = 'PROD'",
            "AND origin = 'DEV'",
        ]

    def test_an_unparseable_filter_is_not_silently_repaired(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            format_filter("origin = ")
