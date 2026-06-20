---
name: run-id-filter-then-assert-tautology
description: UC3 evidence-Langfuse run_id tests — the filter-then-assert looks tautological but the scoping is INTENTIONAL (result tables hold multiple runs' rows); discriminate via any_rows_found + run_id field-presence, NOT by asserting every row==this run_id
metadata:
  type: project
---

In `tests/integration/api_wired/test_uc3_01_ontology_generation.py` and
`tests/e2e/use-case/uc3-01-ontology-generation.spec.ts`, the per-row run_id check filters rows
where `run_id == this run's id` and the inner `assert r.run_id == run_id` is redundant by
construction. The load-bearing signal is `any_rows_found` (≥1 row carried this run's run_id) — a
NULL or swapped run_id on the new rows leaves the filter empty and fails it.

**Do NOT "fix" the redundancy by asserting EVERY returned row's run_id == this run's id.** The
ontogen result tables legitimately hold rows from *multiple* prior runs — each row carries its own
creating-run id (run_id is insert-only, never overwritten on reuse — BACKEND_LLM.md §Evidence).
`--reset-seed` clears the tables, but earlier runs' rows and uc4-seed context nodes coexist in the
general case. Asserting all-rows-equal-latest-run_id breaks on those legitimate rows (verified: it
failed on a `title` node left by a prior run). The correct, non-tautological pattern: assert the
`run_id` FIELD is present on every row (schema contract) + `any_rows_found` scoped to this run as
the discriminator. (The "assert a seeded row carries a different run_id" idea only works when
deliberately-seeded ontogen rows exist — the standard UC3 api-wired flow has none.)

**E2E double-run (FIXED):** the E2E real-LLM step formerly fired TWO method/run calls (UI Run +
an adminApi shape-probe) and scoped any_rows_found to the *latest* — a false-negative because
reused rows keep run-1's id. It now captures the single UI run's response via
`page.waitForResponse`; do not reintroduce a second run.

**method/run is synchronous + slow under real LLM:** the run POST blocks for minutes, so the
real-LLM tests pass a per-call `timeout=300.0` (api-wired) / `waitForResponse({timeout:300_000})`
(E2E); the shared 30s api_client default ReadTimeouts otherwise.

**How to apply:** When reviewing UC3 ontogen run_id tests, do NOT flag the scoped
filter-then-assert as a defect to "tighten" into all-rows-equal — that scoping is correct. Confirm
the discriminating check is run_id field-presence + any_rows_found, that only one real-LLM
method/run fires per test, and that the run POST carries a minutes-scale timeout.
