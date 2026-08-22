---
name: fernet-blast-radius-env-connections
description: DataSpoke's Airflow connections arrive as AIRFLOW_CONN_* env vars, so almost nothing in the metadata DB is Fernet-encrypted — caps the real severity of Fernet-key rotation/loss findings
metadata:
  type: project
---

The Airflow chart wires `AIRFLOW__CORE__FERNET_KEY` into all four components, and the
umbrella pins `airflow.fernetKeySecretName` in `helm-charts/dataspoke/values.yaml`
(which also suppresses the subchart's own `pre-install` hook Secret
`<fullname>-fernet-key`). That makes a lost/rotated Fernet key look like a data-loss
finding. But DataSpoke supplies its Airflow connections as **env vars**
(`AIRFLOW_CONN_DATASPOKE_API`, injected via `--set-file airflow.extraEnv=`), and
nothing in `src/` calls `Variable.set` / creates DB Connections. Env-var connections
bypass Fernet entirely, so the encrypted surface in the metadata DB is essentially
empty (transient `trigger.kwargs` only).

**Why:** while reviewing the #111 Fernet work I nearly rated "dev teardown loses the
key while PVCs survive" as high severity. Verified with `helm template` + a grep for
`Variable.get|Connection(|AIRFLOW_CONN_` across `src/` — the encrypted rows are not
there. The genuinely stranding item on a dev teardown is
`DATASPOKE_POSTGRES_PASSWORD`, not the Fernet key.

**How to apply:** for any finding about Fernet key rotation/adoption/teardown, check
whether this deployment actually stores Fernet-encrypted rows before assigning
severity — and re-check, since a future feature that creates Airflow Connections or
Variables would flip this. Related: [[project_helm_null_and_replicas_gotchas]].
