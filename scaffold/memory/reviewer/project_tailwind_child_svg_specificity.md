---
name: tailwind-child-svg-specificity
description: Button's base [&_svg]:size-4 outranks a child icon's own h-3.5/w-3.5, so per-icon sizing in Button is dead — tailwind-merge cannot see the conflict
metadata:
  type: project
---

`components/ui/button.tsx` carries `[&_svg]:size-4 [&_svg]:shrink-0` in the cva base. That compiles to `.\[\&_svg\]\:size-4 svg { width/height: 1rem }` — specificity (0,1,1) — which **outranks** an icon element's own `.h-3\.5` (0,1,0). So `<Button><CalendarIcon className="h-3.5 w-3.5"/></Button>` (e.g. `components/range-picker.tsx`) renders a **16px** icon, not 14px. tailwind-merge does not help: the two classes live on different elements, so there is no conflict for it to resolve.

**Why:** it makes "matching" icon sizing across a `Button`-based control and a non-Button control (e.g. a Radix `SelectTrigger`, which has no `[&_svg]:` rule) silently diverge. `ChartGrainPicker` sets `[&>svg]:h-3.5` on its trigger and genuinely gets 14px, while the RangePicker it is meant to sit flush beside gets 16px.

**How to apply:** When a diff claims two adjacent controls are visually matched, do not compare the authored class strings — resolve specificity. Anything rendered inside `ui/button.tsx` is size-4 regardless of its own `h-*`/`w-*`. To match a Button-hosted icon from a non-Button trigger, use `[&>svg]:size-4`, not `h-3.5`. Cosmetic-only, so this is a low finding, not a blocker; jsdom/Vitest cannot catch it — see [[recharts-css-var-svg-attr]] for the same "verify rendering empirically" lesson.
