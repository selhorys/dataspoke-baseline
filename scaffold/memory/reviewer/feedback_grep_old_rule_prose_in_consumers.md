---
name: grep-old-rule-prose-in-consumers
description: When a generator deletes a resolution/precedence rule, grep the OLD rule's prose across non-test consumers AND user-visible UI strings — generators update the implementing module and leave stale doc comments and form hints asserting the dead rule
metadata:
  type: feedback
---

When a stage removes, flips, **or adds an exception to** a rule (precedence
order, fallback chain, source-of-truth plane, grammar production), do not stop at
verifying the implementing module.
Grep the **prose of the old rule** across every non-test consumer, every shell
helper that documents the same wiring, and every **user-visible UI string**
(`hint` / `description` / `label` / `placeholder` props).

**Why:** generators reliably rewrite the doc comment on the module they edit and
reliably miss the copies. Two cases:

- Issue #78 (`env-first` → API-only for the DataHub/Langfuse display links): the
  hook, the runtime-config interface, the layout, and the chart were all correct,
  but four consumers plus one shell helper still carried `env-first, then GET
  /spoke/common/peripheral-links`.
- The metric time-window fix (per-dataset derived window → uniform
  `metric_conf.time_window_sec`): backend, `API.md`, and `USE_CASE_en.md` all
  landed the new semantics, but
  `src/frontend/components/governance/metric-form.tsx` still rendered
  `hint="Fallback freshness window in seconds"` on the very input that sets the
  value. That one is worse than a comment — `Field` renders `hint` in the browser
  whenever there is no error, so the UI *teaches the user* the mental model the
  fix exists to eliminate, on a form shared by two metric types ("freshness" was
  also wrong for `validation-score`).

- The `is_primary` dataset_filter column: adding one grammar production
  (`bool := TRUE | FALSE`, case-insensitive per `API.md`) silently falsified a
  *universal* sentence sitting three paragraphs below it in the very same
  rendered component — `dataset-filter-guide.tsx` still tells the user "values
  are case-sensitive". Nothing was removed or flipped, so the usual
  removed-identifier grep finds nothing; the trigger was an **addition** that
  created an exception to a blanket claim.

**How to apply:** grep the distinctive phrase from the old rule (`fallback`,
`env-first`, `then`, `precedence`, `wins`, `per-dataset`, the removed env-var or
config names) filtered to non-test sources — and for an *addition*, re-read the
surrounding prose of the file you edited for blanket words (`always`, `never`,
`all`, `only`, `values are …`) that the new case now contradicts, e.g.
`grep -rni "fallback\|per-dataset" --include=*.ts --include=*.tsx src/frontend/ | grep -v '\.test\.'`
plus the same phrase over `src/` and `helm-charts/bin/`. A zero-hit grep on
*identifiers* (the generator's usual evidence) says nothing about prose. When the
stale prose is a rendered string, report it at medium even if the file sits
outside the generator's declared file list — name it as scope-adjacent so the
orchestrator can route it. Related: [[no-references-remain-brace-grep]],
[[verify-generator-dead-code-claims]].
