---
name: filter-formatter-parity
description: TS formatDatasetFilter and Python format_filter agree only on already-canonical clauses (keyword/column case, redundant parens differ) — plus the node type-strip recipe for running frontend TS without a probe file
metadata:
  type: project
---

`src/frontend/lib/dataset-filter-format.ts` (`formatDatasetFilter`, Auto-indent) and
`src/shared/dataset_filter.py` (`format_filter`) produce **byte-identical** output for a clause
that is already canonical — verified on the API.md example, `IN ('a', 'b')` (comma + one space),
nested groups, `''` escapes, and 4-space indent. They diverge whenever the input is not canonical:

| Input | TS | Python |
|---|---|---|
| `origin = 'PROD' and x` | keeps `and` | uppercases to `AND` |
| `ORIGIN = 'PROD'` | keeps `ORIGIN` | lowercases to `origin` |
| `((origin = 'A'))` | keeps both paren levels | drops the redundant pair |
| `origin = 'PROD' -- c` | `origin = 'PROD' - - c` | raises `DatasetFilterSyntaxError` |

This is by design (FRONTEND_BASIC calls the TS side "purely lexical, no grammar knowledge"), but
`format_filter`'s docstring calls itself the reference the TS output "is pinned against" — a test
that asserts equality on non-canonical input will fail. `formatDatasetFilter` is idempotent on
every case tried, including unbalanced parens and unterminated literals.

**Why:** the divergence is invisible from either file alone, and the tempting cross-layer test is
wrong for most inputs.

**How to apply:** when a stage claims TS/Python formatter parity, run both. Executing the frontend
module needs no probe file inside `src/frontend` (which risks colliding with a concurrent test
stage — see [[scratchpad-shared-with-parallel-agents]]): copy the `.ts` to the scratchpad as
`.mts`, write an `.mts` driver importing it, and run `node --experimental-strip-types driver.mts`
(node 24 in this env). For component behaviour a real Vitest probe is still needed — see
[[frontend-probe-silent-noop]].
