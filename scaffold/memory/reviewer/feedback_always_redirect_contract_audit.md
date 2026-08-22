---
name: always-redirect-contract-audit
description: A route that promises "every outcome is a 302, never a JSON envelope" — enumerate the non-DataSpokeError raisers on its path (authlib OIDC discovery fetch, DB driver errors), not just the catalogued codes
metadata:
  type: feedback
---

When a spec says a route answers with a redirect on *every* outcome (or, generally,
that one response shape is universal), `except DataSpokeError` does not deliver it.
Walk the handler's call graph and list what else can raise.

**Why:** on #83 the two `/auth/google/*` routes became browser-redirect endpoints
and the implementation wrapped both bodies in `try/except DataSpokeError`, matching
the plan. Two live escapes remained. `authorize_redirect` is not local work:
authlib's `load_server_metadata` does an httpx GET of Google's
`.well-known/openid-configuration` with `raise_for_status()` on the *configured*
happy path, so a blocked egress or a Google blip raises `httpx.HTTPError` straight
out of `get_google_login`. And `src/api/main.py` registers no `Exception` handler,
so an escape is Starlette's plain-text 500 — exactly the stranded-browser defect the
feature existed to remove. The callback happened to be covered only because a
pre-existing broad `except Exception` around `authorize_access_token` re-labels
everything `OAUTH_STATE_MISMATCH`; after that point a Postgres outage
(`OperationalError` from the user lookup or the commit) escapes there too. Note the
asymmetry is invisible in review unless you read authlib: the two routes look like
they make the same call.

**How to apply:** for an "always X" contract, separate (a) errors the handler raises
deliberately — the catalogued codes, (b) third-party/library calls in the body, and
(c) driver/transport errors from any session or client the body touches. Only (a) is
covered by catching the project base exception. Prove (b) with a probe: monkeypatch
the library call to raise its native error and drive the route through
`ASGITransport` — if it propagates out of the client call, production returns a 500.
Middleware-plane rejections (rate limit) and dependency-resolution failures are
genuinely outside "what the handler produces" and belong in the spec's exception
list, not in the catch. Related: [[verify-branch-reachability-rationales]].
