---
name: prod-example-not-superset
description: helm-charts/values-prod.example.yaml only carries the keys an operator must edit, so "see config.X below" comments copied from dataspoke/values.yaml go dangling there — and its above/below direction words go stale whenever a new section is inserted
metadata:
  type: feedback
---

When a chart change adds a `config.*` tunable, the comment block usually gets pasted into
both `helm-charts/dataspoke/values.yaml` and `helm-charts/values-prod.example.yaml`. The
example overlay is **not** a superset of the base values — it lists only the keys a prod
operator is expected to edit. So any "see `config.X` below / above" cross-reference that is
valid in `values.yaml` is dangling in the overlay unless `config.X` also happens to be there.

**Why:** the `config.rateLimitPerMinute` addition pasted "see config.trustedProxyIps below"
into `values-prod.example.yaml`, where `trustedProxyIps` appears nowhere else in the file.
Worse than a broken pointer: `trustedProxyIps` is the setting that decides whether the
newly-enforced limit is per-client or one global bucket for all unauthenticated traffic, and
the overlay silently omits it.

**Direction words rot on insertion.** This file's comments navigate by "the … section
below/above", and every new `# ---` section shifts them. Inserting the StorageClass section
ahead of the Airflow log-persistence section left that section's "the `storageClass` used by
the postgresql/redis blocks **below**" pointing backwards at the block now directly above it.

**How to apply:** for every new comment in `values-prod.example.yaml`, grep that same file
for each key it names. If the referenced key is absent, either add it (commented out, with
the chart default) or reword the pointer to name `dataspoke/values.yaml` explicitly. When a
section is inserted, grep the whole file for `below`/`above` and re-check each one against the
new ordering. Same check applies to `helm-charts/README.md`'s prod runbook, which enumerates
prerequisites rather than every tunable. Related: [[helm-null-and-replicas-gotchas]].

**Line-number cross-references rot exactly like the direction words.**
`helm-charts/bin/install.sh:859` cites `values-prod.example.yaml:340-349` for
the Airflow log-persistence block; a docs stage that inserted a sizing section
and a StorageClass section above it left that range naming the unrelated
`pullPolicy` section, with the real block ~160 lines lower. Grep recipe after
any insertion into a doc other files cite:
`grep -rn "values-prod.example.yaml:[0-9]\|install.sh:[0-9]\|values.yaml:[0-9]" helm-charts/ spec/`
Prefer a section-name reference over a line range when suggesting the fix.

**The `config:` block is the recurring dangling target.** `values-prod.example.yaml` has a
`config:` block, but it carries only `corsOrigins`, `oauthPostLoginRedirect`,
`rateLimitPerMinute` and `trustedProxyIps` — no `postgres:`. So a comment (or an `install.sh`
error message) telling the operator to "set `config.postgres.{user,db}` in your overlay — see
values-prod.example.yaml" points at a file where that key does not exist. Grep
`grep -n '^config:' -A 40 helm-charts/values-prod.example.yaml` before accepting any new
`config.X` pointer.
