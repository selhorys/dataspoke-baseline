---
name: dataset-filter-editor-mutation-seams
description: Cycle-2 measured kill/survive table for DatasetFilterEditor + RunDialog + DatasetFilterView (#146/#147) — 22/26 mutants killed; the 4 survivors are equivalent, React-artifact, or spec-conformant
metadata:
  type: project
---

`src/frontend/components/dataset-filter-editor.tsx` buffers raw textarea text per list
dimension and reseeds from props via a `syncedRef` + element-wise `sameList` guard.
`metagen/run-dialog.tsx` reuses the exported `splitList`. Measured by copying each impl to
`components/__revmut_*.tsx` and re-pointing a copy of its test file (isolated — never mutate
the shared file while a parallel reviewer runs; `pnpm vitest run <file>`, and note
`--reporter=basic` was removed in vitest 4 and hard-errors).

Editor (32 tests) — 11/14 killed. Survivors:

| Mutant | Result | Why it does not matter |
|---|---|---|
| `setRaw((prev) => ({[dim]: text}))` (siblings become `undefined`) | SURVIVES | `value={undefined}` flips the sibling textarea to **uncontrolled**, so the DOM retains its text in jsdom *and* in a real browser. The data-loss variant (siblings set to `""`) IS killed by the two multi-dimension composition tests. |
| `.split(/\r?\n/)` → `.split("\n")` | SURVIVES | Equivalent mutant: the following `.trim()` already strips the residual `\r`. |
| reseed **all** dims instead of only stale ones | SURVIVES | Spec states the guard at *filter* level, so reseeding every box on a differing filter is conformant. Pinning per-dimension granularity needs a FRONTEND_BASIC sentence first. |

Killed: comma-split revert, no-trim, no-blank-filter, re-serialise `value={joinList(...)}`,
emit `[]` instead of `undefined`, `sameList`→`a===b`, `emit` not stamping `syncedRef`,
reseed-effect neutered, hints reverted to "comma-separated", `origin` not collapsing to
`undefined`.

RunDialog (8 tests) — 7/7 killed, incl. dry_run default flip and URNs-dropped-on-dry-run.
DatasetFilterView (3 tests) — 4/5 killed; `whitespace-pre-wrap` → `whitespace-break-spaces`
is killed although break-spaces also preserves whitespace (a mild over-pin).

**How to apply:** this component's editor-level seams are now saturated — further mutation
work has low yield. The remaining hole is one level up: no test renders the editor inside
`MetricForm` / `MetagenConfForm` / `OntogenConfForm` (the two conf *page* tests stub the
form outright), and `refetchOnWindowFocus: false` in `app/providers.tsx:49` — which is what
keeps the conf pages' `useEffect([conf])` reseed from firing mid-edit — is **unspecced**, so
pinning it is a spec change first. See reviewer-side `filter-editor-reseed-refetch-coupling`.
