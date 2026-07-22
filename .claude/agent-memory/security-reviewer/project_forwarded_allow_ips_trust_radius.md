---
name: forwarded-allow-ips-trust-radius
description: config.trustedProxyIps -> FORWARDED_ALLOW_IPS is a network-reachability envelope, not a proxy list; it governs BOTH X-Forwarded-For (rate-limit bucket key) and X-Forwarded-Proto (OAuth redirect_uri scheme)
metadata:
  type: project
---

`config.trustedProxyIps` (chart) -> `FORWARDED_ALLOW_IPS` (ConfigMap) -> uvicorn
`ProxyHeadersMiddleware`. Two non-obvious properties, both easy to miss when
reviewing a change to this value:

1. **The list is applied to every hop, not just the immediate peer.**
   `_TrustedHosts.get_trusted_client_address` walks `X-Forwarded-For` in reverse
   and returns the first *untrusted* entry; the gate
   `if client_host in self.trusted_hosts` is satisfied by *any* peer in the list.
   So "trusted proxies" really means "callers permitted to choose their own
   client IP". `_parse_host_port` returns unrecognized values verbatim, so the
   result is not even required to be an IP.
2. **The same gate controls `X-Forwarded-Proto`.** It sets `scope["scheme"]`,
   which `request.url_for` uses — and `src/api/routers/auth.py` builds the Google
   OAuth `redirect_uri` that way. Widening the list flips prod's redirect_uri
   from `http://` to `https://`. Any diff framed as "X-Forwarded-For only" is
   under-describing itself.

**Why:** the rate limiter (`src/api/middleware/rate_limit.py`) keys unauthenticated
traffic on the client IP, and there is **no account lockout and no failed-attempt
counter anywhere in `src/backend/auth/`** — the 10/min on `POST /auth/token` is the
*sole* brute-force control. Anyone who can choose their bucket key has unlimited
password guesses. `POST /auth/token/refresh` also hits Redis before decoding the
cookie (see [[auth-revoke-refresh-asymmetry]]), so bucket rotation is a Redis-load
amplifier too.

**How to apply:** when this value (or the ingress path in front of it) changes,
check the *reachability* set, not the proxy set: the chart's only NetworkPolicy is
egress-only and `networkPolicy.enabled` defaults to `false`, so nothing restricts
pod-to-pod access to the API on :8002. Widening the list without an ingress-side
NetworkPolicy trades an internet-facing global-bucket DoS for adjacent-network
brute force. Empty / malformed / `"*"` values all degrade silently — uvicorn folds
malformed entries into `trusted_literals` and never warns.
