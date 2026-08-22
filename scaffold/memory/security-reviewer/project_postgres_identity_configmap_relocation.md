---
name: postgres-identity-configmap-relocation
description: DATASPOKE_POSTGRES_{USER,DB} moved from the credentials Secret to the app ConfigMap; the load-bearing fact is envFrom last-source-wins (Secret shadows ConfigMap), plus which guard runs on which profile and where the SQL-identifier sink now sources from
metadata:
  type: project
---

Branch `fix/119-122-131-132-helm-credential-integrity` (2026-08-02) took the
credentials contract from 13 keys to **11**: `DATASPOKE_POSTGRES_USER` and
`DATASPOKE_POSTGRES_DB` left `dataspoke-secrets` for the app ConfigMap
(`config.postgres.{user,db}`). Only the password is a secret now.

**The non-obvious fact the whole design rests on:** Kubernetes `envFrom`
resolves duplicate keys by **last source wins**. `api-deployment.yaml` lists
`configMapRef` *then* `secretRef` at all three sites (api + `wait-for-postgres`
+ `alembic-migrate`), and `event-consumer/templates/deployment.yaml` does the
same. So a key left behind in the Secret **shadows** the ConfigMap. That is why
the removal is not cosmetic:

- **dev** self-heals — `_ensure_postgres_identity_leaves_credentials_secret`
  strategic-merge-patches both keys to `null` (`--patch-file /dev/stdin`,
  single-quoted heredoc, fixed literal — nothing lands in argv).
- **prod** rejects — `_check_airflow_credentials_prod` hard-errors if either
  key is still present. `install.sh` never mutates an operator-owned Secret;
  `_ensure_dataspoke_secrets` prod branch is read-or-error only. Verified.

**Both the dev patch and the prod rejection gate on a NON-EMPTY value**
(`[[ -n ... ]]`). A key present with an empty value is invisible to both, still
shadows the ConfigMap with `""`, and `os.environ.get(..., "dataspoke")` returns
`""` rather than the default -> auth failure. Fails closed, but silently, and it
is the inverse of the existence-vs-value trap in
[[credentials-secret-contract-key-addition]] #4. Ask *both* questions on any
projection guard: what does a missing key do, and what does an empty one do.

**Render-time consistency guard** in `templates/configmap.yaml`: `fail`s when
`config.postgres.user != postgresql.auth.username` or `config.postgres.db !=
postgresql.auth.database`, gated on `postgresql.enabled`. Measured with
`helm template --set` — both fire. It is an agreement check, **not** a shape
check. The role name appears a **third** time as a bare literal in
`primary.initdb.scripts` (both values files) that the guard cannot reach.

**Trust-boundary shift worth naming:** `DS_POSTGRES_USER` is interpolated
unparameterized into `GRANT USAGE ON SCHEMA ag_catalog TO ${DS_POSTGRES_USER}`
run as the Postgres **superuser** via `kubectl exec`, and `DS_POSTGRES_DB` into
`psql -d`. Its source moved from a Secret (needs `secrets` update RBAC) to a
ConfigMap (needs `configmaps` update — far more widely delegated). What keeps
this acceptable is the identifier regex `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`, now
applied to **both** values (the DB guard is new in this branch) and running at
`install.sh` before the `psql` call. **dev branch only** — prod runs neither the
GRANT nor the check. Do not let that regex be dropped or moved after the sink.

**Spec claim to distrust:** `spec/feature/HELM_CHART.md` says the Airflow
metadata DSN "composes ... the ConfigMap's `DATASPOKE_POSTGRES_USER`". It does
not — `_derive_airflow_metadata_secret` hardcodes the literal `dataspoke`
(`_url_encode "dataspoke"`). Same class as
[[operator-runbook-is-credential-surface]].

**How to apply:** for any future move of a key between the ConfigMap and the
Secret, render both templates and read the `envFrom` **order**, then trace the
already-installed-cluster path (a key in the old place still wins). Related:
[[credential-uri-escaping-boundary]], [[install-sh-preflight-gate-mechanics]],
[[credentials-secret-envfrom-fanout]].
