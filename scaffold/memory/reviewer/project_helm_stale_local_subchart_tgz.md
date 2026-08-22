---
name: helm-stale-local-subchart-tgz
description: Umbrella helm template/upgrade renders STALE charts/*.tgz, not subcharts/ source — repackage before trusting any umbrella render
metadata:
  type: project
---

`helm-charts/dataspoke` consumes `charts/*.tgz`, not the `subcharts/{frontend,event-consumer}/` source. Those local `.tgz` are `.gitignore`d build artifacts and go stale after editing subchart templates.

**Why:** `helm template helm-charts/dataspoke` on a raw checkout renders the OLD subchart (re-verified 2026-07-20 by appending a marker to `subcharts/event-consumer/templates/deployment.yaml` — the marker was absent from the umbrella render). This silently makes a correct change look broken/missing.

**How to apply:** When reviewing dataspoke frontend/event-consumer subchart changes, do NOT trust a raw `helm template helm-charts/dataspoke`. Either render standalone (`helm template helm-charts/dataspoke/subcharts/<name>`), or extract `charts/<name>-*.tgz` and `diff -r` it against the source tree to check freshness first — right after a full install the tgz IS current, so an umbrella render is valid in that window only.

**The deploy path repackages, so review renders must too.** `_build_chart_deps()` (`rm -f charts/{frontend,event-consumer}-*.tgz` + `helm dependency build`) runs in all four deploy paths: the full dev install, the `--components api` fast path, the `--components frontend` fast path, and prod (re-verified 2026-08-01). So the cluster gets fresh subchart templates — but a reviewer rendering a raw checkout does not. Recipe: copy the chart to a scratch dir, `rm -f charts/{frontend,event-consumer}-*.tgz`, `helm package subcharts/<name> -d charts/`, then `helm template`.

Related airflow gotcha: scheduler/triggerer/dagProcessor hardcode `safe-to-evict: "true"` gated on `*.safeToEvict` (default true) — adding podAnnotations "false" makes a duplicate key; set `*.safeToEvict: false` to suppress. See also [[helm-null-and-replicas-gotchas]].
