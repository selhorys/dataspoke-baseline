---
name: rangepicker-day-bounds-unspecced
description: RangePicker date-mode end-of-day T23:59:59.999Z millisecond bound is impl-only, not spec'd; spec only says inclusive {from,to}
metadata:
  type: project
---

The shared RangePicker (`src/frontend/lib/range.ts`, `components/range-picker.tsx`) date-granularity
math bounds whole UTC days: `from`=00:00:00.000Z of (today-(days-1)), `to`=23:59:59.999Z of today.

**Why:** The exact end-of-day instant (`T23:59:59.999Z`) and "today plus prior six" day-count math
live ONLY in the impl docstring, not in any spec. The canonical spec (`spec/feature/FRONTEND_BASIC.md`
Shared component notes, ~L282-297) specifies: 5 presets with "Last 2 weeks (default)" = 14 days,
`YYYY-MM-DD` / `YYYY-MM-DD HH:mm` display, inclusive `{from,to}`, and param mapping (validation
`attr/validation/result` -> `until=to`, all others -> `to`). API.md:671-672 confirms from/to are
inclusive. Nothing in spec mandates the `.999Z` millisecond or the whole-UTC-day decomposition.

**How to apply:** When reviewing `range.test.ts`, the inclusive-bound + 14-day-default + display-format
+ 5-preset assertions are spec-derived (PASS). The exact `T23:59:59.999Z` / `T00:00:00.000Z`
millisecond assertions are impl-derived invariants -- acceptable as documenting deliberate impl
behavior given they're TZ-stable under frozen time, but they are NOT traceable to a spec line. Don't
demand they be removed; do note they'd need a spec anchor (or a "documents impl, not spec" comment)
to be fully clean. The `until = to` param-mapping invariant has NO unit coverage anywhere (no
validation/governance page tests exist) -- it's only exercised by E2E/integration. Nice-to-have gap.

**Cycle-2 confirmed (2026-06-16, APPROVE):** The Apply-commit test's end-bound assertion was
weakened from exact `"2024-03-15T06:45:59.999Z"` to prefix `committed.to.startsWith("2024-03-15T06:45")`.
This is the right call: `composeIso` (range-picker.tsx) hard-codes `seconds=59, ms=999` for the `to`
bound, so the `:59.999` tail is a pure impl detail. The prefix still pins (a) the seeded end day and
(b) the user-edited minute, and is asserted alongside `committed.kind==="custom"` (line 181, outside
the `if` narrowing guard) and `toHaveBeenCalledTimes(1)` (line 179) — so wrong-day, wrong-time, and
wrong-kind regressions all still fail. The `if (committed.kind==="custom")` is a TS narrowing guard,
not a mask (line 181 already fails the test if kind is wrong). No TZ dependence: frozen clock + UTC
ISO throughout. range.test.ts kept the exact T23:59:59.999Z bounds with a "documents impl, not spec"
comment (acceptable per this note). Renewal invariant (clock-advance preset re-resolves to new day;
custom stays pinned) intact at range.test.ts:86-136. Persistence hook tests
(lib/hooks/use-range-selection.test.ts) unchanged and sound. 764/764 pass.

**Per-picker tz toggle removal (2026-06-16, cycle 1, REVISE):** Feature change — the per-picker
Local|UTC toggle was removed; the picker now takes a fixed `tz` prop from the global Settings
preference (spec FRONTEND_BASIC.md:290 now reads "**no per-panel timezone control**"). Tests cleanly
dropped `onTzChange`/`tzOverride`/`setTzOverride` (grep-clean across whole frontend tree); surviving
picker tests (preset-staging, time-edit→custom, two-calendar, fixedWeeks) all still valid with the
fixed `tz` prop, 816/816 pass. Global-tz coverage intact: timezone.test.ts (default+persist+reactive),
format-time.test.ts (local offset-agnostic / utc exact), events-section.test.tsx (display-site
integration). ONE must-fix: `lib/range.test.ts:223-226` spec-trace COMMENT still quotes the deleted
spec text "A per-picker timezone toggle — Local or UTC …" — a stale, now-FALSE spec citation in a
file labelled unchanged-must-pass. It's a comment so the suite is green, but it asserts the spec says
the opposite of what it says (T1 traceability rot). Fix: rewrite the comment to cite the global-tz
governance (the `tz` here is a lib/range function PARAM driven by global pref, not the removed UI
toggle — the local/utc interpretation coverage itself is correct and must stay).
