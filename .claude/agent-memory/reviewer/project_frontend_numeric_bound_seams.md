---
name: frontend-numeric-bound-seams
description: Two seams when a frontend mirrors a backend numeric bound — no form in src/frontend sets noValidate (so a min/max attr preempts the zod message with a native bubble), and z.coerce.number() turns boolean true into 1
metadata:
  type: project
---

When a stage mirrors a backend numeric bound into `src/frontend` (field attribute + zod
rule), two things are true in this repo and invisible to the Vitest/jsdom suite:

- **No `<form>` in `src/frontend` sets `noValidate`** (grep is empty; `--include=*.tsx`).
  Every RHF form submits natively, so a `min=`/`max=` attribute on `<Input type="number">`
  triggers *interactive constraint validation* first: the browser blocks the submit event,
  RHF never runs, and the user sees the native bubble instead of the zod message the same
  change added. `max={...}` is already the house pattern (`admin/conf/page.tsx`,
  `metagen/conf-form.tsx`, `governance/metric-form.tsx`), and the `min` side has always
  behaved this way — so it is convention-consistent, not a regression. jsdom does not
  implement form validation, so the whole suite stays green either way.
- **`z.coerce.number()` accepts `true`** — it coerces to `1`, passing `.int().positive()`.
  A backend that explicitly rejects `bool` (it subclasses `int` in Python) has no mirror on
  the client unless the zod rule adds a pre-coercion guard. Reachability is usually nil,
  because `register()` on a number input yields a string and the `toInternal` helpers filter
  with `typeof x === "number"` — check that before rating it.

**Why:** both make a "the client mirrors the server bound" claim partly false while every
test, typecheck and lint run stays clean, so neither shows up in a generator's evidence.

**How to apply:** on any diff adding a numeric bound to a form, say explicitly which of the
two enforcement points actually reports to the user, and whether the constant is consumed by
both (a `*_MIN` constant used only by the `min=` attribute while zod keeps `.positive()` is a
drift seam). Probe the zod rule directly with a throwaway spec — see
[[frontend-probe-silent-noop]] for the recipe and the `npx vitest run <path>` caveat.
