---
name: credentials-secret-envfrom-fanout
description: dataspoke-secrets is envFrom'd wholesale (no key selection) into the api + its two init containers and event-consumer; the frontend grant was removed in the #111 run, so every new contract key still lands in three containers that mostly do not read it
metadata:
  type: project
---

`dataspoke-secrets` (the consolidated credentials Secret) is mounted via
`envFrom: secretRef` — **no key selection** — into:

| Container | Template | Needs it? |
|---|---|---|
| `api` + `wait-for-postgres` + `alembic-migrate` init containers | `dataspoke/templates/api-deployment.yaml` (3 `secretRef` sites) | yes (pg/redis/jwt/internal token) |
| `event-consumer` | `dataspoke/subcharts/event-consumer/templates/deployment.yaml:51, 79` | partly |
| ~~`frontend`~~ | removed in the #111 run | no — read zero keys |

So a key added to the contract (e.g. `DATASPOKE_AIRFLOW_FERNET_KEY`) lands in
the API container even though only Airflow consumes it — an API env dump now
also yields decryption of Airflow's stored connection credentials. Verified:
`Settings` is `extra='forbid'` but pydantic-settings only collects declared
fields, so an unmatched `DATASPOKE_*` env var does **not** break startup —
there is no crash signal to notice the widening.

Two live gotchas in the same fan-out:

- `event-consumer/templates/deployment.yaml` **hardcodes**
  `name: dataspoke-secrets` instead of `.Values.secrets.existingSecret`, and
  the ref is `optional: true`. With the prod overlay's
  `secrets.existingSecret: dataspoke-secrets-prod` the consumer would start
  with *no* credentials and the dev-default JWT secret rather than failing.
  Latent only because `event-consumer.enabled: false` by default.
- The frontend fix is the precedent to cite: dropping the `secretRef`, the
  subchart's `existingSecretName` value, and the prod
  `--set frontend.existingSecretName=` all three together.

**Why:** found on the #111 13th-key addition — the consolidated-Secret design
means every future key addition silently widens this grant, and the key
addition itself looks local and safe.

**How to apply:** on any diff that adds a key to the credentials contract,
render `helm template ... --set frontend.enabled=true --set event-consumer.enabled=true`
and enumerate which containers *receive* the key, not just which one consumes it.
Related: [[credentials-secret-contract-key-addition]],
[[operator-runbook-is-credential-surface]].
