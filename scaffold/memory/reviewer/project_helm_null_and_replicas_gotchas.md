---
name: helm-null-and-replicas-gotchas
description: Four verified Helm/chart gotchas that reviews keep tripping on — Bitnami redis replica STS is plural (`-replicas`), `annotations: null` varies by chart depth, the Airflow db-migrate Job is a POST-install hook, and `storageClass: "-"` is a legal sentinel that DNS-name validators reject but only 7 of 14 pinnable keys honour
metadata:
  type: project
---

Four empirically verified facts about `helm-charts/dataspoke` that documentation
and pre-flight guards routinely get wrong. All were confirmed by rendering or by
reading the vendored subchart source, not inferred.

**1. Bitnami redis replica StatefulSet is `dataspoke-redis-replicas` (plural).**
So the PVC is `redis-data-dataspoke-redis-replicas-0`, not `...-replica-0`.
The master is singular (`dataspoke-redis-master` → `redis-data-dataspoke-redis-master-0`),
and the replica *ServiceAccount* is singular too — only the STS/Service/PDB are plural.
Postgres is `data-dataspoke-postgresql-0`.

**Why:** a wrong PVC name makes a documented `kubectl delete pvc` one-liner
partially no-op, silently stranding an 8Gi volume. Docs asserting retained-resource
names had this wrong in both `helm-charts/README.md` and `spec/feature/HELM_CHART.md`.

**How to apply:** whenever a doc, script echo, or spec table names a PVC, render the
chart and read `volumeClaimTemplates` + the owning workload name rather than
deriving the name from the values key. Prefer a `kubectl get pvc` probe over an
unconditional printed list — a probe would have surfaced this at runtime.

**2. `<key>: null` in an overlay has three different outcomes depending on where it lands.**
- Parent-chart values (e.g. `api.ingress.annotations`) → key is **removed** from the render.
- Subchart values (e.g. `frontend.ingress.annotations`, a `file://subcharts/` dep) →
  key survives as a literal `null`; K8s coerces it to `""` at apply time
  (verified: `kubectl apply --dry-run=client` yields `{"key":""}`), so the annotation
  is present-but-empty, NOT absent.
- Charts shipping a `values.schema.json` (the Apache `airflow` dep) → `helm template`
  **errors**: `Invalid type. Expected: string, given: null`. Install aborts.

**Why:** guidance that says "set it to null to disable it" is only correct for one of
the three, and is install-breaking for the airflow ingress.

**How to apply:** never accept a blanket null-merge claim across ingresses/subcharts.
Render each case.

**3. The Airflow subchart's db-migrate Job is a `post-install,post-upgrade` hook,
not `pre-install`/`pre-upgrade`.** `airflow-1.20.0.tgz` →
`templates/jobs/migrate-database-job.yaml` sets, under `migrateDatabaseJob.useHelmHooks`,
`helm.sh/hook: post-install,post-upgrade` + `hook-weight: 1` +
`hook-delete-policy: before-hook-creation,hook-succeeded`. Every other Airflow pod
carries a `wait-for-airflow-migrations` init container that blocks until it finishes,
which is what makes it *feel* like a pre-hook.

**Why:** an issue report, a plan, a spec section, and a values.yaml comment all
asserted "pre-install/pre-upgrade" in the same batch, each copying the one before.
The failure story changes with the phase: a failed post-hook leaves the release
`failed` with every workload already applied and blocked on its init container.

**How to apply:** grep the rendered Job's `helm.sh/hook` annotation before repeating
any hook-phase claim in prose.

**4. `storageClass: "-"` is a legal, upstream-documented Bitnami sentinel — a
DNS-subdomain validator rejects it.** `common/templates/_storage.tpl` maps `"-"` to
`storageClassName: ""` (disable dynamic provisioning / bind a pre-provisioned PV);
every Bitnami `persistence.storageClass` value carries that note. Precedence is
`global.storageClass` → `<component>.persistence.storageClass` →
`global.defaultStorageClass`; `""` and `null` both mean "cluster default".

**Why:** an `install.sh` pre-flight that resolves pinned classes and validates each
against `^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$` before `kubectl get storageclass` will
hard-abort a valid static-PV prod overlay, with a message claiming the value is not
a valid Kubernetes name. Also note the Airflow subchart spells the key
`storageClassName` (not `storageClass`) under `workers.celery.persistence` /
`triggerer.persistence`, so a resolver written against the Bitnami spelling misses
them — and `values-prod.example.yaml` actively tells operators to enable those blocks.

**The `-` sentinel is not universal across the subcharts, though.** Verified by
rendering `airflow-1.20.0.tgz`: only `logs` and `dags` implement
`{{- if (eq "-" ...) }} storageClassName: "" {{- end }}`. `triggerer.persistence`,
`workers.persistence`, `workers.celery.persistence` and `redis.persistence` emit
`storageClassName: "-"` **literally**, which the API server then rejects
(`storageClassName` is validated as a DNS subdomain) — a mid-install failure, not
a pre-flight one. `values-prod.example.yaml` (the "Airflow log persistence" block)
tells operators to enable exactly `workers.celery.persistence` and
`triggerer.persistence`, i.e. two of the four keys that do *not* honour it.

**There is no `existingClaim` escape hatch on three of those four keys.** Verified
in `airflow-1.20.0.tgz` → `values.yaml`: `existingClaim` exists only under
`redis.persistence`, `dags.persistence`, and `logs.persistence`. `triggerer`,
`workers`, and `workers.celery` have none, and `persistence.enabled: false` there
routes the volume to an **emptyDir** (`triggerer-deployment.yaml`:
`{{- else if not $persistence }} emptyDir`), not to a pre-provisioned PV. So any
error message or doc that offers "set `persistence.enabled: false` for a
pre-provisioned PV" as the remedy for a rejected `-` is telling the operator to
silently discard the very log retention `values-prod.example.yaml` told them to
turn on.

**Full pinnable-key census (14, not 11).** Beyond the eleven a resolver typically
covers: `postgresql.readReplicas.persistence.storageClass`,
`postgresql.backup.cronjob.storage.storageClass`, and
`redis.sentinel.persistence.storageClass` — all three honour `-` via
`common.storage.class`, all three unreachable in the shipped standalone/no-backup
shape. Docs that say "all eleven keys" read as exhaustive and are not.

**How to apply:** any guard over chart-value strings must whitelist the upstream
sentinels first (`""`, `null`, `-`) and be checked against every spelling of the key
across subcharts — but scope the `-` whitelist to the keys whose template actually
maps it, or the guard green-lights a value that renders invalid. When the guard
rejects a value, check that the remediation it prints is reachable: grep the
upstream chart for the key it names before believing the message.
Related: [[helm-stale-local-subchart-tgz]].
