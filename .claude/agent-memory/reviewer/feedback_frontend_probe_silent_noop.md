---
name: frontend-probe-silent-noop
description: Reviewer render-probes in this repo fail silently three ways — pnpm test ignores the file filter, console.log is hidden without --reporter=verbose, and an RHF submit that fails validation calls nothing
metadata:
  type: feedback
---

When probing `src/frontend` behaviour with a throwaway Vitest render spec, verify the probe
actually exercised the path before trusting a green result.

Three silent no-ops seen in one session:

1. `pnpm -C src/frontend test -- components/foo.test.tsx` **ignores the path filter** and runs
   the entire 97-file suite. Run `npx vitest run components/foo.test.tsx` from
   `src/frontend` instead (check the printed "Test Files N passed" count matches 1).
2. `console.log` inside a probe is swallowed by the default reporter. Add
   `--reporter=verbose` and redirect to a scratchpad file, then grep it.
3. A `react-hook-form` submit whose zod validation fails **calls `onSubmit` zero times with
   no error thrown** — `expect(mock).toHaveBeenCalled()` is the only thing that catches it.
   A probe that only asserts on submit output proves nothing. Log the call count first.
   Getting a real submit out of `MetricForm` needs literal-valid enum values
   (`mode: "active"`, a `metric_type` from `types/governance.ts`) plus every `.min(1)` field.

jsdom implements **neither** `document.execCommand` nor `navigator.clipboard` (both are
`undefined`). Any clipboard helper that feature-detects them returns its failure value in
Vitest unconditionally, so a "copy" assertion proves only the failure branch — the
`execCommand` selection fallback needs a real browser (or an explicit stub of both).

Rendering any form that embeds a Radix `Select` (all three `DatasetFilterEditor` consumers)
also needs jsdom stubs for `ResizeObserver` and `Element.prototype.hasPointerCapture` /
`setPointerCapture` / `releasePointerCapture` / `scrollIntoView`, or the render throws in a
layout effect.

**Why:** a probe is the evidence behind a PASS/FAIL score; a probe that silently ran nothing
produces a confident but empty verdict.

**How to apply:** whenever a review depends on my own render probe rather than the
generator's tests. See also [[isolate-failures-concurrent-edit]] and
[[scratchpad-shared-with-parallel-agents]] for probe hygiene, and delete the probe file
before reporting (`git status` must be back to the generator's diff).
