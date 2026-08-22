---
name: auth-revoke-204-vs-401-unspecced
description: /auth/token/revoke 204 for anonymous/garbage cookies is an impl choice, not spec; API.md L151 groups revoke with refresh as "require an authenticated caller" yet refresh 401s
metadata:
  type: project
---

`POST /auth/token/revoke` has **no** `Depends(require_authenticated)` (unlike `/auth/me`),
and there is no auth middleware (`src/api/middleware/` holds only logging + rate_limit).
So an anonymous caller with no cookie, or any garbage cookie, gets **204**.

But `spec/API.md` L151 says: "`/auth/me`, `/auth/api-tokens`, `/auth/token/refresh`,
`/auth/token/revoke` require an authenticated caller". Its sibling `/auth/token/refresh`
raises `AuthenticationError` (401) on a missing cookie. Revoke does not — the asymmetry is real.

`spec/feature/AUTH.md` §Refresh & revoke states only the happy path plus the Redis
fail-closed rule; §Failure Modes (L516) lists **only** "Redis unreachable" for revoke.
There is no positive rule for unauthenticated / undecodable-cookie callers.

**Why:** the issue-#59 test pass added 6 cases pinning 204-for-unrevocable-cookie, justified
by "AUTH.md §Failure Modes lists no client-input failure for revoke" — an argument from
*silence/exhaustiveness*, not a positive rule. That table is a list of notable modes, not a
closed enumeration, and the inference is in tension with API.md L151 (a priority-1 doc).

**How to apply:** on any future auth/revoke test diff, do not accept "the spec doesn't forbid
it" as spec traceability. Until AUTH.md gains an explicit rule, treat 204-vs-401 for
anonymous/garbage revoke as an **open spec question**; flag tests pinning either value hard.
Related: [[auth-revoke-503-cookie-retention-framework-guaranteed]].
