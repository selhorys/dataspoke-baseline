---
name: e2e-60s-timeout-vs-180s-polls
description: E2E per-test timeout is 60s but use-case specs hand-roll 120-180s poll budgets, so their "exhausted budget" failure messages are unreachable
metadata:
  type: project
---

`tests/e2e/playwright.config.ts` sets `timeout: 60_000` (per-test) and no use-case spec calls
`test.setTimeout()` / `.slow()`. Several use-case steps hand-roll polling loops with
120_000-180_000 ms deadlines (uc1-01 steps 2/5/6, uc1-03 steps 0/3/5), so a loop that genuinely
exhausts its budget is killed by Playwright's 60s test timeout first.

**Why:** the polls normally settle well inside 60s (the suite is green), so the mismatch is
invisible in practice — it only shows up in the failure path, which is exactly the path a
reviewer is asked to reason about.

**How to apply:** when auditing an E2E assertion that follows a hand-rolled poll ("an exhausted
wait is a failure, not a skip", `spec/TESTING.md` §E2E §Execution discipline), note that the
carefully-worded post-loop `expect(...)` message never prints — the failure surfaces as
"Test timeout of 60000ms exceeded". The spec's *behavior* requirement is still met (it fails, it
does not skip), so this is a diagnostic-quality finding, not a correctness one. Related:
[[e2e-events-panel-frozen-upper-bound]].
