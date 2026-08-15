---
name: metric-measurement-window-test-seams
description: metric time-window test seams after two review cycles — the harnesses worth rebuilding, the 10 mutants now all killed and by which tests, and the residual impl-pinned bits (message string, OverflowError, USE_CASE "positive int")
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
it is uncommitted. (See [[feedback_no_destructive_git_during_review]].)

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

**Spec propagation gap, not a test defect**: `spec/USE_CASE_en.md` ~L739-740 still says
"positive int seconds" with no ceiling, contradicting API.md `[1, 315360000]`. Three
test headers (`test_ingestion_freshness.py`, `test_validation_score.py`,
`test_bootstrap.py`) quote it verbatim and correctly. USE_CASE is priority-1, so this
may be deliberate — route it to `spec-reviewer`, not the test agent.

**Blind spot the unit fake cannot see**: the impl filters `rn == 1` in SQL then builds
`{urn: (t, score)}` last-wins, so "newest row per dataset" is only provable at spot. A
multi-row-per-dataset unit fixture would be testing the fake — the delegation is correct.

`_definition_from_row(row)` builds the returned record from the *same* row object the
service `setattr`s, so in the `AsyncMock(spec=AsyncSession)` unit fakes asserting
`result.metric_conf` vs `row.metric_conf` is near-equivalent — preferring `result` is
right but is not the strong seam its docstring claims.

Related: [[project_owning_source_last_seen_tiebreak_untested]],
[[project_metrics_dryrun_no_event_or_result]], [[feedback_no_destructive_git_during_review]].
