---
name: e2e-testing-md-citations-resolved
description: RESOLVED (2026-07-26) — TESTING.md gained §E2E §Execution discipline + §Selectors, so the previously-fabricated E2E citations now resolve; keeps the normalised-substring sweep that proves it
metadata:
  type: project
---

**Status: resolved.** As of 2026-07-26 `spec/TESTING.md` carries real
`### Execution discipline` (L575) and `### Selectors` (L563) subsections under
`## End-to-End (E2E) Testing`, plus `### Authentication`, `### Two groups`,
`### Ground group`. Every `spec: TESTING.md §E2E …` citation in
`tests/e2e/**/*.spec.ts` was re-pointed at those and now resolves. The earlier
finding (~20 citations attributing rules the doc never stated —
`critical pitfall`, `API-fired`, `Radix`, `expect.poll`, `describe.serial`,
polling/sleep rules) no longer applies; do not re-raise it from memory.

**Why it matters still:** the *class* of defect recurs — a citation laundering a
rule that is not at the cited location. `§Assertion Principles` (Test Data
Design) remains a narrow three-bullet section (no row counts, no surrogate IDs,
no wall-clock timestamps) and is still the wrong anchor for polling/setup rules.

**How to apply — the sweep that settles it in one shot.** Do not eyeball, and do
not use a heading-match checker (~255 hits, mostly false positives from informal
labels like `USE_CASE_en.md §UC1 Case 1`). Instead:

1. Collect each `spec:` comment block plus its more-indented continuation lines.
2. Pull double-quoted fragments ≥20 chars; split on `…`; keep pieces ≥15 chars.
3. Normalise both fragment and the whole `spec/**/*.md` corpus: collapse
   whitespace/backticks/asterisks, fold em-dash/curly-quote/ellipsis, lowercase.
4. Substring-match each piece against the corpus blob.
5. Run it against `git show HEAD:<path>` too and diff the two unmatched sets —
   that separates *newly introduced* mismatches from pre-existing drift.

On the #86 phase-5 remediation this gave 0 unmatched in added blocks and
HEAD 42 → working-tree 40, i.e. no new mismatches. Spec line-wrapping makes a
plain `grep -F` report false absences; only the normalised form is reliable.

Related: [[spec-conformance-86-anchors]], [[dead-assert-tuple-ruff-blind]],
[[e2e-events-panel-frozen-upper-bound]]
