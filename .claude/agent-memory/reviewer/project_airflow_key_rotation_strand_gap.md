---
name: airflow-key-rotation-strand-gap
description: The rotated-signing-key stranding window in install.sh — prod's gap is now just the helm upgrade, but the three dev paths still run _ensure_airflow_fernet_secret's hard abort inside it
metadata:
  type: project
---

`_ensure_airflow_key_secrets` (`helm-charts/bin/install.sh`) writes a rotated
Airflow signing key into `dataspoke-airflow-api-secret-key` /
`dataspoke-airflow-jwt-secret` and sets `AIRFLOW_KEYS_ROTATED`. Only
`_restart_airflow_key_consumers`, later, repairs the split. Anything that
aborts **between** those two strands the key permanently: the next run compares
the credentials Secret against those same projections, finds them equal, skips
the restart, and reports success.

**Why:** prod was fixed (2026-08-01) by moving `_ensure_airflow_key_secrets`
out of Phase 1 to immediately before the umbrella `helm upgrade`, after digest
resolution — prod's gap is now the `helm upgrade` alone (`ingress_class` in
between re-validates an env var already checked in pre-flight, so it cannot
newly fail). The three **dev** paths were not changed: `_helm_upgrade_dataspoke_dev`
and the `--components frontend` fast path both call `_ensure_airflow_key_secrets`
then `_ensure_airflow_fernet_secret`, and the fernet check is a hard `error` on
a source-vs-projection mismatch. Prod deliberately runs fernet *first* for
exactly this reason and says so in its own comment. Reachable on dev: delete
`dataspoke-secrets` while keeping the Postgres PVC and the projections — the
regenerated Secret rotates the signing key (written) and disagrees on the Fernet
key (aborts), every run, forever. Under `--no-digest-pin` up to three unguarded
`_rollout_restart_workload` calls sit in the dev gap too.

**How to apply:** when reviewing any claim about this gap, enumerate what runs
between the write and the restart **per path** — `grep -n
"_ensure_airflow_key_secrets\|_ensure_airflow_fernet_secret\|_restart_airflow_key_consumers\|helm upgrade\|_rollout_restart_workload"
helm-charts/bin/install.sh` renders all four interleavings in one screen. A
residual note that names only the `helm upgrade` is describing prod and
generalising. Related: [[offload-fix-all-callsites]],
[[project_fernet_blast_radius_env_connections]].
