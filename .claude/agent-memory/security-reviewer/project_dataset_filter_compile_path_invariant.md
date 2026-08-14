---
name: dataset-filter-compile-path-invariant
description: How to actually prove src/shared/dataset_filter.py's "no user text reaches SQL" invariant when a grammar production is added — reachability + closed-vocabulary fuzz, not grep — plus the two bounds a new column class silently eats into
metadata:
  type: project
---

`src/shared/dataset_filter.py` is the operator-supplied SQL `WHERE` grammar over
`dataset_registry` (UC3 ontogen / UC4 metagen / UC5 metric scope). Its stated
invariant — no f-string, `%`, `str.format` or `sqlalchemy.text()` **on the
compile path** — is easy to eyeball-approve wrongly, because the module is full
of f-strings that are *fine* (error messages and `format_filter`). Grep proves
nothing; there are ~19 f-strings in the module and exactly one is on the path.

**Three checks that actually settle it** (all offline, no cluster):

1. **Reachability, not grep.** Walk the call graph from `filter_clause` with
   `ast`. It reaches only `_compile`. The single f-string there is
   `raise AssertionError(f"unhandled filter node: {type(node).__name__}")` — a
   type name, never SQL. Everything else (`_tokenize`, `_Parser.*`,
   `_format_*`, `_quote`) is off-path.
2. **Closed-vocabulary fuzz.** Generate ~200k strings from a token bag
   (`;`, `--`, `' OR 1=1`, `/*`, `\x00`, `%s`, `{0}`, real keywords), keep the
   ones that parse (~6k), compile each with `postgresql.dialect()`, and regex
   the rendered SQL against a whitelist of `dataspoke.dataset_registry.<col>`,
   `%(param_N)s`, `::TEXT[]`, `ANY`, `AND/OR/NOT`, `true/false`, `@>`, `=`,
   parens, whitespace. Then separately assert no user byte appears verbatim.
   Result on the `is_primary` addition: 5,808 compiled, 0 vocabulary
   violations, 0 leaks.
3. **Hand-built AST battery.** Construct the dataclass nodes directly with
   hostile values — that is *not* a reachable path (nothing in `src/`
   constructs an AST; every `filter_clause` call site re-parses first, incl.
   `src/backend/_dataset_filter.py:60` which re-parses text read back out of
   the DB), but it shows whether the compiler would leak if it ever were.

**Why the boolean's inline-constant exception is sound** (it is the one
deliberate break in "every literal is a bound param" — `= true` / `= false`
instead of `= :p`, to keep the partial index `WHERE NOT is_primary` reachable):
`_compile` branches on truthiness to `sa.true()`/`sa.false()` and **never
stringifies `node.value`**, so even `BoolEquals(value="' OR 1=1 --")` renders
`is_primary = true`. `_BOOL_COLUMNS[node.column]` KeyErrors on any other
column. Sole construction site is the `_BOOL_LITERALS[...]` lookup in
`_parse_bool_predicate`. Check *that chain*, not the rendered SQL.

`_IDENT_RE` is `[A-Za-z_][A-Za-z0-9_]*`, explicit ASCII ranges, so
`token.value.lower()` cannot do Turkish-İ / Kelvin-sign tricks into a
whitelist key.

**Two bounds a new column class eats into, both easy to miss:**

1. `tests/unit/shared/test_dataset_filter.py::test_the_message_does_not_echo_an_unbounded_fragment`
   caps a 422 body at **< 200 chars**. Measured worst case over a fuzz of all
   message shapes is **185** (unknown-column, which now lists both the scalar
   and boolean whitelists). ~15 chars of headroom; a second boolean column or a
   longer name breaches it. That test is the only guard.
2. `_safe()` truncates at 64 then `repr()`s — measured max output 67 chars.
   Safe today *only* because `_IDENT_RE` admits no backslash or non-ASCII; a
   widened ident charset would let `repr()` expand the fragment past the cap.

**The 422 does not double-echo.** `DatasetFilterSyntaxError` is deliberately
not a `ValueError`, so Pydantic v2 re-raises it unchanged and it lands on
`_handle_invalid_dataset_filter` (`str(exc)` + `detail={"position": N}`) rather
than on `RequestValidationError`, whose envelope *would* carry the raw `input`.
Verify by instantiating the real request model, not by reading the handler —
see [[api-422-echoes-rejected-input]].

**Degrade direction is a design choice, not an accident:** `_association_urns`
degrades a malformed tags payload to `[]` (narrows a filter), while
`_sibling_is_primary` degrades to `True` (widens it). Measured: `is_primary`
comes back `False` **only** when `isPrimary` is a literal Python `bool` False —
`0`, `""`, `"false"`, `[]` all read `True` — so no malformed payload can drop a
dataset *out* of an `is_primary = true` filter, and for UC4 metagen that filter
is a write scope. The widening direction gains an attacker nothing a legitimate
`siblings.primary = true` write does not already give.

Related: [[api-422-echoes-rejected-input]], [[recipe-regex-trust-boundary]],
[[consumer-db-plane-to-wire-boundary]]
