---
name: status-change-silent-skip-and-catalogue
description: A status-code contract change goes green two ways it should not — an integration test whose skip guard keys on the OLD status skips instead of failing, and src/shared/exceptions.py's class-docstring catalogue keeps naming the retired status
metadata:
  type: feedback
---

When a change moves a route from one response shape to another (503 envelope →
302 redirect, 409 → 302, 200 → 204), two consumers go stale *quietly*. Neither
shows up as a failing suite.

**Why:** on #83 the two `/auth/google/*` routes stopped answering `503
OAUTH_NOT_CONFIGURED` and started answering `302 /oauth-error?error=…`.

1. `tests/integration/spot/test_auth_oauth_disabled.py` guards both tests with
   `_oauth_is_configured()`, which probes the login route and returns
   `resp.status_code != 503` — i.e. "not 503 ⇒ credentials are provisioned".
   After the change the unconfigured route returns 302, so the guard reads
   *configured*, both tests `pytest.skip` with the message "OAuth credentials
   are provisioned in this dev install", and the coverage for the contract
   retires itself. The suite stays green; nothing is red to prompt a rewrite.
2. `src/shared/exceptions.py` keeps its code↔status catalogue in the *class
   docstrings* ("Raised when an operation conflicts with current state (HTTP
   409)" over a list of codes), and its module docstring declares that catalogue
   authoritative and complete. `ConflictError` still headed "HTTP 409" while
   `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` and `GOOGLE_ACCOUNT_LINKED_ELSEWHERE`
   — whose only raise path is the callback — can no longer be a 409 at all.
   Grepping the *code name* finds the line; grepping `409` or the route path
   does not, which is why the same pass fixed `oauth_google.py` and `BACKEND.md`
   and missed this one. `BadRequestError`'s list also omits the two codes the
   callback actually raises through it.

**How to apply:** for any status/shape change, run two extra greps beyond the
handler's own call sites — (a) `skip|xfail|if resp.status_code` across
`tests/integration/` for a guard that encodes the old status as a *condition*,
and (b) the affected error codes' names in `src/shared/exceptions.py`, then read
the enclosing class docstring header for its HTTP claim. Prove (a) by asking
"what does this guard now evaluate to?", not "does this file still pass?".
Related: [[grep-old-rule-prose-in-consumers]], [[verify-branch-reachability-rationales]].
