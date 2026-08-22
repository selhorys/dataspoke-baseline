---
name: e2e-beforeall-skip-blast-radius
description: Verified in Playwright 1.60 source — test.skip() in beforeAll skips EVERY test in the suite, and test.setTimeout() in a hook resizes only that hook; both matter when auditing e2e gate hoists
metadata:
  type: project
---

Measured against `tests/e2e/node_modules/.pnpm/playwright@1.60.0/.../lib/worker/workerProcessEntry.js`,
not from docs:

- **`test.skip()` inside `beforeAll` skips the whole suite.** The skip annotation is pushed onto
  `testInfo.annotations` before `TestSkipError` is thrown; `_runAllHooksForSuite` collects it into
  the suite's `extraAnnotations`, and `_runTest` replays those annotations onto **every** later
  test in that suite (`_activeSuites.get(suite)`), setting `expectedStatus = "skipped"`. So
  hoisting a per-step gate into `beforeAll` converts a one-step skip into a whole-arc skip.
- **`test.setTimeout()` inside `beforeAll`/`afterAll` resizes only the hook.** Each hook gets a
  fresh `timeSlot = { timeout: project.timeout }`; `TimeoutManager.setTimeout` writes
  `this._running.slot.timeout`. Hooks therefore need their **own** `setTimeout` — the tests'
  budgets do not cover them, and vice versa.

**Why:** the mode gate (`stub_llm_client`) is consumed by only 1-2 steps in UC3's stub arc and in
UC4, but the whole arc now skips when `/admin/conf` is unreadable. Since the shared
`readStubLlmClient` returns `readable: false` for **any** non-2xx, a 403/500 on a live admin
route silently green-skips ~13 UC4 tests instead of failing.

**How to apply:** when a gate read is hoisted "once per arc", check that the *skip* stayed at the
gate site even though the *read* moved. Storing the discriminated union in the module variable
and resolving it at the consuming step (what `ground/ontogen/result-table.spec.ts` does) gives one
read without the blast radius. Related: [[e2e-predelete-natural-key-timestamped]].
