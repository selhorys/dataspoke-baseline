---
name: verify-branch-reachability-rationales
description: When a generator justifies an error branch with "a 401/error here means X", prove the route can even produce that status — auth is route-level Depends, so unauthenticated routes never 401
metadata:
  type: feedback
---

When a generator defends an error-handling branch with a reachability claim
("this branch is unreachable from caller Y", "a 401 here means the client already
tried to refresh"), derive the claim yourself instead of accepting the prose. Both
directions are wrong often enough to be worth the minutes: branches asserted
unreachable that are reachable, and branches written for statuses the route cannot
emit.

**Why:** In the issue-#59 revoke fix, two rationales failed under checking.
(1) The frontend carved out `err.status === 401` in `handleLogout` on the reasoning
that "a 401 means apiFetch already tried and failed to refresh" — but
`POST /auth/token/revoke` takes no auth dependency, so it can never return 401; the
branch was dead. DataSpoke validates JWT/role with **route-level `Depends`, not
global Starlette middleware** (`src/api/main.py` says so explicitly), so "is this
route authenticated?" is answered per-route, and an unauthenticated route's 401
branch is always dead. (2) Conversely, the backend claimed all of
`mark_refresh_revoked`'s no-op branches were unreachable from the refresh path;
the `ttl <= 0` branch is in fact reachable in a sub-second race, because PyJWT
validates `exp` against a *float* now while the TTL math floors via
`int(time.time())`. It was harmless, but for a different reason than the one given.

**How to apply:** For a status-code branch, grep the route for its auth dependency
before believing any "a 401/403 means…" story. For an "unreachable from X" claim,
compare the two validations line by line — a caller only makes a branch unreachable
if its checks are strictly stronger AND evaluated against the same clock/inputs.
Watch for float-vs-floor and re-read-after-check races between the guard and the
guarded call. A rationale that is accidentally right still warrants a finding: the
next editor will rely on the stated reason. See
[[verify-generator-dead-code-claims]] for the orphaned-export sibling of this.
