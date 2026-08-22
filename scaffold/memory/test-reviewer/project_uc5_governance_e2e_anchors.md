---
name: project-uc5-governance-e2e-anchors
description: Stable test anchors + identity facts for auditing UC5 governance dashboard tests (E2E + metric-card unit)
metadata:
  type: project
---

UC5 governance dashboard test anchors that make assertions load-bearing rather than impl-pinned.

**Why:** Cycle-2 test review of the combined-metric-card refactor needed to confirm new
assertions guard real behavior, not incidental DOM. These facts are non-obvious from a single file.

**How to apply:** When reviewing `tests/e2e/use-case/uc5-01-governance.spec.ts` or
`src/frontend/components/governance/metric-card.test.tsx`:

- `MetricDefinition.id === the client-supplied metric_id` from the create body (FRONTEND_GOVERNANCE.md
  §Metrics "client-supplied metric_id"; step 1c asserts `conf.id === cfg.metric_id`). So
  `data-testid={`metric-card-${metric.id}`}` is correctly scoped by `getByTestId(`metric-card-${metric_id}`)`.
- Dashboard renders ONLY one `MetricCard` per enabled metric — no standalone "Daily trend" h2
  (dashboard/page.tsx). The negative guard `getByRole("heading",{name:"Daily trend"}).toHaveCount(0)`
  is valid; its retry envelope is the surrounding `.toPass`.
- A mounted recharts chart emits `.recharts-wrapper` (wrapper div) and `svg.recharts-surface` (Surface) —
  real classes, good structural anchors for "inline trend chart present".
- metric-card.test.tsx mocks `useDisplayTz: () => "utc"` — the valid `TzMode` union value (lowercase;
  `"UTC"` uppercase is NOT assignable and breaks `tsc` even though Vitest still passes). The measured-at
  assertion derives its expected string from the same `formatDate(measured_at, "utc")` the component uses,
  so it is self-consistent across host TZ. The spec pins *that a date shows*, not its format.
- `METRIC_EMITTED_KEYS["doc-health"] = ["total","doc_health"]` — "total" is a genuine values key, so the
  empty-branch `queryByText("total").not.toBeInTheDocument()` guard is meaningful.
