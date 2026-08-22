---
name: validation-score-cadence-test-seams
description: validation-score counts + cadence-anchored window + dataset_filter negation — final cycle-2 state (F1-F4 closed), the exact orphaned-citation residue left behind, and the phrase-window sweep that found it
metadata:
  type: project
---

Change set: `dataset_filter` `!=`/`NOT IN`, validation conf `attribute`/`parameter`,
`validation-score` recast from a score **sum** to three **counts** with a per-dataset
cadence-anchored window and a service-supplied measurement instant (`scheduled_at`).

**Two review-fix cycles complete.** 3694/3694 unit green, ruff clean, e2e `tsc` +
`--list` clean (170 tests / 41 files), spot+api-wired collect (37). `src/`, `src/frontend/`,
`spec/`, `migrations/` untouched by the cycle-2 fix pass (mtime-verified: fix pass ran
03:07–03:11, all src/spec mtimes ≤ 22:54 the prior day).

**Cycle-2 findings all closed and independently re-verified:**
- F2 (`tests/unit/shared/test_dataset_filter.py`): 5 sites keep type + `position`; the
  kept `"array column"` / `"scalar column"` / `"boolean"` substrings ARE spec-mandated by
  BACKEND.md §Dataset resolution "a parse error naming the kind it actually is".
- F3 (`test_validation_score.py`): every set-equality is paired with its own `len()`, so
  duplicate-collapse is still caught. The load-bearing `lower_bound`/`upper_bound` detail
  assertions in `test_offset_one_shifts_the_window_back_by_one_cadence_unit` survive.
- F4 (`tests/unit/api/routers/spoke/test_validation.py`): 6 key-set pins. Independently
  confirmed complete = `ValidationConfResponse` {dataset_urn, description, variables,
  attribute, parameter?, created_at, updated_at} + `SingleResponse.resp_time`.

**Mutation re-check of `ingestion_freshness.py` (fix pass was docstring-only):** 4/4 killed
— `>= cutoff`→`>`, tier label `observation`→`source_level`, `detail.time_window_sec`+1,
`total`/`ingested_in_time` swap. Assertion logic intact.

**Orphaned-citation residue (8 sites, NON-BLOCKING — docstrings only, zero assertion
impact; every stale quote stays semantically true for the file it sits in):**
- `spot/test_metrics.py:91,193` — USE_CASE "**the** measurement window" (now "the
  measurement window's **width**"). These are F1's own named class, in an F1-named file.
- `test_ingestion_freshness.py:113,1546`, `test_doc_health.py:89`, `spot/test_metrics.py:2734`
  — BACKEND.md §Verdict contract rewrite: "covers **every** dataset in scope" →
  "covers every dataset the measurer **evaluated**"; "Full coverage is what makes" →
  "Covering the passing datasets too is what makes".
- `spot/test_metrics.py:2960` — API.md `unknown` causes went from 2 to 3 (the new
  validation-score no-config cause).
- `tests/unit/shared/test_dataset_filter.py:876` — API.md keyword list gained `NOT`.

**Pre-existing (at HEAD, NOT this change set):** `tests/unit/shared/test_dataset_filter.py:11,1219`
and `tests/unit/backend/test_dataset_filter.py:137` quote "Every literal compiles to a
bound parameter … so user filter text never reaches the database as SQL text"; BACKEND.md:1490
actually reads "**Every literal in the compiled statement is a bound parameter, with one
deliberate exception**". The flattened form also drops the boolean-inline carve-out the
same file tests elsewhere.

**Sweep that found all of this — reuse it.** Full-sentence orphan matching found ZERO;
only overlapping **10-word phrase windows** over *contiguous removed-line runs* of
`git diff HEAD -- spec/` (normalised: collapse backtick runs, NBSP, whitespace), filtered
to phrases absent from the current spec corpus, then substring-searched across
`tests/**/*.py`, `tests/**/*.ts`, `src/frontend/**/*.test.ts*`, surfaced them. A naive
docstring-quote regex produces ~50% false positives (it splits prose at inner `"` and
misses ``…`` elisions) — always fuzzy-confirm with `difflib.find_longest_match` before
calling a citation fabricated. `test_bootstrap.py` was a phrase-window false positive
(the window stitched a live quote to adjacent test prose).

**Frontend hand-off, still open (step 8, fenced out of the test stage):**
`src/frontend/types/governance.ts:19` still maps `validation-score` →
`["total", "validation_score_sum"]`, plus 4 Vitest files. The E2E specs were updated to
`valid_confd`/`valid_in_time` this cycle, so `tests/e2e/use-case/uc5-01-governance.spec.ts`
cannot pass until that map lands.

**Citation traps:** "API.md §Error Catalogue — a malformed request body is a 422" is wrong —
that table gives *Malformed request* **400** (cf. [[validation-invalid-param-422]]).
The BACKEND.md quotes at "Measurers carry no second copy of the bound", "the grammar's own
whitelist", "it pushes the compiled filter clause into a paginated query" and "a `metrics`
descriptor per emitted key" DO exist verbatim — they only fail a naive `grep` because the
sentences wrap mid-phrase.

Related: [[dataset-filter-verdict-test-seams]], [[is-primary-filter-test-seams]],
[[metric-measurement-window-test-seams]], [[feedback-review-method]].
