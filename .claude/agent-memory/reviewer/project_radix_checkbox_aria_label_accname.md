---
name: radix-checkbox-aria-label-accname
description: Radix Checkbox in this repo carries an explicit aria-label, so a wrapping <label> never contributes to the accessible name — a green getByRole("checkbox", {name}) suite CANNOT prove a newly added group label landed outside the labels
metadata:
  type: project
---

`components/ui/checkbox` (Radix) renders `<button type="button" role="checkbox"
aria-label="...">` and every call site in `src/frontend` passes an explicit
`aria-label`. Verified by DOM dump on
`components/governance/metric-dataset-table.tsx`: the three verdict toggles sit
inside `<label class="flex ...">…<Checkbox aria-label={verdict}/>{verdict}</label>`,
and each accessible name is exactly the `aria-label`.

**Why it matters:** when a stage adds a *visible* group label next to such
checkboxes, the classic failure is nesting it inside one of the `<label>`
wrappers, which would change that checkbox's accessible name and break every
`getByRole("checkbox", { name: "true" })` query. But because `aria-label` wins
the accname computation over the host-language `<label>`, **the queries keep
passing either way**. A green Vitest/Playwright run is *not* evidence that the
label landed outside. `label.textContent` would be wrong, and sighted/AT
behaviour would diverge, with zero test signal.

**How to apply:** verify structurally, not by test outcome. Reading the JSX is
usually enough (is the new node a sibling of the `.map()` that emits the
`<label>`s, or inside it?). To be certain, dump the DOM from a throwaway test in
the component's own directory and delete it after:

```tsx
const group = screen.getByRole("group", { name: "…" });
console.log("GROUP_HTML>>>" + group.outerHTML);
// per checkbox: aria-label vs (el.closest("label")?.textContent)
console.log(el.closest("label")?.textContent);
```

Run with `pnpm -C src/frontend exec vitest run <path> --reporter=verbose`
(`pnpm test` swallows file filters, and non-verbose reporters swallow
`console.log` — see [[frontend-probe-silent-noop]]). The tell for a correct
placement is `closest("label").textContent === verdict` (not
`"criterion met: true"`). Related: [[frontend-probe-silent-noop]].
