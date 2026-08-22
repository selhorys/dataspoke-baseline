---
name: run-id-filter-then-assert-tautology
description: UC3 ontogen run_id tests — filter-then-assert scoping is INTENTIONAL (discriminate via any_rows_found + run_id field-presence, not every-row equality); plus the verified spec anchors for any_rows_found / RUN_COMPLETE and the stale BACKEND_LLM §Test Mode skipif line
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

**E2E double-run:** the E2E real-LLM step must fire exactly ONE method/run call and capture its
response via `page.waitForResponse`. Do not add a second call (e.g. a UI Run plus an adminApi
shape-probe) and scope any_rows_found to the latter — reused rows keep the first run's id, so the
second-run scoping false-negatives.

**method/run is synchronous + slow under real LLM:** the run POST blocks for minutes, so the
real-LLM tests pass a per-call `timeout=300.0` (api-wired) / `waitForResponse({timeout:300_000})`
(E2E); the shared 30s api_client default ReadTimeouts otherwise.

**How to apply:** When reviewing UC3 ontogen run_id tests, do NOT flag the scoped
filter-then-assert as a defect to "tighten" into all-rows-equal — that scoping is correct. Confirm
the discriminating check is run_id field-presence + any_rows_found, that only one real-LLM
method/run fires per test, and that the run POST carries a minutes-scale timeout.

## Spec anchors for `any_rows_found` (verified 2026-07-17)

`any_rows_found` has **no product-spec basis** — do not let a test cite
`BACKEND_LLM.md §Test Mode` for it. §Test Mode (L337-360) describes *stub* behaviour only
("stub Producer returns one schema-valid empty payload", L349 — that half IS citable); it states
nothing about a real LLM being required to persist ≥1 row. The honest anchors:

- the ≥1-row rule → `TESTING.md §Assertion Discipline` (anti-vacuity backstop, a test-suite rule)
- the run_id-stamping half → `BACKEND_LLM.md §Termination` L236 ("persist each row tagged with
  the `run_id`")

Also verified wrong at their cited location: `BACKEND_LLM.md §Wiring` (L286-294) never mentions
`ONTOGEN.RUN_COMPLETE` — the RUN_COMPLETE detail contract lives in `§Evidence` L282-284. Any test
citing "§Wiring — RUN_COMPLETE must follow run_debate" is mis-anchored.

`BACKEND_LLM.md §Test Mode` L358 is stale: it prescribes
`@pytest.mark.skipif(runtime_conf.get("stub_llm_client"), ...)` for "UC3 / UC4 `_with_real_llm`
variants". That is not implementable (a fixture is not available at decoration time), contradicts
`TESTING.md §Running` (inline guard, first statement of the body — and TESTING.md P3 outranks
BACKEND_LLM.md P5), and names a UC3 variant that no longer exists since UC3 became one test
parametrized over `llm_mode` in `["stub","real"]`.
