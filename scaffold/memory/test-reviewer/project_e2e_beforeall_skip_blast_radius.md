---
name: e2e-beforeall-skip-blast-radius
description: Verified in Playwright 1.60 source — test.skip() in beforeAll skips EVERY test in the suite. Promoted to spec/TESTING.md §Execution discipline.
metadata:
  type: project
---

Now documented in `spec/TESTING.md` §End-to-End Testing → Execution discipline.

**Incident that surfaced it:** the `stub_llm_client` mode gate, consumed by only 1-2 steps in
UC3's stub arc and in UC4, was hoisted into a `beforeAll` — a 403/500 on `/admin/conf` then
silently green-skipped ~13 UC4 tests instead of failing.

Related: [[e2e-predelete-natural-key-timestamped]].
