---
name: vitest-tz-pin-isolation
description: Why a process.env.TZ pin inside one Vitest describe is safe in src/frontend, and which restore branch is load-bearing. Promoted to spec/TESTING.md §Unit Testing → TypeScript (Frontend).
metadata:
  type: project
---

Now documented in `spec/TESTING.md` §Unit Testing → TypeScript (Frontend).

**Measured detail kept for reference:** the naive `process.env.TZ = saved` restore fails only when
the host has no `TZ` set (common in CI containers and this dev host); verified empirically across
`UTC`, `America/New_York`, `Pacific/Kiritimati` (+14), `Pacific/Midway` (-11),
`Australia/Lord_Howe` (half-hour + DST), and `env -u TZ`.

Related: [[chart-grain-local-monotonicity]].
