---
name: next-nonpublic-env-client-bundle
description: Verified facts about reading non-NEXT_PUBLIC env vars from a client-bundled Next.js module — no inlining, no ReferenceError — plus the canary/SSR verification recipe
metadata:
  type: project
---

`src/frontend/lib/runtime-config.ts` reads `process.env.DATASPOKE_API_BASE_URL` /
`DATASPOKE_AIRFLOW_URL` from a module that is bundled into client chunks. Three facts
verified against Next.js 15.5.21 in this repo (not assumptions):

1. **No inlining.** Next substitutes values only for `NEXT_PUBLIC_*`; `next.config.ts`
   declares no `env` block. `DATASPOKE_API_BASE_URL=http://leak-canary.invalid pnpm -C
   src/frontend build` then `grep -rl "leak-canary" src/frontend/.next/` → no matches.
2. **No ReferenceError in the browser.** The chunk compiles to
   `apiBaseUrl:r.env.DATASPOKE_API_BASE_URL||"…"` where `r = n(31129)` is Next's `process`
   shim (`r.g.process` else polyfill module 7558). So the expression yields `undefined`,
   not a crash, if that branch is ever reached client-side (only when the layout's inline
   `window.__DATASPOKE_RUNTIME_CONFIG__` script did not run, e.g. blocked by CSP).
3. **Nothing bakes at build time.** `app/layout.tsx` has `export const dynamic =
   "force-dynamic"`, so every route — including `/_not-found` — builds as `ƒ (Dynamic)`.
   Check the build's route table for a stray `○ (Static)` before trusting this.

**Why:** issue #129 (SSR Google sign-in href rendered relative and 404'd in the container).
Any future review of "is it safe to read a server-only env var here?" hits the same three
questions.

**How to apply:** end-to-end proof, no Kubernetes needed —
`DATASPOKE_API_BASE_URL=http://api.example.test pnpm exec next start -p 3111` then
`curl -s localhost:3111/login | grep -o 'href="[^"]*google[^"]*"'` must show the absolute
URL. Note `next start`/`next build` load `src/frontend/.env.local` (NEXT_PUBLIC_* only), so
a local build is not a faithful prod bundle; `src/frontend/.dockerignore` excludes `.env*`.
Prettier is not enforced here (158 files already fail `--check`, no `format` script, no CI),
so formatting drift is not a finding. Related: [[verify-branch-reachability-rationales]].
