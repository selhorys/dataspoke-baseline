---
name: consumer-db-plane-to-wire-boundary
description: peripheral_config JSONB is a trust boundary on READ; the DataHub event consumer now re-validates via the shared kafka_security rule engine — check any new reader follows the same pattern
metadata:
  type: project
---

`peripheral_config.settings` is untyped JSONB. The project convention (stated verbatim in
`src/api/routers/spoke/common/peripheral_links.py`) is that a row "written by direct SQL or
by a caller that bypasses the admin request schema" must be re-checked **on the way out**,
not only on PATCH.

**Current state (verified 2026-07-21, issue #69):** the DataHub Kafka path now follows it.
`src/shared/datahub/kafka_security.py` holds one predicate (`check_kafka_security`) called
from two enforcement points — `src/api/schemas/admin.py` (→ 422) and
`build_consumer_config` in `src/shared/datahub/consumer.py` (→ `KafkaConfigurationError`).
`Consumer(...)` is constructed at exactly one site, fed only by `build_consumer_config`, so
there is no unvalidated path to a client. The module imports only stdlib — the consumer
process pulls in zero `src.api` modules (verified by `sys.modules` after import).

**Why this table is high-stakes:** its values decide (a) whether SASL credentials go over
TLS or cleartext and (b) which host receives a SigV4 MSK IAM token minted from the pod's
IRSA role. That last one is privilege escalation from app-Admin to cloud IAM, not just a
misconfiguration.

**Known residual:** rule 6 allowlists all of `*.amazonaws.com`, which includes hostnames an
attacker can provision (EC2 public DNS, `ec2-<ip>.compute-1.amazonaws.com`). The tight MSK
shape is already expressed two lines away in `_MSK_REGION_RE`. Also: PATCH validates
outside the `FOR UPDATE` used for the version bump, so concurrent PATCHes can merge into an
invalid row — contained because the consumer's predicate is a superset of every
DB-observable rule.

**How to apply:** when reviewing anything that reads `peripheral_config`, ask whether the
reader re-validates. "The admin API validates it" is insufficient for this table. When
reviewing a *host allowlist*, check whether the allowed suffix contains names the attacker
can obtain — a suffix match on a cloud provider's shared domain usually does. Related:
[[peripheral-config-to-href-trust-chain]], [[pydantic-v2-pattern-anchoring]].

Sensitive path not in the agent's glob list: `src/backend/admin/peripheral_health.py`
(operator-facing error strings persisted and served over the admin API).
