---
name: redis-single-failure-domain
description: One Redis instance carries auth revocation keys, distributed locks and the rate-limiter counters; maxmemory is instance-wide so DB1 exhaustion breaks DB0 — and the unbounded tenant is the attacker-keyable limiter key space, not the response cache the chart rationale names
metadata:
  type: project
---

The chart ships **one** Redis (`redis.master` + one replica) shared by three
tenants across two logical DBs:

| Tenant | DB | Bound | Reachable by |
|---|---|---|---|
| refresh-token revocation (`src/backend/auth/tokens.py`) | 0 | key = truncated SHA-256, TTL = remaining refresh lifetime | authenticated |
| distributed locks (`src/shared/cache/client.py` `SET NX`) | 0 | TTL 300–3600 s | authenticated |
| response cache (`quality:{urn}:score`, `validation:{urn}:result`) | 0 | **bounded by dataset count** | authenticated |
| slowapi counters (`RATE_LIMIT_REDIS_DB = 1`) | 1 | fixed-window, ~200 B/key, one key per distinct caller key per minute | **unauthenticated** |

`redis.commonConfiguration` pins `maxmemory 256mb` + `maxmemory-policy
noeviction`. `noeviction` is the correct call — any LRU/LFU policy drops
revocation and lock keys silently and *correctly* by Redis's own logic, and a
dropped revocation key un-revokes a refresh token. `maxmemory` is **instance-
wide**; there is no per-logical-DB bound, so a DB 1 flood makes DB 0 writes
fail.

**Both `helm-charts/dataspoke/values.yaml` and `spec/feature/HELM_CHART.md`
§Redis memory policy now state this correctly** (the spec's earlier inversion —
naming the response cache as the unbounded tenant — is fixed). They diverge on a
*different* point: the chart comment says the sub-limit `maxmemory` is
"headroom, not a safety guarantee" and names two residual risks, while the spec
still asserts the gap is sized so "a full resync cannot close the distance" and
never mentions the BGREWRITEAOF fork.** The response cache is keyed per dataset URN and needs auth.
The unbounded tenant is DB 1: `rate_limit.py` `_get_user_key` returns
`"pat:" + sha256(bearer)[:32]` for any `Bearer dsk_…` **without verifying the
token exists**, so a fresh random token mints a fresh Redis key per request from
an unauthenticated caller. Downstream of exhaustion: `mark_refresh_revoked` /
`is_refresh_revoked` raise `StorageUnavailableError` → 503 on logout/refresh for
everyone (fail-closed), locks fail, and the default limiter
(`in_memory_fallback_enabled=True`) degrades **silently** to per-process
counting. Practical cost is high (order 10^6 keys/min) but the ceiling is now
deterministic at 256 MB rather than the 512Mi cgroup.

**How to apply:** any diff touching `redis.commonConfiguration`, `maxmemory`,
`RATE_LIMIT_REDIS_DB`, or the limiter key function must be judged against the
whole instance, never one DB. `commonConfiguration` is a plain **scalar** in the
Bitnami chart — setting it replaces the upstream default, so the two
`loadmodule` lines, `appendonly yes` and `save ""` must be carried forward
verbatim or AOF durability dies silently (verified present in the rendered
`d-redis-configuration` ConfigMap). Two tails the chart comment's arithmetic
omits: a `BGREWRITEAOF` fork's copy-on-write can push RSS well past the
`maxmemory` figure inside the same 512Mi cgroup, and tightening
`client-output-buffer-limit replica` to `64mb 32mb 60` trades an OOMKill for a
master-initiated replica disconnect / full-resync loop if writes during a
resync exceed the cap. The app connects to `dataspoke-redis-master` only, so
the second tail costs standby durability, not the request path. AOF at rest is safe: revocation keys
store a truncated SHA-256, not the raw token.

Related: [[default-rate-limit-plane-enforcement]],
[[slowapi-blocking-storage-event-loop]], [[auth-revoke-refresh-asymmetry]]
