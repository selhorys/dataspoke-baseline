---
name: peripheral-cache-multireplica
description: peripheral_config has a 30s module-level process cache and the API runs replicaCount 2 — read-modify-write counters computed off it can silently no-op
metadata:
  type: project
---

`get_peripheral_config()` (`src/backend/admin/peripheral_service.py`) serves from a
module-level dict with a 30s TTL, and `helm-charts/dataspoke/values.yaml` sets the API
`replicaCount: 2`. A write path that does *read → compute → write* against that cached
read is therefore not single-writer: replica B can hold a value replica A already
superseded.

**Why:** the DataHub Kafka work (issue #69) added `kafka_sasl_password_version`, a
counter the event consumer polls to detect a Secret-only rotation. It is computed as
`cached_version + 1`, so a rotation served by a replica with a stale cache writes the
same number back — the consumer's connection tuple is unchanged, it never rebuilds, and
it keeps authenticating with the rotated-away password. The same stale read also feeds
cross-field validation, so a partial PATCH can be judged against a tuple the DB no
longer holds and admit a state the rules forbid.

**How to apply:** whenever a router computes a value from `get_peripheral_config()` and
then writes it back, check for an `invalidate_peripheral_config_cache(name)` immediately
before the read, or push the read-modify-write into the service so it happens on the row
inside one transaction. `_apply_datahub_patch_and_respond` already calls the invalidator
on its no-DB-write branch, so the absence of one before the `current` read is a smell,
not a deliberate choice. Unit tests mock the session and never exercise two processes —
this class of bug only shows up in api-wired or in prod.
