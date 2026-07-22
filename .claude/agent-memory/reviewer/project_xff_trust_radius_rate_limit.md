---
name: xff-trust-radius-rate-limit
description: FORWARDED_ALLOW_IPS trusts XFF *chain entries*, not just the peer — any caller sourced from a trusted range can forge the rate-limit bucket key, which is why the default is loopback-only and per-client bucketing is opt-in
metadata:
  type: project
---

`config.trustedProxyIps` → `FORWARDED_ALLOW_IPS` (issue #76) defaults to
`127.0.0.1` — loopback only, trusting no proxy. Per-client bucketing is opt-in:
the operator names their ingress controller's pod CIDR. Three non-obvious
consequences that any review of this area must check:

1. **The trust list is applied to every hop in `X-Forwarded-For`, not just the
   immediate peer.** `_TrustedHosts.get_trusted_client_address` walks the chain
   in reverse and returns the first *untrusted* entry. So any caller sourced
   from a trusted range can send `X-Forwarded-For: <anything>` and have that
   value become `request.client.host`, i.e. choose its own rate-limit bucket —
   and rotate it per request, defeating the limit entirely. This is why a
   private-range envelope is not a safe default: it would admit every in-cluster
   pod, plus VPC-CNI EC2 hosts, VPN clients, and peered networks. `POST
   /auth/token` has no account-lockout fallback, so the limiter is the only
   brute-force control.
2. **Setting the value alone does not deliver per-client bucketing.** Every hop
   in front must also preserve the client address. The dev managed-mode
   nginx-ingress Service is `type: LoadBalancer` with no `externalTrafficPolicy`,
   so it defaults to `Cluster` and kube-proxy masquerades the source to a node
   IP. Prod behind an L7 LB needs ingress-nginx `use-forwarded-headers` too
   (default `false` discards the incoming header).
3. **The value is not a rate-limiting-only knob.** The same trust gate governs
   `X-Forwarded-Proto` (`proxy_headers.py:43-50`), and `get_google_login` derives
   the OAuth `redirect_uri` from the request scheme (`src/api/routers/auth.py`),
   so widening it flips a deployment's generated redirect from `http://` to
   `https://` and breaks Google OAuth until the console entry matches.

Empty and `"*"` are rejected at render time by a `fail` guard in
`templates/configmap.yaml`. Without it, `FORWARDED_ALLOW_IPS: ""` parses to a
single literal `''` that matches nothing, so XFF is ignored and the original
single-bucket bug returns with no error and no log. Note the guard does *not*
catch an over-broad range — only the chart default and review do.

**Why:** the whole fix is one config line whose correctness is invisible; both
facts were missed or understated in the first pass.
**How to apply:** when reviewing anything touching rate limiting, client-IP
attribution, or ingress values, verify against `uvicorn/middleware/proxy_headers.py`
and the actual ingress Service spec — don't accept "we set the trusted proxies"
as proof the bug is fixed. Related: [[helm-null-and-replicas-gotchas]].
