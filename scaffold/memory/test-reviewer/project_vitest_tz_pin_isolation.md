---
name: vitest-tz-pin-isolation
description: Why a process.env.TZ pin inside one Vitest describe is safe in src/frontend, and which restore branch is load-bearing — measured, not assumed
metadata:
  type: project
---

`src/frontend/vitest.config.mts` sets no `pool` / `isolate` / `fileParallelism`, so Vitest 4
defaults apply: **`pool: "forks"`, `isolate: true`**. Each test file therefore runs in its own
forked child process with its own `process.env` copy — a `process.env.TZ` pin in one file
**cannot** reach another file. Within a file, suites run in declaration order, sequentially.

**Why:** cycle-2 review of `lib/chart-grain.test.ts` (#127) had to decide whether a
`beforeAll` TZ pin (`Asia/Seoul`) leaks. It does not, but the reasoning is invisible from the
test file alone and gets re-litigated every time someone pins a zone.

**How to apply** when auditing a TZ/locale/global pin in this suite:

- Collection-time constants inside a `describe` callback (e.g. `const saved = process.env.TZ`)
  are evaluated *before* any `beforeAll`, so they capture the host value correctly. Constants
  built from `Date.UTC(...)` + `.toISOString()` are zone-independent and immune either way.
  The trap is a collection-time constant built from **local** getters — that one must be a
  factory function called inside the test.
- The restore MUST be `if (saved === undefined) delete process.env.TZ; else process.env.TZ = saved;`.
  Assigning `undefined` stores the literal string `"undefined"`, which Node treats as an
  invalid zone and silently falls back to UTC.
- **Measured**: the naive `process.env.TZ = saved` restore fails *only* when the host has no
  `TZ` set (common in CI containers and on this dev host) — and then only via the block's own
  `afterAll` self-check (`expect(process.env.TZ).toBe(saved)` + a `getTimezoneOffset()`
  backstop captured at import). Under `TZ=Asia/Seoul` / `America/New_York` / `UTC` the naive
  restore is indistinguishable. So the self-check is the *only* guard — do not accept a TZ pin
  whose `afterAll` lacks one.
- Verify empirically, cheaply: `TZ=<zone> pnpm -C src/frontend test <file>` across
  `UTC`, `America/New_York`, `Pacific/Kiritimati` (+14), `Pacific/Midway` (-11),
  `Australia/Lord_Howe` (half-hour + DST), and once with `env -u TZ`.

Related: [[chart-grain-local-monotonicity]].
