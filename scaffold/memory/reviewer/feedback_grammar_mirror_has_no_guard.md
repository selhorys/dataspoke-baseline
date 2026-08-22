---
name: grammar-mirror-has-no-guard
description: The dataset_filter grammar is hand-copied into FOUR places (spec/API.md, the module docstring, DATASET_FILTER_FIELD_DESCRIPTION's OpenAPI text, dataset-filter-guide.tsx) and only one has any test — diff all four on every grammar change
metadata:
  type: feedback
---

The `dataset_filter` grammar exists in four hand-maintained copies. Diff every one of them on any
grammar change; nothing in CI compares them.

| Copy | What it is | Coverage |
|---|---|---|
| `spec/API.md` §`dataset_filter` | the authority | — |
| `src/shared/dataset_filter.py` module docstring | EBNF block | none (byte-identical today; a python `difflib` of the fenced block vs the docstring lines is a 10-line check) |
| `src/api/schemas/_dataset_filter.py` `DATASET_FILTER_FIELD_DESCRIPTION` | **user-facing OpenAPI text**, imported by ontogen + metagen + metrics request schemas | none |
| `src/frontend/components/dataset-filter-guide.tsx` | rendered `GRAMMAR` + `EXAMPLE` | only the collapsed `<summary>` text |

**Why:** three grammar additions have now landed (SQL `dataset_filter`, `is_primary`, then
`!=` / `NOT IN`). Each time a copy went stale in prose rather than in the grammar line, so greps
for the new identifier find nothing (see [[grep-old-rule-prose-in-consumers]]). On the `!=` /
`NOT IN` pass the module docstring and the EBNF were perfect while
`DATASET_FILTER_FIELD_DESCRIPTION` still published "scalar, '=' and IN", "array,
\"'value' IN column\"" and "AND/OR/IN/TRUE/FALSE are case-insensitive" on three request bodies —
the API documenting a grammar its parser had outgrown.

**How to apply:** four mechanical checks, not a read-through:
1. `difflib` the `spec/API.md` fenced block against the module docstring's EBNF lines.
2. Read `DATASET_FILTER_FIELD_DESCRIPTION` in full — it enumerates operators *and* the
   case-insensitive keyword list, so a new keyword (`NOT`) falsifies two clauses.
3. Grep the parser's error-hint strings (`write it as "'value' IN <column>"`) — they name the
   admissible spellings and go stale the same way.
4. Extract the guide's `GRAMMAR` literals and diff; read the whole component for falsified prose;
   assert `EXAMPLE` is formatter-canonical — recipe in [[filter-formatter-parity]].
   On the `!=` / `NOT IN` frontend pass this is exactly where it bit: the guide's `GRAMMAR`
   block diffed clean against API.md (only the pre-existing "(see below)" trim), while the new
   prose paragraph read "Both work on the scalar and array columns" — distributing `!=` over
   the array columns, which the parser rejects (`_parse_predicate` routes an array column to
   `"'value' IN <column>"`). API.md's own sentence, "Negation is available on the scalar and
   array columns only", is true of negation jointly and false distributed. Rephrasing an
   API.md sentence is the drift mechanism — diff the *claim*, not the wording. The fix pass
   distributed it correctly ("`!=` applies to the scalar columns; `NOT IN` to the scalar
   and array columns; the boolean column takes `=` alone") — but nothing pins it:
   `dataset-filter-guide.test.tsx` asserts the 12 `GRAMMAR` lines and the `<summary>`
   label and **nothing in the prose paragraphs**, so a green Vitest run is no evidence
   here and every future keyword change re-opens the same hole.
Also check whether the test stage's plan table actually lists a guide spec; twice it has not.
