---
name: grammar-mirror-has-no-guard
description: dataset-filter-guide.tsx hand-copies spec/API.md's grammar fence and a formatter-canonical EXAMPLE, and NO test asserts anything inside it — diff the mirror yourself on every grammar change
metadata:
  type: feedback
---

`src/frontend/components/dataset-filter-guide.tsx` renders the `dataset_filter` grammar as a
hand-copied string block that must track `spec/API.md` §`dataset_filter`. Nothing enforces that:
its only coverage is `dataset-filter-editor.test.tsx`, which asserts the collapsed
`<summary>` text ("Filter grammar") and never opens it. Every grammar change ships the guide
with zero assertions.

**Why:** two grammar additions have now landed (SQL `dataset_filter`, then `is_primary`), and each
time the guide's *prose* — not the grammar block — went stale: a blanket "values are
case-sensitive" sentence three paragraphs below the new production silently contradicted it.
Greps for the new identifier find the grammar line and miss the falsified sentence
(see [[grep-old-rule-prose-in-consumers]]).

**How to apply:** on any `dataset_filter` change, do three mechanical checks rather than reading:
1. Extract the guide's `GRAMMAR` string literals, join them, and diff against the fenced grammar
   in `spec/API.md` — they are byte-identical today except the `term` line's `(see below)`.
2. Read the *whole* component for prose that the new production falsifies, not just the changed
   lines.
3. Assert the `EXAMPLE` const is formatter-canonical: `formatDatasetFilter(EXAMPLE) === EXAMPLE`
   AND the flat one-line form formats to it — recipe in [[filter-formatter-parity]].
Also check whether the test stage's plan table actually lists a guide spec; twice it has not.
