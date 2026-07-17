---
name: frontend-401-refresh-conflation
description: apiFetch's refreshed===false conflates "token invalid" with "Redis down 503" and its refresh branch is gated on `&& token` — any UI "401 means no live session" carve-out is unsound
metadata:
  type: project
---

Two properties of `src/frontend/lib/api/client.ts` make the inference
**"an ApiError 401 means there is no live session"** false. Any UI error handler
that carves out 401 to skip a fail-closed path is relying on that false premise.

1. The 401→refresh branch is gated on `&& token`. With no access token in the
   Zustand store, a 401 throws straight through with **no refresh attempted**.
2. `ensureFreshToken()` returns `false` for *any* non-ok refresh response —
   including **503** and network errors, not just 401. During a Redis outage
   `/auth/token/refresh` returns 503 (`is_refresh_revoked` fails closed), so
   `refreshed === false` while the refresh cookie is **perfectly live**. The
   inference is exactly backwards in the outage the fail-closed exists for.

**Why:** issue #59's `handleLogout` fix (correctly) fails closed on revoke errors
but carves out `err.status === 401` → `clear() + replace("/login")`. That carve-out
is *currently unreachable* — `/auth/token/revoke` has no auth dependency and was
verified to return only 204/503/422, never 401 — so it is dead code, not a live
bug. It is armed, though: adding `Depends(require_authenticated)` to revoke (a
plausible hardening, and something a generic "every route needs auth" review would
ask for) makes it reachable and re-opens the kiosk fail-open.

**How to apply:** on any auth/session diff, check whether revoke gained an auth
dependency — if it did, the carve-out must go in the same change. More generally,
reject "401 ⇒ no live session" reasoning in frontend error handlers; the sound
test is an explicit success signal, not the absence of one.

Related: [[auth-fail-closed-spans-layers]], [[auth-revoke-refresh-asymmetry]]
