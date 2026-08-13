---
name: metric-measurement-window-test-seams
description: fix-metric-time-window cycle-1+2 — measured which metric-window tests kill the pre-fix derivation, the exact-cutoff spec/impl divergence, and where the unit fake is blind
metadata:
  type: project
---

`ingestion-freshness` / `validation-score` dropped per-dataset window derivation for
`metric_conf.time_window_sec` (BACKEND.md §Measurement window; evidence model moved to
§Ingestion evidence). Two measured harnesses, both read-only, both worth rebuilding:

**Pre-fix harness** — `git show dev:` the two measurer files into /tmp, then in a pytest
plugin (`-p`, on `PYTHONPATH`) **exec the old source into the real module's `__dict__`**.
Exec-in-place matters: loading the old code as a *separate* module leaves the tests'
`_freeze_now` monkeypatch (`setattr(<real module>, "datetime", …)`) pointing at the wrong
globals, and three boundary tests then "fail" for the wrong reason. Also stub
`src.shared.schedule` (deleted by the fix) and `config_service.get_runtime_config`
(the DTO no longer carries `validation_score_n_intervals`).

**Mutation harness** — same trick with a text-substituted copy of the *current* file
(`assert text.count(pattern) == 1` first).

Measured, post-fix-pass:

- **Exactly 5 unit tests fail against the pre-fix impl**: freshness
  `…_passive_owned_dataset_outside_the_declared_window_is_stale`,
  `window_is_the_declared_config_value_for_a_passive_owned_dataset`,
  `every_owning_mode_and_tier_reports_the_same_declared_window`,
  `stale_breakdown_detail_includes_the_window_and_the_evidence_tier`; validation
  `breakdown_detail_keys_are_exactly_the_three_spec_fields`. The two key-set equalities
  catch the removed `window_source`; the rest catch PASSIVE→7200.
- **Genuine spot carriers** (by construction, not run): passive declared-window,
  unclaimed-dataset (`window_source` absence), validation declared-window
  (pre-fix sum 1.5 vs 1.0 — 4 rows/dataset 24h apart makes the old 48h window differ
  from a declared 86400). `breakdown_counts_reconcile` and `reads_source_keyed_events`
  are **not** carriers: their sources are `ACTIVE_CUSTOM_MANAGED daily`, whose derived
  window was already 172800.
- **All 6 cutoff mutants die** (`<`↔`<=`, `>`↔`>=`, `window_sec ± 1` on both measurers).
  Note the `<=`/`>=` mutants are killed *only* by the two exact-cutoff pins.

**Exact-cutoff divergence is real and mutual.** freshness `last_event_at > cutoff`
(exactly-at ⇒ stale) vs validation `latest_data_time < cutoff` (exactly-at ⇒ counted).
BACKEND.md's freshness wording ("no older than" / "older than") reads *inclusive*, so
freshness diverges; validation's "inside the window" is genuinely ambiguous. Both are
pinned in tests with self-declared "this is not a spec assertion" docstrings. The
spec should settle it in one clause; until then treat a flip as review-visible, not a bug.

**Blind spots the unit fake cannot see**: the new impl filters `rn == 1` in SQL and then
builds `{urn: (t, score)}` last-wins, so "newest row per dataset" is only provable at spot
(the spot fixture's fresh side — 1h vs 25/49/73h against a 24h window — is what carries it).
A multi-row-per-dataset unit fixture would be testing the fake, not the SQL; the delegation
is correct, not a hole.

Related: [[project_owning_source_last_seen_tiebreak_untested]],
[[project_metrics_dryrun_no_event_or_result]].
