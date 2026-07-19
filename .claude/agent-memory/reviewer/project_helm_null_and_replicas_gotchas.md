---
name: helm-null-and-replicas-gotchas
description: Two verified Helm/chart gotchas that doc reviews keep tripping on — Bitnami redis replica STS is plural (`-replicas`), and `annotations: null` behaves differently in parent chart vs subchart vs schema-validated chart
metadata:
  type: project
---

Two empirically verified facts about `helm-charts/dataspoke` that documentation
routinely gets wrong. Both were confirmed with `helm template`, not inferred.

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
Render each case. Related: [[helm-stale-local-subchart-tgz]].
