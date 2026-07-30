---
name: slowapi-blocking-storage-event-loop
description: slowapi's Redis storage is synchronous and blocks the whole ASGI loop; both planes now run the check via anyio.to_thread (auth plane fixed by pre-checking and setting request.state._rate_limiting_complete), verified with a heartbeat-tick probe rather than elapsed time
metadata:
  type: project
---

`slowapi` 0.1.10 + `limits` 5.8 use the **synchronous** `redis` client, and
stock slowapi runs the check inside the async endpoint wrapper
(`extension.py` `async_wrapper` → `_check_request_limit`) or inline in
`SlowAPIMiddleware.dispatch`, so it blocks the ASGI event loop — not just the
request. `limits` forwards `Limiter(storage_options=...)` to `redis.from_url`,
which is the only place a timeout can be set.

**The asymmetry that makes fail-closed dangerous:** slowapi sets its sticky
`_storage_dead` flag *only* inside `if self._in_memory_fallback_enabled and not
self._storage_dead`. A `Limiter(in_memory_fallback_enabled=False,
swallow_errors=False)` therefore **never** marks storage dead — no circuit
breaker — so every request re-attempts and pays the full timeout. DataSpoke
substitutes `AUTH_STORAGE_FAILURE_COOLDOWN_SECONDS = 5.0` fast-deny for that.

**Current state (both planes off-loop):**
- Both limiters pass `storage_options={socket_connect_timeout: 2.0,
  socket_timeout: 2.0}` (`RATE_LIMIT_STORAGE_TIMEOUT_SECONDS`).
- Default plane: `await to_thread.run_sync(limiter._check_request_limit, …)`
  in `RouteResolvingSlowAPIMiddleware.dispatch`.
- Auth plane: `auth_route_limit.guarded` pulls the `Request` out of the call,
  runs `to_thread.run_sync(auth_limiter._check_request_limit, request,
  exempted, False)` itself, then sets `request.state._rate_limiting_complete =
  True` so slowapi's `async_wrapper` skips its inline check
  (extension.py:732-734). Note it must pass the *same* function object slowapi
  registered the limit under (`exempted`), or the endpoint name misses
  `_route_limits`.

**How to verify — heartbeat ticks, never elapsed time.** Elapsed time is
identical on both paths (the request still waits); only tick count separates
them. Measured on this tree with a 1.5s synthetic stall on
`auth_limiter._check_request_limit`, one `POST /api/v1/auth/token`, and a 10 ms
heartbeat coroutine in the same loop (httpx `ASGITransport`, not `TestClient` —
`TestClient` runs the app in its own loop thread and the probe would measure
nothing): **128 ticks off-loop vs 2 ticks inline**, both at ~1517 ms wall.
A free loop ticks ~150.

Any diff that adds a `Limiter`, flips `in_memory_fallback_enabled`, or moves
routes between limiters must be re-checked for (a) `storage_options` timeouts,
(b) a fast-deny path, and (c) the check running **off** the loop. Note a
dedicated Redis logical DB does **not** protect counters from eviction:
`maxmemory-policy` is instance-wide and `allkeys-*` evicts across all DBs.

Related: [[default-rate-limit-plane-enforcement]],
[[auth-revoke-refresh-asymmetry]], [[auth-fail-closed-spans-layers]]
