---
name: helm-stale-local-subchart-tgz
description: Umbrella helm template/upgrade renders STALE charts/*.tgz, not subcharts/ source; --components api does NOT rebuild deps
metadata:
  type: project
---

`helm-charts/dataspoke` consumes `charts/*.tgz`, not the `subcharts/{frontend,event-consumer}/` source. Those local `.tgz` are `.gitignore`d build artifacts and go stale after editing subchart templates.

**Why:** `helm template helm-charts/dataspoke` on a raw checkout renders the OLD subchart (re-verified 2026-07-20 by appending a marker to `subcharts/event-consumer/templates/deployment.yaml` — the marker was absent from the umbrella render). This silently makes a correct change look broken/missing.

**How to apply:** When reviewing dataspoke frontend/event-consumer subchart changes, do NOT trust a raw `helm template helm-charts/dataspoke`. Either render standalone (`helm template helm-charts/dataspoke/subcharts/<name>`), or extract `charts/<name>-*.tgz` and `diff -r` it against the source tree to check freshness first — right after a full install the tgz IS current, so an umbrella render is valid in that window only.

**The deploy path is only partly safe.** `_build_chart_deps()` (`rm -f charts/{frontend,event-consumer}-*.tgz` + `helm dependency build`) runs in exactly three places: the full dev install, the `--components frontend` fast path, and prod. It does **not** run in the `--components api` fast path, which nevertheless does a full-release `helm upgrade` off `-f values-dev.yaml`. So `--components api` ships fresh `src/` (image is rebuilt + pod restarted) but STALE subchart templates. `--set` values still apply, so an image override lands while a `command:`/`serviceAccountName` edit does not — yielding a pod that starts and looks healthy while running the wrong process.

Related airflow gotcha: scheduler/triggerer/dagProcessor hardcode `safe-to-evict: "true"` gated on `*.safeToEvict` (default true) — adding podAnnotations "false" makes a duplicate key; set `*.safeToEvict: false` to suppress. See also [[helm-null-and-replicas-gotchas]].
