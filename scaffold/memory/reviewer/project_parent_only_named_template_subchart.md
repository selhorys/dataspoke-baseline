---
name: parent-only-named-template-subchart
description: RESOLVED — subcharts now define chart-scoped frontend.imageRef / event-consumer.imageRef; keep the tgz-vs-source diff recipe for checking subchart freshness
metadata:
  type: project
---

`helm-charts/dataspoke/templates/_helpers.tpl` defines `dataspoke.imageRef`
(renders `repo@digest` when `image.digest` is set, else `repo:tag`). The
frontend and event-consumer subcharts each define their **own** chart-scoped
copy (`frontend.imageRef` / `event-consumer.imageRef`) in their own
`templates/_helpers.tpl`, so `helm lint helm-charts/dataspoke/subcharts/*`
passes standalone again (verified).

**Why:** Helm's named-template namespace is global within one render, so a
parent-only definition works for the umbrella install but hard-fails a
standalone subchart render — and that standalone render is the documented way
to bypass the stale `charts/*.tgz` problem ([[helm-stale-local-subchart-tgz]]).

**How to apply:** the umbrella `values.yaml` frontend/event-consumer `digest:`
comments still point at `dataspoke.imageRef` — that pointer is wrong; those
maps flow to the subchart-scoped helpers. To check subchart freshness without
trusting comments, extract `helm-charts/dataspoke/charts/<name>-0.1.0.tgz` into
a scratchpad and `diff -r` it against `subcharts/<name>/` (only Chart.yaml key
ordering should differ). `_build_chart_deps` in install.sh `rm -f`s those two
tgz before `helm dependency build`, so both dev and prod repackage from source.
