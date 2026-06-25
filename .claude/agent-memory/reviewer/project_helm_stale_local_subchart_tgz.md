---
name: helm-stale-local-subchart-tgz
description: Umbrella helm template renders STALE local subchart tgz, not subcharts/ source; verify via standalone render
metadata:
  type: project
---

`helm-charts/dataspoke` consumes `charts/*.tgz`, not the `subcharts/{frontend,event-consumer}/` source. Those local `.tgz` are `.gitignore`d build artifacts and go stale after editing subchart templates.

**Why:** `helm template helm-charts/dataspoke` on a raw checkout renders the OLD subchart (e.g. a new `pdb.yaml` and `podAnnotations` edit in source were ABSENT from render because the tgz predated them). This silently makes a correct change look broken/missing in an umbrella render.

**How to apply:** When reviewing dataspoke frontend/event-consumer subchart changes, do NOT trust a raw `helm template helm-charts/dataspoke`. Validate via standalone `helm template helm-charts/dataspoke/subcharts/<name>` (and lint there), OR rebuild deps first. The deploy path is safe — `install.sh` `_build_chart_deps()` does `rm -f charts/frontend-*.tgz charts/event-consumer-*.tgz` then `helm dependency build`, so source edits DO ship. Langfuse wrapper `values.yaml` is read directly (no repackage needed). Related airflow gotcha: scheduler/triggerer/dagProcessor hardcode `safe-to-evict: "true"` gated on `*.safeToEvict` (default true) — adding podAnnotations "false" makes a duplicate key; set `*.safeToEvict: false` to suppress.
