---
name: auth-revoke-refresh-asymmetry
description: Revoke now validates the JWT before touching Redis (issue #59); the asymmetry inverted — /token/refresh still hits Redis BEFORE decoding, so garbage cookies reach Redis unvalidated
metadata:
  type: project
---

**Status (verified 2026-07-17, issue #59 re-review): the revoke half is fixed.**
`mark_refresh_revoked` now decodes first and only writes for a signature-valid,
`type=="refresh"`, unexpired token; everything else is a 204 no-op. The old
unauthenticated `jwt.DecodeError` → 500 is gone.

**The asymmetry inverted rather than disappeared.** `POST /auth/token/refresh`
(`src/api/routers/auth.py`) calls `is_refresh_revoked(redis, cookie)` **before**
`decode_refresh_token(cookie)`. So an unauthenticated garbage cookie still reaches
a real `redis.get` on the refresh path with zero JWT validation — the exact shape
revoke just fixed, now only on refresh. Neither route carries `@limiter.limit`
(unlike `/token`, `/register`, `/password/reset/*`).

**Why:** the ordering is deliberate and correct for auth (revocation must be
checked before minting), so it is not an auth bypass — it is a DoS amplifier:
during a Redis outage each sprayed cookie holds a request slot for the Redis
timeout (~20s), unauthenticated and unrate-limited.

**How to apply:** if the backlog item for rate-limiting these routes gets written,
frame it around **`/token/refresh`**, not revoke — revoke's cheap no-op path now
returns before any Redis call, so it is the weaker half. Do not re-file the
revoke-500 finding; it is closed.

Related: [[auth-fail-closed-spans-layers]], [[frontend-401-refresh-conflation]]
