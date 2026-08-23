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

**Cycle-2 (2026-06-16, APPROVE).** The Apply-commit test's end-bound assertion was weakened from
exact `"2024-03-15T06:45:59.999Z"` to prefix `committed.to.startsWith("2024-03-15T06:45")` — correct,
since `composeIso` (range-picker.tsx) hard-codes `seconds=59, ms=999` for `to`, so the `:59.999` tail
is pure impl detail. The prefix still pins the seeded end day and the user-edited minute, paired with
`committed.kind==="custom"` (line 181) and `toHaveBeenCalledTimes(1)` (line 179), so wrong-day,
wrong-time, and wrong-kind regressions all still fail; `range.test.ts` keeps the exact `.999Z` bounds
elsewhere behind a "documents impl, not spec" comment. Renewal invariant intact at
`range.test.ts:86-136`; `use-range-selection.test.ts` unchanged. 764/764 pass.

**Cycle-1 (2026-06-16, REVISE) — per-picker tz toggle removed.** The per-picker Local|UTC toggle was
removed; the picker now takes a fixed `tz` prop from the global Settings preference
(`FRONTEND_BASIC.md:290`: "no per-panel timezone control"). Tests dropped `onTzChange`/`tzOverride`/
`setTzOverride` cleanly (grep-clean across the frontend tree), 816/816 pass. Global-tz coverage:
`timezone.test.ts`, `format-time.test.ts`; the display-site integration piece moved from the deleted
`events-section.test.tsx` to `src/frontend/components/events-panel.test.tsx` (`/data/[urn]`
unified-hub migration, commit `7620ab99`).

**Still open:** `lib/range.test.ts:223-226`'s spec-trace comment still quotes the deleted line "A
per-picker timezone toggle — Local or UTC …" in a file labelled unchanged-must-pass — a false T1
citation (the suite stays green since it's only a comment, but it asserts the spec says the opposite
of what it now says). Fix: repoint the comment to the global-tz governance — the `tz` here is a
lib/range function param driven by global pref, not the removed toggle; the underlying local/utc test
coverage itself is correct and must stay.
