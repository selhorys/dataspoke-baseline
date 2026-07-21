---
name: datahub-gms-public-virtual-host
description: DataHub GMS is served at the root of its own public ingress host with DataHub's auth-excluded paths reachable unauthenticated; ingress carries no TLS block and disables ssl-redirect
metadata:
  type: project
---

Since issue #80, DataHub GMS is published at `datahub-gms.<INGRESS_DOMAIN>/`
(`dev-peripherals/datahub/gms-ingress.yaml`, backend `datahub-datahub-gms:8080`),
replacing the old `datahub.<domain>/gms/(.*)` + rewrite route.

Two durable facts to reuse instead of re-deriving:

1. **The exposure set did not change.** The old regex matched everything except
   bare `/gms`, so it never filtered. GMS's own `AuthenticationEnforcementFilter`
   registers on `/*` and enforces regardless of the ingress path, with a fixed
   unauthenticated allow-list (`metadata-service/configuration/src/main/resources/application.yaml`,
   `authentication.excludedPaths`): `/schema-registry/*`, `/health`, `/health/live`,
   `/health/detailed`, `/config`, `/config/search/export`, `/public-iceberg/*`,
   `/actuator/prometheus`, `/openapi/operations/dev/featureFlags*`. `/schema-registry/api`
   is a read **and write** surface. Those are internet-reachable in shared/EKS mode.
2. **That host has no TLS.** `DATASPOKE_KUBE_INGRESS_TLS_SECRET` plumbing
   (`_frontend_helm_set_args`, `_api_airflow_tls_helm_set_args` in `bin/install.sh`)
   covers only the API, frontend, and Airflow ingresses. The GMS Ingress has no
   `tls:` block and carries `nginx.ingress.kubernetes.io/ssl-redirect: "false"`,
   while the install's own PAT mint sends `Authorization: Basic <system_client_secret>`
   and a `NO_EXPIRY` Bearer PAT to that host.

**How to apply:** do not re-litigate "did the ingress change break the auth
boundary" — it did not. Judge instead whether new work adds an ingress-level control
(allow-list, mTLS, auth annotation) or extends TLS coverage to the peripheral hosts,
and whether operator docs list `datahub-gms.` for cert SAN / WAF policy, not just DNS.
