---
name: postgres-identity-plane-facts
description: Verified census of where the DataSpoke Postgres role/database names actually live — only the ROLE is a bare initdb literal, the DSN role is hardcoded in install.sh, and the Airflow metadata Secret's checksum is constant
metadata:
  type: project
---

Verified by rendering `helm-charts/dataspoke` and reading `airflow-1.20.0.tgz`
during the #119/#122/#131/#132 credential-integrity review (2026-08-02). Six
separate comments in that batch got the first fact wrong, each copying the one
before it.

**1. Only the ROLE is a bare literal in `primary.initdb.scripts`; the DATABASE
is not.** `01-extensions.sql` has `GRANT USAGE/SELECT ... TO dataspoke` and
`create-airflow-db.sql` has `CREATE DATABASE airflow OWNER dataspoke` — all four
occurrences are the **role**. The `dataspoke` *database* never appears: the
scripts run against `postgresql.auth.database` (the default initdb connection),
and `CREATE DATABASE airflow` names Airflow's own metadata store, unrelated to
`config.postgres.db`. So renaming the database is a clean two-site change
(`postgresql.auth.database` + `config.postgres.db`), fully covered by the render
guard; renaming the **role** is not.

**2. The Airflow metadata DSN's role is a hardcoded literal in `install.sh`, not
read from any values file.** `_derive_airflow_metadata_secret` builds
`postgresql://dataspoke:<pw>@dataspoke-postgresql:5432/airflow?sslmode=disable`
with `_url_encode "dataspoke"`. Host, database and role are all hardcoded — only
the password varies. Any doc that says the DSN "composes the ConfigMap's
`DATASPOKE_POSTGRES_USER`" is wrong, and any "change these N sites together"
recipe that omits this function hands the operator a broken Airflow.

**3. Site census for the role name: four, not three.**
`postgresql.auth.username`, `config.postgres.user`, the initdb SQL literals (in
BOTH `values.yaml` and `values-dev.yaml`), and `_derive_airflow_metadata_secret`.
The `templates/configmap.yaml` render guard reaches only the first two.

**4. `airflow.data.metadataSecretName` makes `checksum/metadata-secret` a
constant.** `airflow/templates/secrets/metadata-connection-secret.yaml` opens
with `{{- if not .Values.data.metadataSecretName }}`, so with the name set the
template renders only its license comment and every Deployment's
`checksum/metadata-secret: {{ include ... | sha256sum }}` hashes the same string
forever. Helm never rolls on a DSN change — an explicit rollout restart is
mandatory. Same shape as the `jwtSecretName` suppression.

**5. The metadata DSN reaches all four Airflow components, not two.**
`AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` lives in the `standard_airflow_environment`
helper, included by api-server, scheduler, dag-processor, triggerer, workers,
flower, the migrate-database job and both cleanup cronjobs. A comment claiming
"api-server and scheduler only" is describing the JWT secret and generalising.

**How to apply:** before repeating any "these names appear N times" or "this
value comes from X" claim about the Postgres identity, grep the initdb SQL for
the literal and read `_derive_airflow_metadata_secret`'s body. Related:
[[helm-null-and-replicas-gotchas]], [[dsn-escape-symmetry-facts]],
[[airflow-key-rotation-strand-gap]].
