---
name: grain-no-request-param-untested
description: "A display-only control's 'adds no request parameter' clause needs a two-render deep-equal param assertion per surface; tsc catches a new key but not an altered from/to/until/limit"
metadata:
  type: project
---

`FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker` binds three surfaces (governance
dashboard header, governance metric-detail `Result` header, per-dataset Validation panel
`Quality Score` row) with: *"the grain … adds no request parameter: it never alters the
`from` / `to` / `until` / `limit` a call site sends."*

**The trap (measured, issue #127 cycle 2).** With only the `/governance/metrics/[id]` E2E
guarding the clause, injecting `grain` into `useMetricResults` / `useValidationResults` params
at the other two sites left the entire 1598-test frontend suite green. `tsc --noEmit` *does*
reject a new key (`TS2353` excess-property check against `MetricResultsParams` /
`ValidationResultsParams`) — but a leak that alters an **existing** param, e.g.
`limit: grain === "hourly" ? 2000 : 1000`, is invisible to both tsc and the whole suite. That
altered-existing-param form is precisely what the spec sentence names.

**Why:** a presentational picker cannot host a falsifiable "issues no fetch" test — the claim
only has teeth at the panel *owners*. It is easy to read one E2E as covering the whole clause.

**How to apply:** per surface, render at two grains and assert the captured hook argument list
is deep-equal. Two things the naive version needs, both learned the hard way here:

- **Freeze the clock** (`vi.useFakeTimers()` + `setSystemTime`). A preset's lower bound resolves
  against `Date.now()`, so two separate mounts differ for reasons unrelated to grain — red for
  the wrong reason.
- **Add a non-vacuity backstop.** Have the chart stub echo the prop (`data-grain`) or its plotted
  categories, and assert the two legs actually differ. Without it the test passes trivially when
  hydration never applies the stored grain.

Landed for all three surfaces in `validation-data-panel.test.tsx`, `metric-card.test.tsx`
(`grain adds no request parameter` describes) and the E2E. Reuse the shape for any future
display-only control that claims to leave a request untouched.
