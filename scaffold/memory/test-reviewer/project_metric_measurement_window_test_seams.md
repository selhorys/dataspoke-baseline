---
name: metric-measurement-window-test-seams
description: metric time-window test seams — the mutation harness, the 10 mutants and their sole killers, the residual impl-pinned bits (message string, OverflowError), and the now-resolved USE_CASE "positive int" citation form
metadata:
  type: project
---

`ingestion-freshness` / `validation-score` apply `metric_conf.time_window_sec`
(BACKEND.md §Metrics Service — Measurement window / **Boundary is inclusive** /
**Window bounds**; those are bold-run labels, not markdown headings — citations
naming them as `§…` are legitimate).

**Mutation harness worth rebuilding** — `cp` the impl file to the scratchpad,
`str.replace` with a `count(pattern) == 1` assert, run, restore from the copy in a
`trap … EXIT`, then `diff -q` the restore. Never `git checkout` a file under review —
it is uncommitted. (See [[no-destructive-git-during-review]].)

**Mutation table — cycle-2 measured, 10/10 killed:**

| mutant | killed by |
|---|---|
| `src/shared/metric_conf.py` MAX → `1_000_000` | `tests/unit/shared/test_metric_conf.py` (the one Python place the literal is written out) |
| `src/frontend/types/governance.ts` MAX → `1_000_000` | `metric-form.schema.test.ts` "declares the ceiling the spec names" |
| freshness `cutoff = now - (window_sec + 1)` | `test_event_one_second_outside_window_is_stale` (sole) |
| `1 <=` → `0 <=` | `test_time_window_of_zero_is_rejected` + `test_patch_metric_conf_zero_window_raises` |
| `>=` → `>` on **each** measurer | **exactly one test each** — `test_event_exactly_at_cutoff_is_fresh`, `test_row_exactly_at_cutoff_is_counted`. Relaxing either exact-instant pin to ±1s makes its mutant immortal; the ±1s neighbours added in cycle 2 do **not** cover it |
| measurers `min(window, 315_360_000)` (own copy of the bound + silent clamp) | `test_an_out_of_range_stored_window_fails_the_run_rather_than_being_clamped` (both measurer files) |
| PATCH window check gated on `"metric_conf" in patch` | `test_patch_of_another_field_alone_still_rejects_an_out_of_range_stored_window` (sole) |
| patched `metric_conf` never `setattr`'d onto the row | 7 service tests incl. the repair test |
| frontend `METRIC_TIME_WINDOW_SEC_MIN` 1 → 0 | pre-existing Vitest "fails when time_window_sec is 0" |

**Residual impl-pinning (accepted, low):**
- `test_rejection_message_names_the_closed_interval_and_the_metric_type` asserts the
  *exact* sentence; spec fixes the code (`422 INVALID_PARAMETER`) and the interval,
  not the wording. Only the interval half is spec-derived.
- The F5 measurer tests pin `OverflowError` (they pass `10**20`, past `timedelta`'s
  range). Spec says the run "fails", not which exception. Tolerable only because
  "Measurers carry no second copy of the bound" makes an explicit measurer-side
  validation error non-conformant too.
- `MAX == 3650*24*60*60` (Py and TS) is implied by the preceding literal assert — it
  can never fail independently; it is documentation, not a second seam.

**Spec propagation gap — RESOLVED (#166)**: `spec/USE_CASE_en.md` L739-744 / `_kr.md`
L741-746 now say "positive int seconds **within the API's admissible range**,
[`API.md` §Metric](API.md#metric-spokegovernancemetric); factory default `172800`" — a
link, not a copy. `315360000` still lives in exactly two places, `spec/API.md:609` and
`spec/feature/BACKEND.md:1263`; keep it out of USE_CASE. The seven test citations of
that sentence are now elided as `(positive int seconds … factory default 172800)`
(`test_bootstrap.py:172`, `measurers/test_ingestion_freshness.py:12` + `:918`,
`measurers/test_validation_score.py:12`, `api_wired/test_uc5_01_governance.py:166`,
`spot/test_metrics.py:86` + `:188`) — the `…` elides exactly the range clause, so the
elided form is the accurate one. Do not "restore" the old comma form.

**Blind spot the unit fake cannot see**: the impl filters `rn == 1` in SQL then builds
`{urn: (t, score)}` last-wins, so "newest row per dataset" is only provable at spot. A
multi-row-per-dataset unit fixture would be testing the fake — the delegation is correct.

`_definition_from_row(row)` builds the returned record from the *same* row object the
service `setattr`s, so in the `AsyncMock(spec=AsyncSession)` unit fakes asserting
`result.metric_conf` vs `row.metric_conf` is near-equivalent — preferring `result` is
right but is not the strong seam its docstring claims.

Related: [[owning-source-last-seen-tiebreak-untested]],
[[no-destructive-git-during-review]]. (The user-memory
`project_metrics_dryrun_no_event_or_result` covers the same dry-run-persists-nothing fact; no
corresponding note exists in this evaluator-memory corpus.)
