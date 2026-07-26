---
name: e2e-events-panel-frozen-upper-bound
description: Event panels freeze their range upper bound at mount from the BROWSER clock, so fresh events are invisible until remount — the real cause behind toPass wrappers on event assertions
metadata:
  type: project
---

`src/frontend/components/events-panel.tsx` computes
`range = useMemo(() => resolveRange(selection, "datetime", tz), [selection, tz])`.
`selection` is a stable state object, so the memo runs once per mount and
`presetRange` datetime mode sets `to = new Date()` — the **browser's** clock —
which then never advances. `useDatasetEvents` polls forever against that frozen
upper bound.

Two consequences:
- A user leaving the page open never sees a newly-written event.
- Rows are stamped by the **cluster** clock. Any browser-behind-server skew (the
  dev host measured ~2.1 s behind) puts a just-written event in the browser's
  future, and the panel correctly reports "No events … in the selected window".

`lib/range.ts` documents a preset as "re-resolved on every read so it always
includes today" — the `useMemo` defeats that within a mount. The freshness
semantics are **not** specified (see [[rangepicker-day-bounds-unspecced]]), so
this is an impl/doc contradiction, not a spec violation.

**Why:** it is the standing reason E2E event assertions get wrapped in
`expect(async () => { goto; assert }).toPass({ timeout })` — a remount recomputes
`to` and absorbs the gap.

**How to apply:** when a reviewed diff adds a toPass/reload-retry around an event
assertion, this is legitimate (TESTING.md §E2E §Execution discipline sanctions
navigate-and-assert-in-toPass) — do not score it as masking. But check the
justification fits: the skew window is seconds, so a step whose events were
written a prior step earlier (behind a 30–180 s readiness poll) cannot be
explained by it, and a skew-specific comment there is a mis-diagnosis. Say
plainly that the underlying frozen-bound behaviour deserves a product issue.

Related: [[rangepicker-day-bounds-unspecced]], [[waitfor-presettlement-race]]
