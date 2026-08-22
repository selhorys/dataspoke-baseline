---
name: e2e-predelete-natural-key-timestamped
description: Which tests/e2e natural keys are stable vs per-load timestamped — a pre-delete hook keyed on a Date.now() name is inert and cannot fix the leak it claims to
metadata:
  type: project
---

`spec/TESTING.md §E2E §Execution discipline` requires "each setup path pre-deletes by
natural key". Whether that hook does anything depends on how the key is built:

| File | Key | Stable? |
|---|---|---|
| `use-case/uc1-01-datahub-managed.spec.ts` | `SOURCE_NAME = "dummy datahub-managed"` + `urn:li:dataHubSecret:UC1_POSTGRES_PASSWORD` | yes |
| `use-case/uc1-02-active-custom-postgres.spec.ts` | `"dummy postgres example_db in catalog schema"` | yes |
| `use-case/uc1-03-passive-kafka.spec.ts` | `"dummy kafka topics"` | yes |
| `ground/metagen/conf-new.spec.ts` | `ground-new-${Date.now().toString(36)}` | **no** |
| `use-case/uc4-01-metadata-generation.spec.ts` | `uc4-{eu,oe,rival}-${Date.now().toString(36)}` | **no** |

**Why it matters:** Playwright recycles the worker process after a failure, so a serial-group
retry **re-imports the spec module** and `Date.now()` yields a *new* name. A `beforeAll` that
lists and filters `name === CONF_NAME` therefore cannot match the previous attempt's leftover —
the loop body is unreachable and the "POST succeeded, read-back died, afterAll had no id" leak
it was added to fix survives. A timestamped name prevents *collisions*; it does not enable
*pre-deletion*.

**How to apply:** whenever a generator reports "added a beforeAll pre-delete by natural key",
check how the key is constructed before accepting it. Fix is either a fixed literal name (the
uc1-* pattern) or a prefix sweep (`name.startsWith("ground-new-")` / `"uc4-eu-"`).
Related: [[e2e-60s-timeout-vs-180s-polls]].
