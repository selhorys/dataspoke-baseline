---
name: runtime-config-ssr-test-seams
description: Issue #129 (SSR Google href) test seams — the 7-mutant battery the Vitest suite now survives, the jsdom server-shape seam, and the E2E run-mode caveat that can still make the guard vacuous
metadata:
  type: project
---

`src/frontend/lib/runtime-config.ts` resolves per field:
`window.__DATASPOKE_RUNTIME_CONFIG__ > DATASPOKE_* > NEXT_PUBLIC_* > ""`.

**Mutation battery (measured, `pnpm -C src/frontend exec vitest run lib/runtime-config.test.ts`,
11 tests).** Six defect mutants are killed and the behaviour-preserving restructure survives —
re-run these before believing any future claim about this file:

| Mutant | Result |
|---|---|
| tier collapse (one `Boolean(DATASPOKE_API_BASE_URL)` decision for both fields) | killed by the mixed-tier case |
| `??` for `||` on the server DATASPOKE reads | killed by the DATASPOKE-empty case |
| swapped DATASPOKE_API/AIRFLOW | killed ×3 |
| NEXT_PUBLIC_* outranks DATASPOKE_* | killed ×2 |
| window branch treats `""` as configured | killed by the window-empty case |
| only apiBaseUrl gets the server fix | killed ×2 |
| **production-equivalent restructure** (`typeof window === "undefined"` → DATASPOKE_*, else global → NEXT_PUBLIC) | **passes 11/11 — no false failure** |

The jsdom conflation is fixed by `vi.stubGlobal("window", undefined)` in every DATASPOKE-tier case
(`typeof` on a global whose value is `undefined` yields `"undefined"`, so the impl takes the server
branch), with `vi.unstubAllGlobals()` in `afterEach` and a `expect(typeof window).toBe("undefined")`
backstop per case. `beforeEach` stubs DATASPOKE_* to `undefined` too, so no ambient shell value can
make a "nothing configured" case pass for the wrong reason.

**Anchor status (still open).** `spec/feature/FRONTEND_BASIC.md §Stack` carries only "the server
injects `DATASPOKE_API_BASE_URL` into the page; empty falls back to same-origin". The two middle
tiers (`DATASPOKE_* > NEXT_PUBLIC_*`) and "an empty string counts as unset" live only in
`src/frontend/README.md §Production`, rewritten in the same change set as the fix. The test file
carries an explicit ANCHOR CAVEAT naming this; promoting the rule into §Stack is an open action.

**E2E run-mode vacuity (measured, `next start` on the checked-in `.next`).** With
`DATASPOKE_API_BASE_URL` set, `/login`'s first HTML carries
`<a href="http://api.<host>/api/v1/auth/google/login"><button …>Sign in with Google</button></a>` —
the Suspense boundary does NOT swallow it (root layout is `dynamic = "force-dynamic"`,
`prerender-manifest.json routes` is empty), so the raw-HTML regex premise holds. With DATASPOKE_*
absent the href falls to the **build-time-inlined** NEXT_PUBLIC value and is still absolute — and
because `.env.local` and `apiBaseUrl()` both derive from the ingress domain, the host-equality
assertion passes too. So under host `pnpm dev` + `PLAYWRIGHT_BASE_URL` the guard is fully vacuous;
only the cluster image (`.dockerignore` line 23 excludes `.env*`) makes it load-bearing. The files
document this in prose; a `test.skip` on `new URL(appBaseUrl()).hostname === "localhost"` would
enforce it (`TESTING.md §Assertion Discipline` "Skip only on an absent precondition" sanctions it).

**How to apply:** on any re-review of runtime-config or the auth ground specs, re-run the battery
above rather than trusting a report, and check whether the §Stack promotion has landed.
Related: [[e2e-testing-md-citations-resolved]].
