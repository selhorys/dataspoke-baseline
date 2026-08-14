"""``dataset_filter`` grammar — tokenizer, parser, SQL compiler and formatter.

A ``dataset_filter`` is a SQL ``WHERE``-clause string evaluated against
``dataset_registry``, DataSpoke's local mirror of the DataHub dataset estate.
UC3 ontogen, UC4 metagen and UC5 metrics share this one grammar.

Grammar (spec/API.md §``dataset_filter`` grammar)::

    filter      := ε | expr                     -- empty string = all datasets
    expr        := term { (AND|OR) term }        -- one operator kind per level
    term        := predicate | '(' expr ')'      -- parens nest at most 2 deep
    predicate   := scalar_col '=' string
                 | scalar_col IN '(' string {',' string} ')'
                 | string IN array_col
                 | bool_col '=' bool
    scalar_col  := dataset_urn | origin | platform_urn
    array_col   := tag_urns | glossary_term_urns
    bool_col    := is_primary
    bool        := TRUE | FALSE                  -- bare word, never quoted
    string      := '...'                         -- single quotes; '' escapes one

Keywords (``AND``/``OR``/``IN``), the ``TRUE``/``FALSE`` bare words and column
names are matched case-insensitively; string **values** are case-sensitive. A
quoted boolean (``is_primary = 'true'``) is a syntax error, as is a boolean
column used with ``IN``. Mixing ``AND`` and ``OR`` at one level requires
parentheses. Caps: ≤ 8,000 characters and ≤ 1,000 string literals.

**Security invariant.** User filter text never reaches the database as SQL text.
:func:`filter_clause` compiles the AST to a SQLAlchemy boolean expression in
which every *user-supplied* literal is a bound parameter and *every* column
identifier comes from the fixed whitelists below (``_SCALAR_COLUMNS`` /
``_ARRAY_COLUMNS`` / ``_BOOL_COLUMNS``), which map grammar keywords to ORM
column objects. A boolean predicate is the one exception to the binding rule and
deliberately so: it compiles to the inline constant ``= true`` / ``= false``,
whose operand is one of two Python constants the parser selected — never
operator text — and whose spelling is what keeps the partial index reachable.
This module contains no f-string, ``%``-formatting, ``str.format`` or
``sqlalchemy.text()`` on the compile path — the parser is the only thing between
an operator's input and a query.

Spec: spec/API.md §``dataset_filter`` grammar, spec/feature/BACKEND.md
§Dataset resolution, spec/feature/BACKEND_SCHEMA.md §``dataset_registry``.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

# The dataset-URN shape lives with the other URN helpers; re-exported here so a
# reader of the grammar sees what a `dataset_urn` literal must look like.
from src.shared.datahub.urn import DATASET_URN_RE
from src.shared.db.models import DatasetRegistry
from src.shared.exceptions import DataSpokeError, InvalidDatasetUrnError

__all__ = [
    "DATASET_URN_RE",
    "MAX_FILTER_CHARS",
    "MAX_PAREN_DEPTH",
    "MAX_STRING_LITERALS",
    "ArrayContains",
    "BoolEquals",
    "BoolNode",
    "DatasetFilterSyntaxError",
    "Equals",
    "FilterAst",
    "InList",
    "check_dataset_urn_literals",
    "filter_clause",
    "format_filter",
    "literal_dataset_urns",
    "parse_filter",
]

# ── Caps (spec/API.md §dataset_filter grammar — Caps) ─────────────────────────

MAX_FILTER_CHARS = 8_000
MAX_STRING_LITERALS = 1_000
#: Parenthesised nesting levels admitted. The unparenthesised top level is depth
#: 0, so ``a AND (b OR (c AND d))`` (depth 2) parses and a third level does not.
MAX_PAREN_DEPTH = 2

# Column whitelists. These are the ONLY identifiers the compiler can emit, and
# they are ORM column objects rather than strings, so no user-supplied text can
# become a column reference.
_SCALAR_COLUMNS: dict[str, InstrumentedAttribute[Any]] = {
    "dataset_urn": DatasetRegistry.dataset_urn,
    "origin": DatasetRegistry.origin,
    "platform_urn": DatasetRegistry.platform_urn,
}
_ARRAY_COLUMNS: dict[str, InstrumentedAttribute[Any]] = {
    "tag_urns": DatasetRegistry.tag_urns,
    "glossary_term_urns": DatasetRegistry.glossary_term_urns,
}
_BOOL_COLUMNS: dict[str, InstrumentedAttribute[Any]] = {
    "is_primary": DatasetRegistry.is_primary,
}

#: The two bare words a boolean predicate accepts, matched case-insensitively.
#: The parser maps the word to a Python constant here, so the compiler never
#: sees operator text on the value side of a boolean predicate.
_BOOL_LITERALS: dict[str, bool] = {"true": True, "false": False}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Longest untrusted fragment quoted back in a syntax-error message.
_ERROR_FRAGMENT_MAX = 64


class DatasetFilterSyntaxError(DataSpokeError):
    """A ``dataset_filter`` string does not parse, or breaches a cap.

    Deliberately **not** a ``ValueError``: Pydantic v2 re-raises non-``ValueError``
    exceptions out of a validator unchanged, which is what lets a schema-layer
    check surface as ``422 INVALID_DATASET_FILTER`` carrying the character
    position rather than being folded into a generic ``INVALID_PARAMETER``
    envelope (spec/API.md §Error Catalogue).
    """

    error_code: str = "INVALID_DATASET_FILTER"

    def __init__(self, message: str, position: int) -> None:
        self.position = position
        self.detail: dict[str, Any] = {"position": position}
        super().__init__(f"{message} (at character {position})")


# ── AST ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Equals:
    """``scalar_col = 'value'``."""

    column: str
    value: str


@dataclass(frozen=True)
class InList:
    """``scalar_col IN ('a', 'b')``."""

    column: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ArrayContains:
    """``'value' IN array_col``."""

    column: str
    value: str


@dataclass(frozen=True)
class BoolEquals:
    """``bool_col = TRUE`` / ``bool_col = FALSE``.

    ``value`` is a Python ``bool`` the parser derived from the bare word, not the
    operator's text — the quoted form (``is_primary = 'true'``) never parses.
    """

    column: str
    value: bool


@dataclass(frozen=True)
class BoolNode:
    """``AND``/``OR`` over two or more operands at one nesting level."""

    op: Literal["AND", "OR"]
    children: tuple["Node", ...]


Node = Equals | InList | ArrayContains | BoolEquals | BoolNode


@dataclass(frozen=True)
class FilterAst:
    """A parsed filter. ``root is None`` for the empty (match-everything) filter."""

    root: Node | None

    @property
    def is_empty(self) -> bool:
        return self.root is None


# ── Tokenizer ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Token:
    kind: str  # "ident" | "string" | "(" | ")" | "," | "=" | "eof"
    value: str
    position: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    literals = 0

    while i < n:
        ch = text[i]

        if ch.isspace():
            i += 1
            continue

        if ch in "(),=":
            tokens.append(_Token(ch, ch, i))
            i += 1
            continue

        if ch == "'":
            start = i
            i += 1
            chunks: list[str] = []
            while True:
                if i >= n:
                    raise DatasetFilterSyntaxError("unterminated string literal", start)
                c = text[i]
                if c == "'":
                    # '' escapes a single quote; a lone quote closes the literal.
                    if i + 1 < n and text[i + 1] == "'":
                        chunks.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                chunks.append(c)
                i += 1
            literals += 1
            if literals > MAX_STRING_LITERALS:
                raise DatasetFilterSyntaxError(
                    f"filter carries more than {MAX_STRING_LITERALS} string literals",
                    start,
                )
            tokens.append(_Token("string", "".join(chunks), start))
            continue

        match = _IDENT_RE.match(text, i)
        if match:
            tokens.append(_Token("ident", match.group(0), i))
            i = match.end()
            continue

        raise DatasetFilterSyntaxError(f"unexpected character {_safe(ch)}", i)

    tokens.append(_Token("eof", "", n))
    return tokens


def _safe(value: str) -> str:
    """Render an untrusted fragment for an error message.

    Control characters are stripped so a value cannot forge log or envelope
    structure, and the fragment is truncated: an identifier may run to the
    8,000-character filter cap, and quoting it whole would let a bad request
    dictate the size of its own 422 body.
    """
    scrubbed = _CTRL_RE.sub("?", value)
    if len(scrubbed) > _ERROR_FRAGMENT_MAX:
        scrubbed = scrubbed[:_ERROR_FRAGMENT_MAX] + "…"
    return repr(scrubbed)


# ── Parser ────────────────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    # -- token helpers --
    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _next(self) -> _Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _at_keyword(self, keyword: str) -> bool:
        token = self._peek()
        return token.kind == "ident" and token.value.lower() == keyword

    def _expect(self, kind: str, what: str) -> _Token:
        token = self._peek()
        if token.kind != kind:
            raise DatasetFilterSyntaxError(f"expected {what}", token.position)
        return self._next()

    # -- grammar --
    def parse(self) -> FilterAst:
        if self._peek().kind == "eof":
            return FilterAst(root=None)
        node = self._parse_expr(depth=0)
        trailing = self._peek()
        if trailing.kind != "eof":
            raise DatasetFilterSyntaxError("expected AND, OR or end of filter", trailing.position)
        return FilterAst(root=node)

    def _parse_expr(self, depth: int) -> Node:
        children = [self._parse_term(depth)]
        op: Literal["AND", "OR"] | None = None

        while self._at_keyword("and") or self._at_keyword("or"):
            token = self._next()
            keyword: Literal["AND", "OR"] = "AND" if token.value.lower() == "and" else "OR"
            if op is None:
                op = keyword
            elif op != keyword:
                raise DatasetFilterSyntaxError(
                    "AND and OR cannot be mixed at the same level; use parentheses",
                    token.position,
                )
            children.append(self._parse_term(depth))

        if op is None:
            return children[0]
        return BoolNode(op=op, children=tuple(children))

    def _parse_term(self, depth: int) -> Node:
        token = self._peek()
        if token.kind == "(":
            if depth + 1 > MAX_PAREN_DEPTH:
                raise DatasetFilterSyntaxError(
                    f"parentheses nest deeper than {MAX_PAREN_DEPTH} levels",
                    token.position,
                )
            self._next()
            node = self._parse_expr(depth + 1)
            self._expect(")", "a closing parenthesis")
            return node
        return self._parse_predicate()

    def _parse_predicate(self) -> Node:
        token = self._peek()

        if token.kind == "string":
            # string IN array_col
            self._next()
            if not self._at_keyword("in"):
                raise DatasetFilterSyntaxError(
                    "expected IN after a string literal", self._peek().position
                )
            self._next()
            column_token = self._expect("ident", "an array column name")
            column = column_token.value.lower()
            if column not in _ARRAY_COLUMNS:
                if column in _SCALAR_COLUMNS:
                    raise DatasetFilterSyntaxError(
                        f"column {_safe(column_token.value)} is a scalar column, not an "
                        f"array column; write it as \"<column> = 'value'\"; "
                        f"array columns are {_column_list(_ARRAY_COLUMNS)}",
                        column_token.position,
                    )
                if column in _BOOL_COLUMNS:
                    raise DatasetFilterSyntaxError(
                        f"column {_safe(column_token.value)} is a boolean column, not an "
                        'array column; write it as "<column> = TRUE"; '
                        f"array columns are {_column_list(_ARRAY_COLUMNS)}",
                        column_token.position,
                    )
                raise DatasetFilterSyntaxError(
                    f"unknown array column {_safe(column_token.value)}; "
                    f"array columns are {_column_list(_ARRAY_COLUMNS)}",
                    column_token.position,
                )
            return ArrayContains(column=column, value=token.value)

        if token.kind == "ident":
            column_token = self._next()
            column = column_token.value.lower()
            if column in _BOOL_COLUMNS:
                return self._parse_bool_predicate(column)
            if column not in _SCALAR_COLUMNS:
                if column in _ARRAY_COLUMNS:
                    raise DatasetFilterSyntaxError(
                        f"column {_safe(column_token.value)} is an array column; "
                        "write it as \"'value' IN <column>\"",
                        column_token.position,
                    )
                # The array columns are deliberately not listed here: they are
                # named by the wrong-kind branch above, and every clause added to
                # this message eats into the bound that keeps a 422 body's size
                # out of the requester's control (see :func:`_safe`).
                raise DatasetFilterSyntaxError(
                    f"unknown column {_safe(column_token.value)}; "
                    f"scalar columns are {_column_list(_SCALAR_COLUMNS)}, "
                    f"boolean columns are {_column_list(_BOOL_COLUMNS)}",
                    column_token.position,
                )

            nxt = self._peek()
            if nxt.kind == "=":
                self._next()
                value_token = self._expect("string", "a quoted string value")
                return Equals(column=column, value=value_token.value)
            if nxt.kind == "ident" and nxt.value.lower() == "in":
                self._next()
                self._expect("(", "an opening parenthesis after IN")
                values: list[str] = [self._expect("string", "a quoted string value").value]
                while self._peek().kind == ",":
                    self._next()
                    values.append(self._expect("string", "a quoted string value").value)
                self._expect(")", "a closing parenthesis")
                return InList(column=column, values=tuple(values))
            raise DatasetFilterSyntaxError("expected = or IN after a column name", nxt.position)

        raise DatasetFilterSyntaxError("expected a column name or a quoted string", token.position)

    def _parse_bool_predicate(self, column: str) -> Node:
        """``bool_col '=' bool`` — the column token is already consumed.

        The value is a bare ``TRUE``/``FALSE`` word, matched case-insensitively.
        A quoted value is rejected rather than coerced: ``is_primary = 'true'``
        would otherwise have to mean either the boolean or the string ``'true'``,
        and a silent choice between them is a filter that quietly matches the
        wrong set of datasets.
        """
        nxt = self._peek()
        if nxt.kind != "=":
            raise DatasetFilterSyntaxError(
                "expected = after a boolean column; boolean columns take neither IN "
                'nor a quoted value, only "<column> = TRUE" or "<column> = FALSE"',
                nxt.position,
            )
        self._next()

        value_token = self._peek()
        if value_token.kind == "string":
            raise DatasetFilterSyntaxError(
                "expected TRUE or FALSE after a boolean column; the value is a bare "
                "word, never quoted",
                value_token.position,
            )
        if value_token.kind != "ident" or value_token.value.lower() not in _BOOL_LITERALS:
            raise DatasetFilterSyntaxError(
                "expected TRUE or FALSE after a boolean column", value_token.position
            )
        self._next()
        return BoolEquals(column=column, value=_BOOL_LITERALS[value_token.value.lower()])


def _column_list(columns: dict[str, Any]) -> str:
    return ", ".join(sorted(columns))


def parse_filter(text: str | None) -> FilterAst:
    """Parse *text* into a :class:`FilterAst`.

    ``None``, ``""`` and whitespace-only text all parse to the empty filter,
    which matches every registered dataset.

    Raises:
        DatasetFilterSyntaxError: on a syntax error, an unknown column, a
            breached cap, or a non-string input. The exception carries the
            0-based character position.
    """
    source = text or ""
    if not isinstance(source, str):
        # Every caller is typed to pass a string, so this is unreachable today;
        # it stays as a boundary check because the alternative failure — the
        # tokenizer indexing a non-string — is a 500 where the contract says 422.
        raise DatasetFilterSyntaxError("filter must be a string", 0)
    if len(source) > MAX_FILTER_CHARS:
        raise DatasetFilterSyntaxError(
            f"filter text exceeds {MAX_FILTER_CHARS} characters", MAX_FILTER_CHARS
        )
    return _Parser(_tokenize(source)).parse()


# ── Literal inspection ────────────────────────────────────────────────────────


def literal_dataset_urns(ast: FilterAst) -> list[str]:
    """Return the ``dataset_urn`` literals the filter names, in source order.

    These feed the run-complete event's ``unresolved_urns``: a URN the operator
    named explicitly that matches no registered dataset is reported back.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Node) -> None:
        if isinstance(node, BoolNode):
            for child in node.children:
                walk(child)
        elif isinstance(node, Equals):
            if node.column == "dataset_urn" and node.value not in seen:
                seen.add(node.value)
                found.append(node.value)
        elif isinstance(node, InList):
            if node.column == "dataset_urn":
                for value in node.values:
                    if value not in seen:
                        seen.add(value)
                        found.append(value)

    if ast.root is not None:
        walk(ast.root)
    return found


def check_dataset_urn_literals(ast: FilterAst) -> None:
    """Raise :class:`InvalidDatasetUrnError` for a malformed ``dataset_urn`` literal.

    Surfaces as ``422 INVALID_DATASET_URN`` (spec/API.md §Error Catalogue).
    """
    for urn in literal_dataset_urns(ast):
        if not DATASET_URN_RE.match(urn):
            raise InvalidDatasetUrnError(urn)


# ── SQL compilation ───────────────────────────────────────────────────────────


def filter_clause(ast: FilterAst) -> ColumnElement[bool]:
    """Compile *ast* to a SQLAlchemy boolean expression over ``dataset_registry``.

    The empty filter compiles to ``TRUE`` — it matches every registered dataset;
    the caller supplies the ``datahub_registered`` restriction.

    Every *user-supplied* literal becomes a bound parameter and every column
    identifier comes from the module's whitelists, so no user text is ever
    rendered into SQL. The boolean operand is the sole inline constant: it is one
    of two Python constants the parser selected, rendered as ``= true`` /
    ``= false`` so the partial index on ``is_primary`` stays reachable.
    """
    if ast.root is None:
        return sa.true()
    return _compile(ast.root)


def _compile(node: Node) -> ColumnElement[bool]:
    if isinstance(node, BoolNode):
        parts = [_compile(child) for child in node.children]
        return sa.and_(*parts) if node.op == "AND" else sa.or_(*parts)

    if isinstance(node, Equals):
        column = _SCALAR_COLUMNS[node.column]
        # `sa.literal(...)` is a BindParameter — the value travels out-of-band.
        return column == sa.literal(node.value, Text())

    if isinstance(node, InList):
        column = _SCALAR_COLUMNS[node.column]
        # `col = ANY(:p::TEXT[])` rather than `col IN (:p1, :p2, …)`: the whole
        # list travels as ONE bound array parameter. An IN-list binds one
        # parameter per literal, and callers compile several filters into one
        # statement (the catalog page ORs every enabled metagen conf), so a
        # per-literal shape walks into the PostgreSQL wire protocol's
        # 32,767-parameter ceiling at a few dozen dense filters. PostgreSQL plans
        # `= ANY(array)` as an indexable ScalarArrayOpExpr, so the btree indexes
        # on `origin` / `platform_urn` still serve it. asyncpg wants a Python
        # list here, not the `"{a,b}"` string form psycopg tolerates.
        return column == sa.any_(
            sa.bindparam(None, value=list(node.values), type_=PG_ARRAY(Text()), unique=True)
        )

    if isinstance(node, BoolEquals):
        bool_column = _BOOL_COLUMNS[node.column]
        # `= true` / `= false` rather than `= :p` or `IS true` / `IS false`. The
        # operand is one of two Python constants the parser chose, so nothing of
        # the operator's text reaches the statement either way, but the spelling
        # decides whether `ix_dataset_registry_not_primary` (partial,
        # `WHERE NOT is_primary`) is reachable: PostgreSQL folds `= false` into
        # `NOT col`, which matches the stored index predicate, while a
        # `BooleanTest` node (`IS false`) never does and seq-scans the registry.
        # A bound parameter would match only under a custom plan and lose the
        # index under `plan_cache_mode = force_generic_plan`.
        return bool_column == (sa.true() if node.value else sa.false())

    if isinstance(node, ArrayContains):
        column = _ARRAY_COLUMNS[node.column]
        # `@>` (array containment) is the GIN index's access path. The right
        # operand binds as a one-element TEXT[] parameter: asyncpg wants a Python
        # list here and rejects the `"{a,b}"` string form psycopg tolerates.
        return column.contains(
            sa.bindparam(None, value=[node.value], type_=PG_ARRAY(Text()), unique=True)
        )

    raise AssertionError(f"unhandled filter node: {type(node).__name__}")


# ── Canonical formatter ───────────────────────────────────────────────────────

_INDENT = "    "


def format_filter(text: str | None) -> str:
    """Return *text* re-rendered in canonical indentation.

    One operand per line, the operator leading each continuation line, and a
    parenthesised group opened at the end of its line with its body indented one
    level. Redundant parentheses around a single predicate are dropped.

    **No backend route calls this, by design.** It is the executable reference
    for the layout the frontend's Auto-indent button produces — that formatter is
    lexical TypeScript (`src/frontend/lib/dataset-filter-format.ts`) with no
    grammar knowledge, so this function is what its expected output is pinned
    against. Read it as a specification of the canonical form, not as dead code.

    Raises:
        DatasetFilterSyntaxError: when *text* does not parse.
    """
    ast = parse_filter(text)
    if ast.root is None:
        return ""
    return _format_node(ast.root, 0)


def _format_node(node: Node, indent: int) -> str:
    pad = _INDENT * indent
    if isinstance(node, BoolNode):
        lines: list[str] = []
        for position, child in enumerate(node.children):
            prefix = "" if position == 0 else f"{node.op} "
            lines.append(pad + prefix + _format_operand(child, indent))
        return "\n".join(lines)
    return pad + _format_predicate(node)


def _format_operand(node: Node, indent: int) -> str:
    """Render one operand, opening a parenthesised block for a nested group."""
    if isinstance(node, BoolNode):
        body = _format_node(node, indent + 1)
        return "(\n" + body + "\n" + _INDENT * indent + ")"
    return _format_predicate(node)


def _format_predicate(node: Node) -> str:
    if isinstance(node, Equals):
        return f"{node.column} = {_quote(node.value)}"
    if isinstance(node, InList):
        rendered = ", ".join(_quote(value) for value in node.values)
        return f"{node.column} IN ({rendered})"
    if isinstance(node, ArrayContains):
        return f"{_quote(node.value)} IN {node.column}"
    if isinstance(node, BoolEquals):
        # Lowercase in the canonical form, matching the lowercase column names.
        rendered = "true" if node.value else "false"
        return f"{node.column} = {rendered}"
    raise AssertionError(f"unhandled filter node: {type(node).__name__}")


def _quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"
