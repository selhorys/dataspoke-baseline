---
name: auth-fail-closed-spans-layers
description: Fail-closed auth fixes must be reviewed end-to-end — a correct backend 503 is undone if the frontend consumer swallows the error (issue #59: both layers now verified fixed)
metadata:
  type: project
---

A backend fail-closed fix on an auth/session route is only half the control. Review the
**client consumer** in the same pass, or the fail-open just moves up a layer.

**Verified precedent (issue #59, closed 2026-07-17):** the backend correctly raised
`StorageUnavailableError` → 503 and retained the refresh cookie, but the UI logout
handler wrapped the call in `try/catch{}` + `finally { clear(); redirect("/login") }`,
so the user was told logout succeeded while the refresh cookie stayed valid *and* now
stayed in the browser. Net posture on a shared machine was worse than the fail-open it
replaced. Grading only the router diff scored PASS on a control that did not hold.

**Why:** the invariant the spec cares about (`spec/feature/AUTH.md §Refresh & revoke`,
Failure Modes table) is user-visible — "user must learn the token is still valid". That
is an end-to-end property, not an HTTP-status property.

**How to apply:** when a diff makes an auth/session route fail closed, grep the frontend
(`src/frontend/`) for the route and check the error path actually surfaces. Report it as
a finding against the change even though the offending file belongs to the `frontend`
generator — the orchestrator merges findings and can dispatch the fix pass. Same check
applies to future fail-closed work on refresh, password reset, or API-token revocation.
When checking the UI half, `[[frontend-401-refresh-conflation]]` documents the specific
false premise these handlers tend to reach for.

Related: [[auth-revoke-refresh-asymmetry]], [[frontend-401-refresh-conflation]]
