---
name: fabricated-correlation-id
description: "Derive it the same way the middleware does" produces a DIFFERENT trace_id when the derivation ends in a fresh uuid4 — the handler's log line then correlates with nothing; drive a real request and diff the ids
metadata:
  type: feedback
---

When a handler-side log line is asked to carry a correlation id "derived exactly
the way the middleware derives it", check whether that derivation has a
*generative* fallback. If it ends in `str(uuid.uuid4())`, copying the derivation
copies the recipe, not the value — the two lines get two different ids.

**Why:** on #83 `_request_actor` in `src/api/routers/auth.py` was added so the
`oauth_route_refused` line would name the actor behind a refused OAuth bind. It
reproduced `RequestLoggingMiddleware`'s
`request.headers.get(_TRACE_HEADER) or str(uuid.uuid4())` verbatim. Browsers
cannot set custom headers on a full-page navigation and the chart injects no
`X-Trace-Id`, so on exactly these two routes the header is never present and both
sides always take the uuid4 branch. Driving the real app proved it: the
`request_started`/`request_finished` pair and the echoed response header carried
`b1a0eaba…` while `oauth_route_refused` carried `5b0aa0a6…`. A fabricated id is
worse than an absent one — it looks joinable, so an operator greps it and finds a
single orphan line. Note `src/api/main.py:_error_json` takes the opposite (and
safer) tack for the same header: absent ⇒ `""`.

**How to apply:** the fix shape is a request-scoped value the middleware
publishes (`request.state.trace_id` / a ContextVar) that handlers read, not a
re-derivation. Prove either way with a probe: build the app, drive the route
through `ASGITransport` with structlog rendering JSON to a buffer, and compare
the `trace_id` on the handler line against the middleware pair. Same class of
check applies to `client_ip`: under the chart default
`config.trustedProxyIps: "127.0.0.1"` uvicorn does not rewrite `scope["client"]`,
so it is the ingress pod's address and names no actor at all. Related:
[[verify-branch-reachability-rationales]], [[exc-info-leaks-bind-parameters]].
