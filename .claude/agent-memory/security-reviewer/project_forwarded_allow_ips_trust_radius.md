---
name: forwarded-allow-ips-trust-radius
description: config.trustedProxyIps -> FORWARDED_ALLOW_IPS is a network-reachability envelope, not a proxy list; it governs BOTH X-Forwarded-For (rate-limit bucket key) and X-Forwarded-Proto (OAuth redirect_uri scheme), and the chart's fail guard blocks only "" and literal "*" while 0.0.0.0/0 renders
metadata:
  type: project
---

`config.trustedProxyIps` (chart) -> `FORWARDED_ALLOW_IPS` (ConfigMap) -> uvicorn
`ProxyHeadersMiddleware`. Three non-obvious properties, all easy to miss when
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
3. **The render guard is a two-literal blocklist, not a semantic check.**
   `templates/configmap.yaml` `fail`s only on `""` and the literal `"*"`.
   Verified against uvicorn 0.49.0: `_TrustedHosts("0.0.0.0/0")` returns
   `True` for `1.2.3.4`, i.e. `0.0.0.0/0` (and `::/0`, and the full RFC1918
   set) renders happily and is *semantically identical to the `"*"` the guard
   rejects*. Read the guard's blocklist, not its prose, before accepting "the
   chart prevents an over-broad trust list".
   `helm-charts/values-prod.example.yaml` did omit the key entirely until the
   #111 run added a commented `trustedProxyIps` entry with the condensed
   hazard note — prod still *inherits* the loopback-only default (single-bucket
   mode) unless the operator uncomments it. Grep the example overlay, not the
   base values, when judging "the default is documented".

**Why:** the rate limiter (`src/api/middleware/rate_limit.py`) keys unauthenticated
traffic on the client IP, and there is **no account lockout and no failed-attempt
counter anywhere in `src/backend/auth/`** — the 10/min on `POST /auth/token` is the
*sole* brute-force control. With the loopback default, `client.host` is the
nginx-ingress pod IP for every external caller, so that 10/min is one bucket for
the whole deployment (`/auth/token` is a fixed path, so the URL-keyed bucketing in
[[default-rate-limit-plane-enforcement]] does not split it). Conversely, anyone
who *can* choose their bucket key has unlimited password guesses.
`POST /auth/token/refresh` also hits Redis before decoding the cookie (see
[[auth-revoke-refresh-asymmetry]]), so bucket rotation is a Redis-load amplifier
too.

**How to apply:** when this value (or the ingress path in front of it) changes,
check the *reachability* set, not the proxy set: the chart's only NetworkPolicy is
egress-only and `networkPolicy.enabled` defaults to `false`, so nothing restricts
pod-to-pod access to the API on :8002. Widening the list without an ingress-side
NetworkPolicy trades an internet-facing global-bucket DoS for adjacent-network
brute force. Empty / malformed / `"*"` values all degrade silently — uvicorn folds
malformed entries into `trusted_literals` and never warns.
Related: [[default-rate-limit-plane-enforcement]],
[[operator-runbook-is-credential-surface]],
[[credentials-secret-contract-key-addition]].
