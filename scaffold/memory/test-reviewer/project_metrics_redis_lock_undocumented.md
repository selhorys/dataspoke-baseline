---
name: metrics-redis-lock-undocumented
description: MetricsService.run uses a metrics:running:{metric_id} Redis SET NX lock that BACKEND.md §Concurrency Guards omits
metadata:
  type: project
---

`src/backend/metrics/service.py` `MetricsService.run` acquires a direct-execution Redis
SET NX lock `metrics:running:{metric_id}` (token-CAS release via `delete_if_value`), but
`spec/feature/BACKEND.md §Concurrency Guards` SET NX table (headed "for direct-execution
flows") lists ONLY `ingestion:running:{dataset_urn}`, `ontogen:running:singleton`,
`metagen:running:{conf_id}`. Metrics appears there only under **Airflow conf-based dedup**
(`metrics-{metric_id}`). The `409 METRIC_RUNNING` error code IS spec-documented, so the 409
contract is traceable; the Redis-key + lock lifecycle is impl-derived.

**Why:** A metrics `test_run_lock.py` citing §Concurrency Guards for the Redis lock overstates
the spec basis — the key format isn't in the cited table. Structural spec gap, not a passing bug.

**How to apply:** When reviewing metrics-lock tests, treat the `metrics:running:{metric_id}`
key + release-on-both-outcomes as impl-derived (still valuable — leak prevention), but the
409 METRIC_RUNNING duplicate-run assertion as spec-traceable. Recommend the spec list the
metrics direct-execution lock in the SET NX table, or the test acknowledge it as an impl
detail. Related: [[recipe-mask-string-divergence]], [[validation-no-status-aspect-divergence]].
