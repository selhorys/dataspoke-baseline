---
name: prod-example-not-superset
description: helm-charts/values-prod.example.yaml only carries the keys an operator must edit, so "see config.X below" comments copied from dataspoke/values.yaml go dangling there
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

**How to apply:** for every new comment in `values-prod.example.yaml`, grep that same file
for each key it names. If the referenced key is absent, either add it (commented out, with
the chart default) or reword the pointer to name `dataspoke/values.yaml` explicitly. Same
check applies to `helm-charts/README.md`'s prod runbook, which enumerates prerequisites
rather than every tunable. Related: [[project_helm_null_and_replicas_gotchas]].
