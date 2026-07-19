# HELM_CHART — DataSpoke Deployment Subsystem

## Table of Contents

1. [Overview](#overview)
2. [Repository Layout](#repository-layout)
3. [Profiles](#profiles)
4. [Installation](#installation)
5. [Uninstallation](#uninstallation)
6. [Umbrella Chart Structure](#umbrella-chart-structure)
7. [Configuration — Four-Tier Env Vars](#configuration--four-tier-env-vars)
8. [Namespace Sourcing](#namespace-sourcing)
9. [The .env File](#the-env-file)
10. [Configuration Flow](#configuration-flow)
11. [Image Builds](#image-builds)
12. [Dev-Only Peripherals](#dev-only-peripherals)
13. [Post-Install Seeding](#post-install-seeding)
14. [Resource Sizing](#resource-sizing)
15. [Ingress & Network Policy](#ingress--network-policy)
16. [Secrets Management](#secrets-management)
17. [Health Check](#health-check)
18. [Troubleshooting](#troubleshooting)
19. [References](#references)

---

## Overview

`helm-charts/` is the single deployment subsystem for DataSpoke — both production
and development. It comprises:

- `helm-charts/dataspoke/` — umbrella Helm chart packaging frontend, API,
  event-consumer, PostgreSQL, Redis, and Airflow.
- `helm-charts/bin/` — install/uninstall/build/health scripts that orchestrate the
  charts plus dev-only peripherals.
- `helm-charts/dev-peripherals/` — values files, charts, and plain-K8s manifests for
  the dev-only peripheral components (nginx-ingress, DataHub, Langfuse, dummy data,
  dev-lock). Langfuse lives here as `dev-peripherals/langfuse/` — a chart whose single
  `values.yaml` holds dev-profile values; in production the operator supplies their own.

The same umbrella chart serves both profiles. The **profile** (`dev` or `prod`)
selects the values overlay and the surrounding component set; the chart itself
is profile-agnostic.

> **Example namespace names.** `dataspoke-01`, `datahub-01`, `langfuse-01`, and
> `dataspoke-dummy-data-01` appear throughout this document as illustrative
> values only. Every namespace is operator-chosen via the four `.env` vars
> `DATASPOKE_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`,
> `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE`, and
> `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE`. `.env.dev.example` ships the names above
> as examples; see §Namespace Sourcing.

```
helm-charts/bin/install.sh --profile dev      # full dev stack incl. peripherals
helm-charts/bin/install.sh --profile prod     # umbrella chart only; operator supplies values + peripherals
```

The CLI is the same for both profiles. The API image rebuild is a first-class
step of `install.sh` (`--components api` runs only the rebuild + helm-upgrade
+ rollout cycle).

---

## Repository Layout

```
helm-charts/
├── README.md                           # operational guide for bin/ scripts
├── .env.dev.example                     # dev canonical env-var listing (3 sections)
├── .env.prod.example                    # prod operator template (deployment shape only)
├── values-prod.example.yaml             # prod values-overlay template (--values starting point)
├── bin/
│   ├── install.sh                       # main installer
│   ├── uninstall.sh                     # main uninstaller
│   ├── health-check.sh                  # service-by-service probe
│   ├── build-image.sh                   # api | airflow | postgres | frontend (Cloud Build / ECR / local)
│   ├── port-forward.sh                  # forward TCP services to 127.0.0.1 (shared ingress mode)
│   ├── lib/helpers.sh                   # logging + kubectl/helm wrappers + ingress-mode helpers
│   ├── dev-peripherals/                 # dev-only orchestrators
│   │   ├── nginx-ingress.sh
│   │   ├── datahub.sh
│   │   ├── langfuse.sh
│   │   ├── dummy-data.sh
│   │   └── dev-lock.sh
│   └── post-install/                    # admin-API seeding (admin user: both profiles)
│       ├── seed-peripheral-config.sh
│       ├── seed-runtime-config.sh
│       └── seed-admin-user.sh
├── dataspoke/                           # umbrella Helm chart
│   ├── Chart.yaml
│   ├── values.yaml                      # prod defaults
│   ├── values-dev.yaml                  # dev overlay
│   ├── templates/                       # api-deployment/service/ingress/pdb, configmap, secrets, RBAC, networkpolicy
│   ├── subcharts/{frontend,event-consumer}/
│   └── charts/                          # bitnami pg/redis, apache-airflow (resolved deps)
└── dev-peripherals/                     # dev-only values + manifests + charts
    ├── nginx-ingress/values.yaml
    ├── datahub/
    │   ├── values.yaml
    │   ├── prerequisites-values.yaml
    │   ├── gms-ingress.yaml
    │   └── kafka-external-svc.yaml
    ├── langfuse/                        # chart for Langfuse subsystem (single dev-profile values.yaml)
    ├── dummy-data/manifests/            # plain K8s manifests (PG + Kafka KRaft)
    └── dev-lock/manifests/              # plain K8s manifests (Python HTTP service)
```

---

## Profiles

| Aspect | `dev` | `prod` |
|---|---|---|
| Umbrella chart | ✓ (`values-dev.yaml`) | ✓ (`values.yaml` + operator overlay) |
| Image rebuild | ✓ (default) | ✓ or `--skip-build` (CI-built image) |
| Frontend subchart | ✗ (host `npm run dev`) | ✓ |
| Event-consumer subchart | ✗ | ✗ (opt-in by operator) |
| RuntimeConfig stub fields (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`) seeded `true` | ✓ | ✗ (defaults `false`) |
| nginx-ingress install | ✓ (managed mode); ✗ (shared mode reuses the operator's controller) | ✗ (operator's controller) |
| DataHub install | ✓ (in-cluster) | ✗ (external; operator-managed) |
| Langfuse install | ✓ (in-cluster) | ✗ (external; operator-managed) |
| Dummy data | ✓ | ✗ |
| Dev-lock | ✓ | ✗ |
| Post-install peripheral seeding | ✓ | ✗ (operator uses admin API / UI) |
| Post-install admin-user seed | ✓ | ✓ (both skippable with `--skip-seed`) |
| Online LLM key rotation | ✓ | ✓ |

**Why prod skips peripherals**: production DataHub and Langfuse installations
require sizing, HA, persistence, and security choices that are organization-
specific and out of this project's scope. Operators bring their own; DataSpoke
wires them via the runtime admin API (`/api/v1/admin/peripherals/{datahub,langfuse}`).

---

## Installation

`bin/install.sh` is the single installer entry point.

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--profile {dev\|prod}` | required | Selects component set + values overlay. |
| `--env-file <path>` | `helm-charts/.env.<profile>` | Env file to source. Defaults to `.env.dev` for `--profile dev`, `.env.prod` for `--profile prod`. Exported so child and post-install scripts inherit the same resolved file. |
| `--components <list>` | all-for-profile | Comma-separated subset (e.g. `api`, `dataspoke-infra`, `datahub`). |
| `--from-component <name>` | — | Resume an interrupted full install at this component. |
| `--skip-build` | false | Skip Docker image rebuild (api/airflow/postgres). |
| `--skip-seed` | false (dev) | Skip post-install admin-API seeding. |
| `--values <path>` | — | Extra values file passed to the umbrella chart (prod). |
| `--image-tag <tag>` | `dev` | Override the image tag for api/airflow/postgres (prod CI). |
| `--frontend {none\|local\|cluster}` | `none` (dev), `cluster` (prod) | Frontend deployment mode for a full install. `none`: not deployed. `local` (dev-only): writes `src/frontend/.env.local` pointing at the in-cluster API for host `pnpm dev`. `cluster`: builds the image and deploys the UI in-cluster. |
| `--help`, `-h` | — | Print usage. |

### Phases — dev profile

| # | Phase | Components | Notes |
|---|---|---|---|
| 1 | Pre-flight | tool check, context switch, namespace ensure, nginx-ingress install | nginx-ingress must complete first to provide `INGRESS_IP` / `_DOMAIN` for downstream. |
| 2 | **Parallel bootstrap** | `build-image.sh api` ‖ `build-image.sh airflow` ‖ `build-image.sh postgres` ‖ `dev-peripherals/datahub.sh` ‖ `dev-peripherals/langfuse.sh` | bash `&` + `wait`. Failures of any branch abort the install. `build-image.sh frontend` is added only when `--frontend cluster`. |
| 3 | Umbrella chart | `helm upgrade --install dataspoke ./helm-charts/dataspoke -f values-dev.yaml` | Depends on phase 2: images pulled by deployment, DataHub URL/PAT/Kafka + Langfuse host/public-key fed via `--set` for downstream seeding. `frontend.enabled` is `false` unless `--frontend cluster`, which appends the frontend `--set` flags and waits for the `dataspoke-frontend` rollout. |
| 4 | **Parallel post-bootstrap** | `dev-peripherals/dummy-data.sh` ‖ `dev-peripherals/dev-lock.sh` | Both depend on cluster connectivity but not on each other. |
| 5 | Post-install seeding | `seed-peripheral-config.sh`, `seed-runtime-config.sh`, `seed-admin-user.sh` | PATCHes `/internal/admin/peripherals/{datahub,langfuse}`, `/internal/admin/conf`, and POSTs `/internal/admin/bootstrap` (idempotent: seeds the default `dataspoke@dataspoke.local / dataspoke` Admin only when no Admin exists). Skipped by `--skip-seed`. |

### Phases — prod profile

| # | Phase | Components | Notes |
|---|---|---|---|
| 1 | Pre-flight | tool check, context switch, namespace ensure | No nginx-ingress install — operator's controller. |
| 2 | Image build | `build-image.sh api` ‖ `build-image.sh airflow` ‖ `build-image.sh postgres` | Skipped by `--skip-build` when CI built and pushed the images. `build-image.sh frontend` runs under the default `--frontend cluster`; skipped under `--frontend none`. |
| 3 | Umbrella chart | `helm upgrade --install dataspoke ./helm-charts/dataspoke -f values.yaml -f <operator-overlay>` | Operator supplies values overlay with their own ingress hosts, TLS, registry, replica counts, source-credential references. `frontend.enabled` is set from `--frontend` (`cluster`→true, `none`→false; default `cluster`). |
| — | Admin seed | `post-install/seed-admin-user.sh` | Runs after the chart phase unless `--skip-seed` is passed. Idempotent; seeds the default `dataspoke@dataspoke.local / dataspoke` Admin only when no Admin exists. Carries no `step` marker of its own. |

Peripheral wiring (DataHub URL/token, Langfuse host/keys, LLM provider/model/key)
is the operator's responsibility post-install, via `/api/v1/admin/peripherals/*`
and `/api/v1/admin/conf`. An AI scaffold may automate this for an organization
but is out of baseline scope. The phases above cover the install itself — for the
surrounding lifecycle (Secret pre-creation, the mandatory credential rotation
that follows the automatic seed, peripheral registration) see §Prod operator
workflow.

### Component names

| Component | Profiles | Source |
|---|---|---|
| `nginx-ingress` | dev | `dev-peripherals/nginx-ingress.sh` |
| `datahub` | dev | `dev-peripherals/datahub.sh` |
| `langfuse` | dev | `dev-peripherals/langfuse.sh` |
| `dataspoke-infra` | dev, prod | `dataspoke/` umbrella chart (alias: `chart`, `umbrella`) |
| `api` | dev, prod | umbrella chart, `api.*` block (rebuilds api image and `helm upgrade` of the API only) |
| `frontend` | dev, prod | umbrella chart, `frontend.*` block (rebuilds frontend image and `helm upgrade` of the UI only) |
| `dummy-data` | dev | `dev-peripherals/dummy-data.sh` |
| `dev-lock` | dev | `dev-peripherals/dev-lock.sh` |
| `seed` | dev | `post-install/*` |

`--components api` rebuilds the API image, runs `helm upgrade` against the
umbrella chart, and rolls the API deployment. `--components frontend` is the
analogous code-iteration path for the UI pod.

For a full install, `--frontend` governs the UI: `none` deploys nothing; `local`
(dev-only) writes `src/frontend/.env.local` after seeding so host `pnpm dev`
reaches the in-cluster API; `cluster` deploys the containerised UI. The `local`
and `cluster` install summaries surface the Web UI URL and the default
`dataspoke@dataspoke.local / dataspoke` login.

---

## Uninstallation

`bin/uninstall.sh --profile {dev|prod} [--env-file <path>] [--components frontend] [--no-question] [--delete-pvcs] [--delete-namespaces] [--delete-all]`

`--env-file` carries the same profile-aware default as `install.sh`
(`.env.dev` for dev, `.env.prod` for prod) and is exported for child scripts.

`--components frontend` is a targeted teardown: `helm upgrade --reuse-values
--set frontend.enabled=false` on the `dataspoke` release, leaving all other
components in place. Only `frontend` is supported (the api subchart is the core
service — stop it with `kubectl scale --replicas=0`). Without `--components`, the
full profile is torn down.

Reverse order of install. Both profiles tear down the umbrella Helm release.
The dev profile additionally removes peripherals and dev-lock. `--no-question`
suppresses every interactive prompt (gate, PVC, namespace).

### What a prod uninstall leaves behind

Teardown is deliberately non-destructive to state: it removes the Helm release
and the chart-derived Airflow Secrets, and nothing else.

| Retained | Detail |
|---|---|
| `data-dataspoke-postgresql-0` | 50 Gi — operational DB (relational + pgvector) and Airflow metadata |
| `redis-data-dataspoke-redis-master-0` | 8 Gi |
| `redis-data-dataspoke-redis-replicas-0` | 8 Gi — note the plural `replicas`, matching the StatefulSet name; the master claim is singular |
| `dataspoke-secrets` (or the `secrets.existingSecret` name) | Operator-owned; never deleted |
| `dataspoke-airflow-fernet-key` | Airflow chart Secret carrying `helm.sh/resource-policy: keep` |
| `dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret` | Out-of-band, not Helm-managed (see §Secrets Management) |

Three PVCs, **66 Gi** total, keep consuming storage after teardown. The uninstall
output names exactly three Secrets as deleted —
`dataspoke-airflow-metadata-db`, `dataspoke-airflow-api-secret-key`,
`dataspoke-airflow-jwt-secret` — and separately logs `dataspoke-secrets` as
retained. `dataspoke-airflow-fernet-key` and the out-of-band Secrets survive
silently, named in neither line, so an operator auditing residue must look for
them explicitly.

**Fernet key ↔ Postgres PVC coupling.** Airflow encrypts connection secrets in
its metadata DB with the fernet key held in `dataspoke-airflow-fernet-key`.
Because that metadata lives in the retained Postgres PVC, the two must be kept
or dropped together: deleting the fernet-key Secret while keeping the PVC leaves
every stored Airflow connection permanently undecryptable on reinstall.

**Full wipe.** `--delete-pvcs` is a dev-only flag. In prod the sanctioned full
wipe is `--delete-namespaces` (or `--delete-all`), which removes the namespace
and with it the PVCs and every Secret above — including the keep-annotated and
out-of-band ones. Recreate the operator Secret before the next install.

---

## Umbrella Chart Structure

Standard Helm umbrella with `Chart.yaml` (apiVersion v2), `values.yaml` (prod)
and `values-dev.yaml` (dev overlay), `templates/` (configmap, secrets, RBAC,
networkpolicy, plus the API Deployment/Service/Ingress rendered directly from
the umbrella), two application `subcharts/` (frontend, event-consumer), and
`charts/` (fetched dependency archives).

The API server is **not** a separate subchart. Its Deployment, Service, and
Ingress live in `templates/api-*.yaml` so the API binds to the
`dataspoke-api` cluster DNS name that Airflow callbacks expect, while still
respecting the `api.*` values block.

### Dependencies

| Subchart | Source | Version | Condition |
|---|---|---|---|
| frontend | `file://subcharts/frontend` | 0.1.0 | `frontend.enabled` |
| event-consumer | `file://subcharts/event-consumer` | 0.1.0 | `event-consumer.enabled` |
| postgresql | `bitnami/postgresql` | ~18.5.0 | `postgresql.enabled` |
| redis | `bitnami/redis` | ~25.3.0 | `redis.enabled` |
| airflow | `apache-airflow/airflow` | ~1.20.0 (ships Airflow 3.1.8) | `airflow.enabled` |

The API is configured under the `api.*` values block (not a subchart) and gated
by `api.enabled` against the umbrella's own templates.

**Bitnami image sourcing**: Bitnami moved unversioned tags behind a paywall in
August 2025, so the redis subchart is pinned to `bitnamilegacy/redis:8.2.1-debian-12-r0`
— the `bitnamilegacy/*` namespace keeps a free, chart-compatible image available.
Because that image sits outside the repository the subchart expects, the chart
also sets `global.security.allowInsecureImages: true` to opt out of Bitnami's
image-origin check. The custom PostgreSQL image layering pgvector + AGE on a
`bitnamilegacy` base is the related case behind the same flag. Both image pins
are supply-chain-relevant: prod operators are expected to repoint these at their
own registry mirror.

### Component matrix

| Component | Type | Prod | Dev | Stateful |
|---|---|---|---|---|
| frontend | Deployment | ✓ | ✗ (host) | no |
| api | Deployment | ✓ | ✓ (in-cluster; stub-mode flags seeded in RuntimeConfig) | no |
| event-consumer | Deployment | ✗ (opt-in) | ✗ | no |
| postgresql | StatefulSet | ✓ | ✓ | yes (PV) |
| redis | StatefulSet | ✓ | ✓ | yes (PV, 8Gi) |
| airflow (api-server + scheduler + triggerer + dag-processor) | Deployments | ✓ | ✓ | no (task logs in emptyDir; metadata in PG) |

Each component has a `<component>.enabled` toggle.

### Eviction resilience

Every component ships a PodDisruptionBudget paired with a
`cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` pod annotation:
`templates/api-pdb.yaml`, `subcharts/{frontend,event-consumer}/templates/pdb.yaml`,
and the subchart-native keys for the dependencies (bitnami redis `master.pdb` /
`replica.pdb`, bitnami postgresql `primary.pdb`, Airflow
`podDisruptionBudget.config`). This is a deliberate availability guard against
Autopilot / cluster-autoscaler evicting a pod during scale-in. The annotation
alone is advisory, so the PDB is what actually blocks the disruption. On the
Airflow scheduler, triggerer, and dag-processor `safeToEvict: false` suppresses
the chart's default `safe-to-evict="true"` annotation so `podAnnotations` can set
`"false"` without rendering a conflicting duplicate key.

**Every single-replica component permits zero voluntary disruption** — expressed
as `maxUnavailable: 0` in the Airflow chart and `minAvailable: 1` in the Bitnami
charts (semantically identical at one replica). This covers the Airflow
api-server, scheduler, triggerer, and dag-processor, the postgresql primary, the
redis master, and the dev API (`values-dev.yaml` sets `replicaCount: 1` while
inheriting `api.podDisruptionBudget.minAvailable: 1`). The operational
consequence is that node drains and cluster upgrades **block until an operator
intervenes** — the guard trades drain automation for uptime.

---

## Configuration — Four-Tier Env Vars

| Tier | Prefix | Scope | Read by |
|---|---|---|---|
| App runtime | `DATASPOKE_*` (no `KUBE` / `DEV` / `TEST`) | Both profiles | DataSpoke Python/Node code via K8s ConfigMap/Secret (`envFrom`) |
| Kube deployment | `DATASPOKE_KUBE_*` | Both profiles | `bin/*.sh` install / uninstall / build scripts |
| Dev-only inputs | `DATASPOKE_DEV_*` | Dev profile only | `bin/dev-peripherals/*.sh`, `bin/post-install/*.sh` |
| Test access | `DATASPOKE_TEST_*` | Dev profile only | `tests/integration/{conftest.py,util/*}`; auto-populated by install.sh post-install; never read by app pods |

### Tier 1 — App runtime (`DATASPOKE_*`)

Same names in dev and prod, different values. Injected into pods via ConfigMap
(non-sensitive) or Secret (sensitive) from the `dataspoke-secrets` K8s Secret per
§Configuration Flow. Not present in `helm-charts/.env.dev` / `helm-charts/.env.prod`.

- `DATASPOKE_POSTGRES_{HOST,PORT,USER,PASSWORD,DB}`
- `DATASPOKE_REDIS_{HOST,PORT,PASSWORD}`
- `DATASPOKE_AIRFLOW_{URL,USER,PASSWORD,CALLBACK_BASE_URL}`
- `DATASPOKE_INTERNAL_TOKEN` — shared secret for Airflow → API internal calls
- `DATASPOKE_JWT_SECRET_KEY` — JWT HS256 signing key
- `DATASPOKE_OAUTH_STATE_SECRET` — HMAC key for the Google-OAuth state cookie
- `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` — Google OAuth client secret (paired with the public client_id; see chart-values-only callout below)

**Chart-values-only env vars (not in `.env`)** — sourced from chart values,
rendered into the app ConfigMap, and reaching the API container via `envFrom`;
never sourced from `.env`:

| Env var | Chart value | Role |
|---|---|---|
| `DATASPOKE_CORS_ORIGINS` | `config.corsOrigins` | Comma-separated CORS origins the API accepts (the browser UI origin). Dev always lists `http://localhost:3000` plus both `http://app.<domain>` and `https://app.<domain>`, so a UI page served over either scheme passes CORS regardless of `DATASPOKE_KUBE_INGRESS_SCHEME`. |
| `DATASPOKE_COOKIE_SECURE` | `auth.cookieSecure` | `Secure` flag on auth cookies — `true` in `values.yaml`, `false` in `values-dev.yaml` for HTTP laptop browsers. |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_ID` | `auth.googleClientId` | Google OAuth public client id; absence disables Google login. |
| `DATASPOKE_OAUTH_POST_LOGIN_REDIRECT` | `config.oauthPostLoginRedirect` | URL the Google/OIDC callback 302-redirects to after login (the frontend origin). `install.sh` sets it per `--frontend` mode (`local`→`localhost:3000`, `cluster`→`app.<domain>`); default `"/"` only works when UI and API share a host. |

Keeping these out of `.env` removes the prod footgun of a stray line silently
disabling cookie hardening. Stub-mode wiring for the four dependency factories
lives in the `runtime_config` DB row (`stub_redis_client`, `stub_llm_client`,
`stub_pgvector_manager`, `stub_notification_service`) — see
`BACKEND_LLM.md §Test Mode` and `TESTING.md §Stub Toggles`.

> DataHub, Langfuse, and LLM provider/model/key are **not** app-runtime env
> vars. Their **non-secret** settings (DataHub
> `gms_url`/`frontend_url`/`kafka_brokers`/`service_corpuser_urn`/`default_env`, Langfuse
> `host`/`public_key`/`project_id`/`environment_tag`, LLM provider/model +
> generation knobs) live in
> the DB `peripheral_config` and `runtime_config` tables, updated via
> `/api/v1/admin/peripherals/{datahub,langfuse}` and `/api/v1/admin/conf`. Their
> **secret** fields (DataHub token, Langfuse `secret_key`, LLM API key) never
> touch the DB — they live in K8s Secrets (`dataspoke-datahub-secret`,
> `dataspoke-langfuse-secret`, `dataspoke-llm-secret`), read at runtime via the
> API's RBAC. See §Secrets Management and `BACKEND_LLM.md §LLM API key`.

### Tier 2 — Kube deployment (`DATASPOKE_KUBE_*`)

Same convention in both profiles; values differ.

- `DATASPOKE_KUBE_CLUSTER` — kubectl context
- `DATASPOKE_KUBE_DATASPOKE_NAMESPACE` — namespace for the umbrella chart
- `DATASPOKE_KUBE_IMAGE_REGISTRY` — registry prefix for built images
- `DATASPOKE_KUBE_CLOUD_VENDOR` — image-build dispatch: `GCP` (Cloud Build,
  no local Docker), `AWS` (ECR login + ensure-repository + local Docker build
  and push, authenticating with `DATASPOKE_AWS_PROFILE`), or empty (local
  Docker build + push). See §Image Builds.
- `DATASPOKE_KUBE_INGRESS_MODE` — `managed` (default) or `shared`. `managed`:
  DataSpoke installs and owns an nginx-ingress controller + LoadBalancer
  (GKE Autopilot / minikube). `shared`: DataSpoke reuses a pre-existing
  cluster ingress controller and installs nothing (AWS/EKS). See §Ingress.
- `DATASPOKE_KUBE_INGRESS_CLASS` — IngressClass name. Currently the only
  supported value is `nginx`: DataSpoke's Ingress resources hardcode this class
  (`values{,-dev}.yaml`, `install.sh`) and carry nginx-specific annotations, so
  the variable is consulted only in shared mode, where the install verifies the
  class exists. In managed mode the installed controller registers the fixed
  `nginx` class from `values-dev.yaml`. Non-`nginx` classes are future work
  (see §Ingress).
- `DATASPOKE_KUBE_INGRESS_IP` — managed: populated by the nginx-ingress
  install from the LoadBalancer external IP; shared: blank (no owned
  LoadBalancer); prod: operator-supplied as needed.
- `DATASPOKE_KUBE_INGRESS_DOMAIN` — managed: derived `<IP>.nip.io`; shared:
  operator-pre-set to a real cluster-published hostname (e.g.
  `dataspoke-dev.your-host.com`, DNS published by the cluster's external-dns);
  prod: operator-supplied.
- `DATASPOKE_KUBE_INGRESS_SCHEME` — `http` (default, both modes) or `https`.
  Selects the URL scheme for every ingress-domain-based URL the dev install
  path builds (frontend config values, `src/frontend/.env.local`
  `NEXT_PUBLIC_*`, the post-login redirect, the DataHub OIDC base, post-install
  seed endpoints, `health-check.sh` probes, the host-bearing `DATASPOKE_TEST_*`
  URLs, and printed access URLs). Set `https` when the shared controller
  terminates TLS in front of the virtual hosts (it emits HSTS, so HTTP pages
  break under mixed-content/auto-upgrade). Validated by the `ingress_scheme()`
  helper in `bin/lib/helpers.sh`; any other value errors. IP:port TCP endpoints
  (dev-lock, Kafka, Postgres) bypass the ingress and never take the scheme.
  Changing the scheme changes the Google OAuth redirect URI the dev DataHub
  OIDC login registers — the operator must register the matching URI.
- `DATASPOKE_KUBE_INGRESS_TLS_SECRET` — optional, default empty. When set, the
  dev install emits `tls:` blocks referencing this Kubernetes TLS Secret on the
  three dev ingresses DataSpoke owns (API, frontend subchart, Airflow
  chart-native). Leave empty when the shared controller terminates TLS with a
  controller-level or wildcard cert (e.g. behind an ALB) and per-Ingress TLS
  config is unnecessary. See §Ingress.

### Tier 3 — Dev-only inputs (`DATASPOKE_DEV_*`)

Operator-supplied inputs for the dev peripheral install. Application pods never
read these.

- Peripheral namespaces: `_KUBE_DATAHUB_NAMESPACE`,
  `_KUBE_LANGFUSE_NAMESPACE`, `_KUBE_DUMMY_DATA_NAMESPACE`
- DataHub chart versions: `_KUBE_DATAHUB_CHART_VERSION`,
  `_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION`
- DataHub install inputs: `_DATAHUB_MYSQL_ROOT_PASSWORD`, `_DATAHUB_MYSQL_PASSWORD`
- Langfuse install internals: `_LANGFUSE_NEXTAUTH_SECRET`, `_LANGFUSE_SALT`,
  `_LANGFUSE_ENCRYPTION_KEY`, `_LANGFUSE_CLICKHOUSE_PASSWORD`,
  `_LANGFUSE_MINIO_{ROOT_USER,ROOT_PASSWORD}`, `_LANGFUSE_POSTGRES_PASSWORD`,
  `_LANGFUSE_REDIS_PASSWORD`
- Langfuse headless-init credentials: `_LANGFUSE_INIT_USER_PASSWORD`
  (auto-generated by `langfuse.sh` and persisted). The matching email / org /
  project identifiers (`INIT_USER_EMAIL`, `INIT_USER_NAME`, `INIT_ORG_ID`,
  `INIT_ORG_NAME`, `INIT_PROJECT_ID`, `INIT_PROJECT_NAME`) default inside
  `langfuse.sh` and only need explicit `.env` entries to override.
- Dummy data inputs: `_DUMMY_DATA_KAFKA_INSTANCE`, `_DUMMY_DATA_POSTGRES_USER`,
  `_DUMMY_DATA_POSTGRES_PASSWORD`, `_DUMMY_DATA_POSTGRES_DB`
- LLM seed: `_LLM_PROVIDER`, `_LLM_API_KEY`, `_LLM_MODEL` — written into the
  `dataspoke-llm-secret` Secret and PATCHed into `/admin/conf`
- Google OAuth credentials: `_GOOGLE_OAUTH_CLIENT_ID`,
  `_GOOGLE_OAUTH_CLIENT_SECRET` — passed to the DataHub peripheral install
  for DataHub-side OIDC (see §DataHub) and seeded into
  `dataspoke-secrets` (`DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET`) and the API
  chart values (`auth.googleClientId`). Absence leaves OAuth disabled on
  both DataSpoke and DataHub.
- Test harness: `_ENV_LOCK_PREACQUIRED` (set by outer wrappers that already
  hold the dev-env lock)

### Tier 4 — Test access (`DATASPOKE_TEST_*`)

Auto-populated by `install.sh` post-install via `_sync_env_from_secret`. Read by
`tests/integration/{conftest.py,util/*}` for laptop-side access to in-cluster
services. App pods never read these.

The **laptop-side host** these point at is ingress-mode-dependent: in managed
mode it is the LoadBalancer external IP (TCP passthrough on the owned
controller); in shared mode it is `127.0.0.1`, reached via
`bin/port-forward.sh` on the same canonical ports. The TCP service ports
(9201/9202/9005/9102/9104/9221) are identical in both modes. The `_PORT`
fields are these fixed passthrough ports, carried statically in `.env.dev.example`;
only the host-bearing values (`_HOST`, `_HOST_PORT`, `_KAFKA_BROKERS`,
`DATASPOKE_TEST_LOCK_URL`) are written by `install.sh`.

- DataSpoke subsystem: `DATASPOKE_TEST_POSTGRES_{HOST,PORT,USER,PASSWORD,DB}`,
  `DATASPOKE_TEST_REDIS_{HOST,PORT,PASSWORD}`,
  `DATASPOKE_TEST_AIRFLOW_{URL,USER,PASSWORD}`,
  `DATASPOKE_TEST_INTERNAL_TOKEN`,
  `DATASPOKE_TEST_JWT_SECRET_KEY` (conftest promotes it to `DATASPOKE_JWT_SECRET_KEY`
  so locally-minted JWTs verify against the API pod)
- DataHub access: `DATASPOKE_TEST_DATAHUB_{GMS_URL,TOKEN,KAFKA_BROKERS,FRONTEND_URL}` —
  `FRONTEND_URL` is the browser-facing UI URL, carried separately because it is not
  derivable from `GMS_URL`; the integration reset helpers restore it into
  `peripheral_config` so a reset leaves the dev UI with a working DataHub link
- Langfuse access: `DATASPOKE_TEST_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}`
- Dev-lock access: `DATASPOKE_TEST_LOCK_URL` — full base URL of the dev-env lock
  service (`http://<host>:9221`, host per the laptop-side rule above). The
  integration and E2E lock protocol uses `$DATASPOKE_TEST_LOCK_URL/lock/...`.
- Dummy data source access: `DATASPOKE_TEST_DUMMY_DATA_{POSTGRES_HOST,POSTGRES_PORT,KAFKA_BROKERS}`
  — laptop-side, mode-dependent host (per the rule above), used by tests that
  read the example source directly.
- `DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT` — **in-cluster**
  cluster-DNS address of the example Postgres
  (`example-postgres.<dummy-data-ns>.svc.cluster.local:5432`),
  **mode-independent**. Used by the in-cluster API pod when it builds
  ingestion source recipes, so it is always the cluster-internal address
  regardless of how a laptop reaches the same database.

### Policies

- Password policy: 16+ chars, mixed case, at least one special character.
- API keys: never committed; `.env` is gitignored. CI/CD injects via K8s
  Secrets or a secrets manager.

---

## Namespace Sourcing

Every namespace DataSpoke deploys into is operator-chosen, declared once in
`helm-charts/.env.<profile>` and sourced from there by every consumer. The four vars are:

| Var | Namespace |
|---|---|
| `DATASPOKE_KUBE_DATASPOKE_NAMESPACE` | umbrella chart (API, Airflow, PG, Redis, dev-lock) |
| `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` | DataHub peripheral install |
| `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE` | Langfuse dev peripheral chart |
| `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE` | dummy-data peripheral install |

`.env.dev.example` ships illustrative values (`dataspoke-01`, `datahub-01`,
`langfuse-01`, `dataspoke-dummy-data-01`). These are examples, not contractual
names — an operator may pick any DNS-label-safe namespace.

**No baked example default.** Scripts (`bin/*.sh`), tests
(`tests/integration/{conftest.py,util/*}`), and chart values source the four
vars with no embedded fallback to an example name. A consumer that reads an
unset var fails fast rather than silently targeting the wrong namespace — a
wrong default would route an install or a destructive reset at a namespace the
operator never named.

**Static namespace-embedding YAML is rendered at install.** The one place a
namespace must appear inside a static manifest — the nginx-ingress `tcp:`
service map in `dev-peripherals/nginx-ingress/values.yaml`, which names the
backend Services by `<namespace>/<service>` — carries `__*_NS__` placeholders
(`__DATASPOKE_NS__`, `__DATAHUB_NS__`, `__DUMMY_DATA_NS__`) that the install
substitutes from `.env` via `sed`, mirroring the existing
`__DATAHUB_INGRESS_HOST__` rendering in `bin/dev-peripherals/datahub.sh`.

**Kafka `advertisedListeners`** is not embedded statically either:
`datahub.sh` injects it with `--set-string` from the datahub namespace plus the
resolved ingress host, so the advertised EXTERNAL listener always matches the
operator's namespace and host.

---

## The .env File

| Path | Tracked | Purpose |
|---|---|---|
| `helm-charts/.env.dev` | gitignored | Per-developer dev-profile values |
| `helm-charts/.env.prod` | gitignored | Per-cluster prod-profile values |
| `helm-charts/.env.dev.example` | tracked | Dev canonical listing (three sections) |
| `helm-charts/.env.prod.example` | tracked | Prod operator template (deployment shape only) |

The runtime env file is profile-named (`.env.<profile>`); copy the matching
`.example` to create it. `install.sh`/`uninstall.sh` resolve it from `--env-file`
or default to `.env.<profile>`. No auto-rename shim is provided.

Dev layout: three top-level sections — Kube deployment operator inputs, Dev
profile operator inputs, Auto-populated block (ingress + `DATASPOKE_TEST_*`).
`bin/*.sh` scripts source it; `tests/integration/conftest.py` loads it for
integration tests.

Prod `.env.prod` contains only the `DATASPOKE_KUBE_*` deployment-shape vars.
All credentials are managed via a pre-created K8s Secret (`secrets.existingSecret`
in the values overlay) — no credentials in `.env` for prod operators.

In dev, the **auto-populated** block is written by install scripts, not edited by
hand: in managed mode `DATASPOKE_KUBE_INGRESS_{IP,DOMAIN}` (by
`dev-peripherals/nginx-ingress.sh`; shared mode leaves `INGRESS_IP` blank and reads
the operator-pre-set `INGRESS_DOMAIN`), `DATASPOKE_TEST_DATAHUB_*` (by
`dev-peripherals/datahub.sh`), `DATASPOKE_TEST_LANGFUSE_*` (by
`dev-peripherals/langfuse.sh`), and the full `DATASPOKE_TEST_*` subsystem block —
including `DATASPOKE_TEST_LOCK_URL` and
`DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT` — by `install.sh`
post-install (`_sync_env_from_secret` extracts credentials from the
`dataspoke-secrets` K8s Secret; the host-bearing vars take the laptop-side
TCP host for the active ingress mode).

---

## Configuration Flow

```
.env.dev  →  bin/install.sh (dev)
              │
              ├─ _ensure_dataspoke_secrets
              │    dev: auto-generate dataspoke-secrets (openssl rand) on first install;
              │         skip if Secret already exists (idempotent)
              │    prod: verify secrets.existingSecret Secret is present; fail fast if missing
              │
              ├─ helm upgrade -f values{-dev}.yaml
              │    dataspoke-secrets referenced via secretRef.name in api-deployment.yaml
              │    postgresql / redis subcharts reference dataspoke-secrets via auth.existingSecret
              │    airflow metadata DB wired via dataspoke-airflow-metadata-db Secret
              │       │
              │       ▼
              │  ConfigMap (dataspoke-app-config) + Secret (dataspoke-secrets)
              │       │
              │       ▼
              │  Deployment envFrom → container env vars (DATASPOKE_* names)
              │
              ├─ _sync_env_from_secret
              │    Extract dataspoke-secrets values → append DATASPOKE_TEST_* block to .env.dev
              │    Also appends DATASPOKE_TEST_DATAHUB_*, DATASPOKE_TEST_LANGFUSE_*,
              │    DATASPOKE_TEST_DUMMY_DATA_* from peripheral install outputs
              │
              └─ post-install/seed-*.sh
                       │
                       ▼
                  PATCH /internal/admin/{peripherals,conf} → DB tables
                       │
                       ▼
                  App reads peripheral_config + runtime_config from DB
                  App reads LLM key from dataspoke-llm-secret via K8s API
```

### Prod operator workflow

The prod install covers the chart and the admin-user seed. Peripheral wiring is
the operator's, performed against the running deployment.

| # | Step | Interface |
|---|---|---|
| 1 | Pre-create the credentials Secret with all twelve keys (see §Secret keys below) | Any secrets manager (ExternalSecrets Operator, Vault Agent, SealedSecrets) or `kubectl create secret generic` |
| 2 | Write the values overlay: `secrets.existingSecret`, ingress hosts, TLS, registry, replica counts | Start from `helm-charts/values-prod.example.yaml` |
| 3 | Install — the chart, then the automatic admin seed | `bin/install.sh --profile prod --image-tag <tag> --values <overlay.yaml>` |
| 4 | **Rotate the default admin credential — required** | `PATCH /api/v1/auth/me` |
| 5 | Register peripherals | `/api/v1/admin/peripherals/{datahub,langfuse,smtp}` and `/api/v1/admin/conf` (LLM provider/model/key) |

The literal copy-paste command sequence, including Secret-creation examples and
verification probes, lives in [`helm-charts/README.md`](../../helm-charts/README.md).

**Pre-flight is a hard gate.** Before touching the chart the prod install fails
fast on: a missing `DATASPOKE_KUBE_INGRESS_CLASS` IngressClass; a missing
credentials Secret; any of the twelve required keys absent or empty;
`DATASPOKE_JWT_SECRET_KEY` still set to the dev default;
`DATASPOKE_AIRFLOW_USER` equal to `admin`; `DATASPOKE_AIRFLOW_PASSWORD` empty or
`admin`; `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` still the dev placeholder. An
explicit `--image-tag` is also required, so a shared registry never receives the
mutable `:dev` tag.

**The admin seed runs automatically.** After the chart phase, the prod install
invokes `seed-admin-user.sh` unless `--skip-seed` is passed. It POSTs
`/internal/admin/bootstrap`, which is idempotent — it returns `created: false`
when any Admin already exists, so re-running an install is safe. The endpoint
makes no external call, so a 503 from it means the API's own Postgres is
unreachable, not a peripheral problem.

**Rotation is required, not advisory.** A first install therefore returns with
an active Admin account whose credentials — `dataspoke@dataspoke.local /
dataspoke` — are published in this repository. The deployment is not
production-ready until step 4 rotates it. Who can reach that account depends on
the operator's ingress controller and network posture, which the prod profile
does not own or configure; the chart adds no source-range restriction or
inbound policy of its own. Operators who want no default credential to exist at
all install with `--skip-seed` and seed deliberately later (see below).

**`api.ingress` host is load-bearing.** `seed-admin-user.sh` addresses the admin
API at `api.<DATASPOKE_KUBE_INGRESS_DOMAIN>`, so the overlay's `api.ingress` host
must be exactly that name or the seed step cannot reach the API and the install
reports a failure at the last phase.

**Seeding by hand.** Under `--skip-seed`, or to re-run the seed after fixing a
failure, invoke the script directly:

```
ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-admin-user.sh
```

The `ENV_FILE=` prefix is required — the script defaults it to `.env.dev`.
The install's own invocation needs no prefix because `install.sh` exports the
resolved env file for child scripts.

**Step 5 is the operator's, not the installer's.** DataHub URL/token, Langfuse
host/keys, and LLM provider/model/key are all registered at runtime through the
admin API (see §Configuration Flow and §Profiles). Until then the dependent
features stay inert rather than failing the install.

### ConfigMap keys (non-sensitive)

`DATASPOKE_POSTGRES_{HOST,PORT,DB}`,
`DATASPOKE_REDIS_{HOST,PORT}`, `DATASPOKE_AIRFLOW_{URL,CALLBACK_BASE_URL}`,
plus the four chart-values-only keys — `DATASPOKE_CORS_ORIGINS`,
`DATASPOKE_COOKIE_SECURE`, `DATASPOKE_GOOGLE_OAUTH_CLIENT_ID`,
`DATASPOKE_OAUTH_POST_LOGIN_REDIRECT` — which come from chart values, not `.env`
(their source values and roles are in §Configuration — Four-Tier Env Vars).
`DATASPOKE_AIRFLOW_CALLBACK_BASE_URL` is hardcoded in the chart (`http://dataspoke-api:8002`);
it is not derived from `.env`.

### Secret keys (`dataspoke-secrets`, mounted via `envFrom`)

Twelve keys consumed by app pods in both dev and prod:

`DATASPOKE_POSTGRES_{USER,PASSWORD}`, `DATASPOKE_POSTGRES_DB`,
`DATASPOKE_REDIS_PASSWORD`,
`DATASPOKE_AIRFLOW_{USER,PASSWORD}`,
`DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY`, `DATASPOKE_AIRFLOW_JWT_SECRET`,
`DATASPOKE_INTERNAL_TOKEN`, `DATASPOKE_JWT_SECRET_KEY`,
`DATASPOKE_OAUTH_STATE_SECRET`, `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET`.

In dev, `install.sh` auto-generates this Secret — the OAuth state secret and
JWT signing key are random; the Google client secret is sourced from
`DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET` in `.env` (placeholder if absent,
which causes the OAuth callback to fail until the operator supplies a real
value). In prod, the operator pre-creates the whole Secret and points the
chart at it via `secrets.existingSecret: <name>`.

### DB-backed (no env var)

- `peripheral_config` table — non-secret connection fields for DataHub
  (`gms_url`, `frontend_url`, `kafka_brokers`, `service_corpuser_urn`, `default_env`), Langfuse
  (`host`, `public_key`, `project_id`, `environment_tag`), and SMTP
  (`host`, `port`, `username`, `from_address`, `use_tls`) — updated via
  `/api/v1/admin/peripherals/{datahub,langfuse,smtp}`. Per-peripheral secret
  fields (`datahub.token`, `langfuse.secret_key`, `smtp.password`) are
  routed by the PATCH handler to dedicated K8s Secrets, never to the DB —
  see Out-of-band Secrets below.
- `runtime_config` table — LLM provider/model, debate/RAG/iteration tunables,
  and `auth_datahub_corp_group` (string, default `dataspoke-users` — names
  the DataHub corpGroup that marks DataSpoke-managed users) — updated via
  `/api/v1/admin/conf`.

### Out-of-band Secret

`dataspoke-llm-secret` (key `api_key`) — LLM provider API key. Provisioned by
`install.sh` from `DATASPOKE_DEV_LLM_API_KEY` in dev; by an operator
(`kubectl` / External Secrets Operator) in prod. The API reads it at runtime
via the K8s API (`api-secret-reader` RBAC) and rotates it online via
`/api/v1/admin/conf`. Not Helm-managed — `helm upgrade` would clobber online
rotations.

---

## Image Builds

Dockerfiles live under `docker-images/{api,airflow,postgres}/Dockerfile`.
The Airflow image bakes DAGs in via `COPY src/workflows/dags/`; the
PostgreSQL image layers `pgvector` + Apache AGE on the `bitnamilegacy/postgresql`
PG 17 runtime (see §Dependencies for why the base is `bitnamilegacy`).

`bin/build-image.sh <name> [<tag>]` (`<name>` ∈ {api, airflow, postgres,
frontend}) is the unified entrypoint. It dispatches on
`DATASPOKE_KUBE_CLOUD_VENDOR`:

- `GCP` → `gcloud builds submit` (no local Docker required); the GCP project is
  parsed from the `<region>-docker.pkg.dev/<project>/<repo>` registry host.
- `AWS` → `aws ecr get-login-password` (using the required `DATASPOKE_AWS_PROFILE`
  CLI profile), an idempotent `describe-repositories` / `create-repository`
  ensure step, then a local `docker build --platform ${DATASPOKE_IMAGE_PLATFORM:-linux/amd64}`
  and `docker push`. The AWS region is parsed from the
  `<acct>.dkr.ecr.<region>.amazonaws.com` registry host. Set
  `DATASPOKE_DOCKER_SUDO=true` to prefix `docker` with `sudo` on hosts that
  require root for the daemon socket.
- empty → local `docker build` + `docker push`.

**Parallelism**: the install script's bootstrap phase runs all three image
builds with bash `&`/`wait`, optionally alongside the DataHub and Langfuse
installs (~10-minute concurrent path vs ~20-minute serial). Image build
output is buffered per branch so the operator sees one stream finish at a
time.

The umbrella chart pulls `${REGISTRY}/postgres:dev`, `${REGISTRY}/airflow:dev`,
`${REGISTRY}/api:dev` (or the operator-supplied tag in prod). Updating a DAG
or DataSpoke code requires a rebuild + `kubectl rollout restart` — both
automated by `bin/install.sh --components api` (or `airflow`, `postgres`).

---

## Dev-Only Peripherals

Each `bin/dev-peripherals/*.sh` is an idempotent installer for one peripheral.
Run independently or as part of a full `--profile dev` install.

### nginx-ingress

`dev-peripherals/nginx-ingress.sh` behaves per `DATASPOKE_KUBE_INGRESS_MODE`
(see §Ingress for the full mode contract).

**Managed mode** (default — GKE Autopilot / minikube):

| Aspect | Value |
|---|---|
| Namespace | `ingress-nginx` |
| Source | `dev-peripherals/nginx-ingress/values.yaml` (helm release) |
| Function | Installs and owns a single LoadBalancer for all dev namespaces — HTTP virtual hosts (port 80) + TCP passthrough (PG 9201, Redis 9202, DataHub Kafka 9005, example PG 9102, example Kafka 9104, lock 9221) |
| Writes to .env.dev | `DATASPOKE_KUBE_INGRESS_IP`, `DATASPOKE_KUBE_INGRESS_DOMAIN`, plus the IP-derived `DATASPOKE_TEST_*` host/broker vars |

**Shared mode** (AWS/EKS, or any cluster with a pre-existing controller): the
script installs nothing. It verifies the `DATASPOKE_KUBE_INGRESS_CLASS`
IngressClass (default `nginx`) exists and that the operator has pre-set
`DATASPOKE_KUBE_INGRESS_DOMAIN`, then early-exits — leaving the controller
untouched (`uninstall.sh` likewise leaves it alone). The shared controller
serves the virtual hosts over the scheme set by `DATASPOKE_KUBE_INGRESS_SCHEME`
(`http` or `https`); the TCP datastores are not published on it, independent of
the scheme, and are reached on `127.0.0.1` via `bin/port-forward.sh`. No
`INGRESS_IP` is written (it stays blank).

### DataHub

| Chart | Version | App Version |
|---|---|---|
| `datahub/datahub-prerequisites` | 0.3.0 | — |
| `datahub/datahub` | 1.0.1 | v1.6.0 (pinned via `global.datahub.version` override) |

Dev decisions:

- **OpenSearch over Elasticsearch** — prerequisites chart 0.3.0 default; same
  ES-client wire protocol.
- **Kafka in KRaft mode** — single controller pod also serves as broker
  (`controller.controllerOnly=false`, `broker.replicaCount=0`), no Zookeeper.
- **No Neo4j** — OpenSearch provides graph backend including multi-hop
  lineage; saves ~2 Gi RAM + 10 Gi PVC.
- **No Schema Registry** — DataHub uses an internal schema registry
  (`type: INTERNAL`).
- **No `--wait` on Helm install** — `datahub-system-update` bootstrap takes
  5–10 min; the script uses custom poll-based readiness instead.
- **Relaxed liveness probes** on GMS and frontend to tolerate transient
  OpenSearch restarts.
- **Frontend ingress uses `className: "nginx"`** — the subchart (0.3.4) uses
  `className`, not `ingressClassName`; the wrong key is silently dropped and
  GKE falls back to provisioning a GCE LoadBalancer.
- **`datahub-gms` and `datahub-frontend` Services are `ClusterIP`** — the chart
  defaults both to `LoadBalancer`, which on AWS/EKS provisions redundant NLBs
  beside the nginx Ingress and exposes the GMS metadata API plus jmx/prometheus
  ports. DataSpoke overrides both to `ClusterIP`; external access is solely
  through the nginx Ingress (GMS at `datahub.<domain>/gms`, frontend at
  `datahub.<domain>/`).

Service name prefixes: `datahub-prerequisites-*` for the prerequisites
release (MySQL, Kafka controller); `opensearch-cluster-master` for the
OpenSearch subchart's own release.

Writes to .env.dev: `DATASPOKE_TEST_DATAHUB_GMS_URL`, `DATASPOKE_TEST_DATAHUB_TOKEN`
(generated PAT), `DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS`,
`DATASPOKE_TEST_DATAHUB_FRONTEND_URL` (browser-facing UI URL).

**Google OIDC SSO**: `helm-charts/dev-peripherals/datahub.sh` configures DataHub's
frontend to authenticate users via the same Google OAuth client as DataSpoke.
On a user's first DataHub login, DataHub just-in-time provisions their corpuser,
yielding `urn:li:corpuser:<email>` — the URN DataSpoke addresses from its own
`users.email` when projecting role and marker-group membership (see
[feature/AUTH.md §DataHub OIDC JIT provisioning](AUTH.md#datahub-oidc-jit-provisioning)).
Two `oidcAuthentication` values are **required** for that URN agreement:
`user_name_claim=email` and `user_name_claim_regex=(.*)`. DataHub's default
regex `([^@]+)` strips the domain and produces `urn:li:corpuser:bob`, which no
DataSpoke row addresses — prod operators wiring their own DataHub must set both.
The peripheral install
passes the following values into the `datahub/datahub` chart when both
`DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID` and
`DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET` are set:

| Helm value | Value |
|------------|-------|
| `datahub.global.datahub.auth.oidc.enabled` | `true` |
| `datahub.global.datahub.auth.oidc.clientId` | `$DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID` |
| `datahub.global.datahub.auth.oidc.clientSecret` | `$DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET` |
| `datahub.global.datahub.auth.oidc.discoveryUri` | `https://accounts.google.com/.well-known/openid-configuration` |
| `datahub.global.datahub.auth.oidc.userIdClaim` | `email` |

When either credential is absent, OIDC stays disabled and DataHub falls back
to native login. In prod, DataHub is operator-managed; the same OIDC values
are set in the operator's DataHub deployment outside the DataSpoke umbrella
chart.

### Langfuse

Dev-only peripheral chart `helm-charts/dev-peripherals/langfuse/` installed in `langfuse-01`
(default) with bundled Postgres, Redis, ClickHouse, MinIO subcharts; its single
`values.yaml` holds dev-profile values, and in prod the operator supplies their own
Langfuse wired via the admin API. The
`langfuse.sh` script auto-generates the NextAuth/salt/encryption/ClickHouse/
MinIO/Postgres/Redis secrets on first run, creates the `dataspoke` project +
public/secret API keys, and writes them back to `.env.dev`.

Writes to .env.dev: `DATASPOKE_TEST_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}` plus
the Langfuse internals (`DATASPOKE_DEV_LANGFUSE_*`) on first install.

**Startup ordering**: the Langfuse `web` and `worker` containers run ClickHouse
migrations in their entrypoint before serving. On a cold install ClickHouse is
not yet accepting connections on `:9000`, so both gate on ClickHouse TCP
readiness via a `wait-for-clickhouse` init container that blocks until the port
is open. Without it the pods crash-loop until ClickHouse happens to come up.

### Dummy data

Plain Kubernetes manifests under `dev-peripherals/dummy-data/manifests/` in the
`dataspoke-dummy-data-01` namespace.

| Component | Image | Mem Limit | Storage | Service |
|---|---|---|---|---|
| PostgreSQL | `postgres:15` | 512 Mi | 5 Gi PVC | `example-postgres:5432` |
| Kafka | `apache/kafka:3.9.0` (KRaft) | 1 Gi | 4 Gi PVC | `example-kafka:9092` (internal), `:9094` (EXTERNAL) |

An `example-kafka-topic-init` Job waits for the broker and creates a single
`example_topic` — a bring-up smoke check that proves the broker accepts topic
creation. No test or DAG consumes it; it is distinct from the two Imazon seed
topics below.

Separate from DataHub's prerequisites Kafka. Simulates an external data
source for ingestion testing. The Kafka EXTERNAL listener advertises the
laptop-side TCP host on `:9104` — the LoadBalancer IP in managed mode (TCP
passthrough on nginx-ingress), or `127.0.0.1` in shared mode (reached via
`bin/port-forward.sh`).

Imazon-themed seed data (5 schemas, 6 tables, 200 rows; 2 Kafka topics, 35
messages; 8 DataHub dataset entities) is loaded by
`uv run python -m tests.integration.util --reset-seed` — not by the install
script. See `TESTING.md §Test Data Design`.

### Dev-lock

Advisory mutex for coordinating multi-tester access. Lightweight Python HTTP
server (pure stdlib, no deps) in the DataSpoke namespace.

| Resource | Details |
|---|---|
| Deployment | `dev-lock` — 1 replica, `python:3.13-slim`, 64 Mi / 100m CPU |
| Service | `dev-lock` — ClusterIP, port 8080; ingress TCP passthrough on `9221` |

Lock state is in-memory only — resets on pod restart. Full protocol in
`TESTING.md §Integration Testing`; HTTP surface (GET/POST acquire/release +
DELETE force-release) in `helm-charts/README.md`.

---

## Post-Install Seeding

Runs after the umbrella chart's API deployment is Ready. The dev profile runs
all three scripts; the prod profile runs `seed-admin-user.sh` only — peripheral
and runtime config are the operator's, per §Prod operator workflow. Each script
is standalone and env-file-driven, so any of them can also be invoked by hand.

| Script | Effect |
|---|---|
| `bin/post-install/seed-peripheral-config.sh` | PATCH `/internal/admin/peripherals/datahub` with `{gms_url, frontend_url, kafka_brokers}` and `/internal/admin/peripherals/langfuse` with `{host, public_key}`. When set in `.env.dev`, the script also forwards optional operator-supplied non-secret fields from `DATASPOKE_DEV_*` env vars: DataHub `service_corpuser_urn` and `default_env`; Langfuse `project_id` (from `DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID`) and `environment_tag`. The secret fields — DataHub PAT `token` and Langfuse `secret_key` — are placed into K8s Secrets out-of-band by the install script before the API pod starts (the API reads them via RBAC); the seed script does not send them through the admin API — only non-secret fields go through it. |
| `bin/post-install/seed-runtime-config.sh` | PATCH `/internal/admin/conf` with `{llm_provider, llm_model}` from `DATASPOKE_DEV_LLM_{PROVIDER,MODEL}`, then a second PATCH setting the four `stub_*` dependency flags (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`) to `true` for the dev profile. |
| `bin/post-install/seed-admin-user.sh` | POST `/internal/admin/bootstrap` to idempotently seed the built-in `dataspoke@dataspoke.local / dataspoke` Admin user (returns `{created: false}` when any Admin already exists). The endpoint makes no DataHub call, so this step has no ordering dependency on peripheral seeding and succeeds on a fresh install before DataHub is wired. See [feature/AUTH.md §Built-in Bootstrap Admin](AUTH.md#built-in-bootstrap-admin). |

Auth: both use the `DATASPOKE_INTERNAL_TOKEN` read from the `dataspoke-secrets` Secret
(mounted on the API pod via `envFrom`).

Skip with `--skip-seed`; useful when a previous install already seeded
peripheral config and the operator wants to preserve their PATCHes.

In prod the admin seed is automatic and the other two scripts do not run: the
operator performs their equivalents through `/api/v1/admin/peripherals/*` and
`/api/v1/admin/conf` against the running deployment. Rotating the seeded default
credential is a required follow-up — see §Prod operator workflow.

---

## Resource Sizing

### Production defaults

| Component | Replicas | CPU Req / Limit | Mem Req / Limit | PV |
|---|---|---|---|---|
| frontend | 2 | 250m / 500m | 256 Mi / 512 Mi | — |
| api | 2 | 500m / 1000m | 512 Mi / 1024 Mi | — |
| event-consumer† | 1 | 250m / 500m | 512 Mi / 1024 Mi | — |
| postgresql | 1 | 1000m / 2000m | 2048 Mi / 6144 Mi | 50 Gi (custom image with `pgvector` + Apache AGE) |
| redis | 1 + 1 | master 250m / 500m; replica 100m / 150m | master 256 Mi / 512 Mi; replica 128 Mi / 192 Mi | 8 Gi per pod (master + replica) = 16 Gi |
| airflow (api-server + scheduler + triggerer + dag-processor) | 1+1+1+1 | per-component; see `values.yaml` | per-component; see `values.yaml` | none — task logs in emptyDir (2 Gi cap), DAGs baked into the custom image |
| **Total** (excludes event-consumer) | | **4075m / 8200m** | **6.5 Gi / 15.7 Gi** | **66 Gi** (postgresql 50 + redis 2×8) |

† event-consumer disabled by default — add ~250m / 500m CPU + ~512 Mi / 1024 Mi
memory when enabled.

The Total row sums the rendered pod specs (`helm template` against `values.yaml`)
rather than the per-pod cells above, so it also carries the Airflow logGroomer
sidecars that the component rows do not break out. Per-component Airflow
requests/limits differ across api-server, scheduler, triggerer, and
dag-processor; the concrete values live in `values.yaml`.

### Dev minimums

Cluster capacity: **8 CPU / 24 GB RAM**. Sum of memory *limits* ≈ 25 GiB (above
24 GB); sum of *requests* ≈ 13 GiB. Pods rarely hit limits simultaneously, so
limits are generous to absorb transient spikes (OpenSearch off-heap,
`mysql_upgrade`, JVM GC).

Storage behaves differently from memory: PVC requests are provisioned in full,
so the storage line is a **floor, not an estimate**. The umbrella chart alone
requests ~18 Gi in dev — postgresql (10 Gi) and the redis master (8 Gi). Dummy
data adds 9 Gi (example-postgres 5 Gi + example-kafka 4 Gi); DataHub and
Langfuse add more on top (Langfuse ~26 Gi, DataHub prerequisites MySQL 10 Gi,
plus OpenSearch/Kafka chart defaults). Size the dev disk from the umbrella
floor upward rather than from a single headline number.

Airflow contributes no storage: `workers.celery.persistence` and
`triggerer.persistence` are pinned off, so task logs use emptyDir bounded by
`logs.emptyDirConfig.sizeLimit` (2 Gi) and the log-groomer sidecars' 15-day
retention. Left at chart defaults these two knobs render 100 Gi log PVCs each.
Disabling them also makes the scheduler and triggerer render as Deployments
rather than StatefulSets, since the chart keys workload kind off the same
`$stateful` condition.

| Component | Namespace | Mem Limit | Notes |
|---|---|---|---|
| OpenSearch | datahub-01 | 3072 Mi | 1 Gi JVM heap + off-heap cache; `singleNode: true` skips bootstrap checks |
| Kafka (KRaft controller) | datahub-01 | 2048 Mi | 1.5 Gi heap; single pod = controller + broker |
| MySQL (bitnami) | datahub-01 | 1536 Mi | `mysql_upgrade` doubles memory on restart |
| datahub-gms | datahub-01 | 3 Gi | |
| datahub-frontend | datahub-01 | 1 Gi | |
| datahub-mae-consumer | datahub-01 | 1 Gi | |
| datahub-mce-consumer | datahub-01 | 1 Gi | |
| datahub-actions | datahub-01 | 512 Mi | |
| dataspoke-api | dataspoke-01 | 1 Gi | In-cluster API |
| airflow (api-server + scheduler + triggerer + dag-processor) | dataspoke-01 | 4×1 Gi + 3×512 Mi sidecars ≈ 5.5 Gi | LocalExecutor; DAGs baked in |
| postgresql (dataspoke) | dataspoke-01 | 4096 Mi | Custom image; pgvector + AGE |
| redis | dataspoke-01 | 512 Mi | |
| dev-lock | dataspoke-01 | 64 Mi | |
| example-postgres | dataspoke-dummy-data-01 | 512 Mi | |
| example-kafka | dataspoke-dummy-data-01 | 1024 Mi | 4 Gi PVC |
| **Total (limits)** | | **~25 Gi** | |

The dev airflow scheduler and triggerer carry raised resource floors above the
table's nominal split so they survive parse/heartbeat spikes; the concrete
requests/limits live in `helm-charts/dataspoke/values-dev.yaml`.

### Ephemeral storage budget (Autopilot)

GKE Autopilot applies a **1 GiB ephemeral-storage request = 1 GiB limit** per
container whenever the spec omits ephemeral-storage. The webhook **forces
`requests == limits`** at admission — Helm values with a higher `limits.ephemeral-storage`
than `requests.ephemeral-storage` are silently normalized to the request value.

Chatty containers (sustained stdout, emptyDir writes, projected-volume mounts)
exhaust the default within minutes and trigger eviction with `Pod ephemeral
local storage usage exceeds the total limit of containers`. `/opt/airflow/logs`
is an emptyDir on every Airflow component, so it counts against the ephemeral
budget alongside stdout. Explicit ephemeral-storage
limits in the table below prevent that class of eviction. Low-log containers
(dev-lock, redis, frontend, Airflow logGroomer sidecars) keep the Autopilot
default.

The table lists the **configured `limits.ephemeral-storage`**. On Autopilot the
effective ceiling is the paired `requests.ephemeral-storage`, which the umbrella
chart sets to half the limit — 4 Gi for the four Airflow components, 2 Gi for
`dataspoke-api` and `postgresql`. Size against the request, not the limit; the
Airflow `logs.emptyDirConfig.sizeLimit` of 2 Gi is chosen to stay under the 4 Gi
Airflow request for that reason.

| Component | Namespace | Configured limit | Why |
|---|---|---|---|
| datahub-gms | datahub-01 | 8 Gi | High-log: continuous Kafka-listener traces |
| datahub-frontend | datahub-01 | 8 Gi | High-log: Play framework access log per request |
| datahub-mae-consumer | datahub-01 | 8 Gi | High-log: every metadata aspect change event |
| datahub-mce-consumer | datahub-01 | 8 Gi | High-log: every metadata change proposal |
| datahub-actions | datahub-01 | 4 Gi | Medium-log: event-driven actions |
| Kafka KRaft controller | datahub-01 | 4 Gi | Medium-log: broker + controller segments |
| OpenSearch | datahub-01 | 4 Gi | Medium-log: JVM GC + index recovery |
| MySQL | datahub-01 | 4 Gi | Medium-log: slow-query and binlog refs |
| airflow-api-server | dataspoke-01 | 8 Gi | High-log: uvicorn access log per request |
| airflow-scheduler | dataspoke-01 | 8 Gi | High stdout: heartbeat + task scheduling |
| airflow-triggerer | dataspoke-01 | 8 Gi | High stdout: event-loop |
| airflow-dag-processor | dataspoke-01 | 8 Gi | High-log: parse cycle per DAG per interval |
| dataspoke-api | dataspoke-01 | 4 Gi | Medium-log: uvicorn access log |
| postgresql (dataspoke) | dataspoke-01 | 4 Gi | Medium-log: WAL + autovacuum |
| example-kafka | dataspoke-dummy-data-01 | 4 Gi | Medium-log: KRaft broker |
| example-postgres | dataspoke-dummy-data-01 | 4 Gi | Medium-log: WAL + checkpoints |

---

## Ingress & Network Policy

### Ingress

DataSpoke supports two dev ingress modes, selected by
`DATASPOKE_KUBE_INGRESS_MODE`:

| | **Managed** (default) | **Shared** |
|---|---|---|
| Target cluster | GKE Autopilot, minikube | AWS/EKS, or any cluster with a pre-existing controller |
| Controller | DataSpoke installs & owns nginx-ingress + a LoadBalancer | reuses the operator's controller; installs nothing |
| Domain | derived `<IP>.nip.io` from the LoadBalancer IP | operator-pre-set real hostname (DNS published by the cluster, e.g. external-dns) |
| Virtual-host scheme | `http` (typical) | `http` or `https` per `DATASPOKE_KUBE_INGRESS_SCHEME`; `https` when the controller terminates TLS + HSTS |
| Virtual hosts | ✓ (app/api/datahub/airflow/langfuse) | ✓ (same hosts, on the operator's controller) |
| TCP datastores | exposed via LoadBalancer TCP passthrough on the IP | not exposed — reached on `127.0.0.1` via `bin/port-forward.sh` |
| Kafka EXTERNAL listener advertises | `<INGRESS_IP>:<port>` | `127.0.0.1:<port>` |
| Teardown | `uninstall.sh` removes the controller | controller left untouched |

Shared mode is the path for clusters where another system owns the ingress
controller and DNS, so DataSpoke must not install or modify them. Prod follows
the same reuse posture: the operator's controller serves the hosts, with
`values.yaml` ingress hosts and TLS secrets operator-supplied.

Frontend, API, and Airflow each have an `ingress` block in their values
supporting `className` (nginx, alb, traefik, etc.), TLS via cert-manager
annotations, and customizable host/path rules.

For the dev install path, `DATASPOKE_KUBE_INGRESS_TLS_SECRET` (optional, default
empty) controls per-Ingress TLS on the three dev ingresses DataSpoke owns. When
set, the install emits a `tls:` block referencing that Kubernetes TLS Secret on
the API ingress (`api.ingress.tls`), the frontend subchart ingress
(`frontend.ingress.tls`), and the Airflow chart-native ingress
(`ingress.apiServer.hosts[].tls`). When empty there are no per-Ingress TLS
blocks — the case where the shared controller terminates TLS with a
controller-level or wildcard cert (e.g. behind an ALB), so per-Ingress config is
unnecessary. This is orthogonal to `DATASPOKE_KUBE_INGRESS_SCHEME`: the scheme
governs the URLs the install builds, the TLS Secret governs whether the
Ingresses themselves carry TLS.

| Resource | Location | Routes |
|---|---|---|
| `templates/api-ingress.yaml` | umbrella chart | `api.<INGRESS_IP>.nip.io/` → `dataspoke-api:8002` |
| `subcharts/frontend/templates/ingress.yaml` | frontend subchart | `app.<INGRESS_IP>.nip.io/` → `dataspoke-frontend:3000` |
| `airflow.ingress` values | airflow chart (native) | `airflow.<INGRESS_IP>.nip.io/` → `dataspoke-airflow-api-server:8080` |
| `dev-peripherals/datahub/gms-ingress.yaml` | kubectl manifest | `datahub.<INGRESS_IP>.nip.io/gms` → `datahub-datahub-gms:8080` |
| `datahub-frontend.ingress` values | DataHub chart (native) | `datahub.<INGRESS_IP>.nip.io/` → `datahub-frontend:9002` |
| `langfuse.ingress` values | Langfuse chart (native) | `langfuse.<INGRESS_IP>.nip.io/` → langfuse web:3000 |

In **managed** mode, TCP passthrough (PostgreSQL :9201, Redis :9202, DataHub
Kafka :9005, example PG :9102, example Kafka :9104, lock :9221) is handled by
the nginx-ingress `tcp-services` ConfigMap (the `tcp` block in
`dev-peripherals/nginx-ingress/values.yaml`) — no Ingress resource needed. That
block names its backend Services by `<namespace>/<service>`; the namespace
segments carry `__*_NS__` placeholders rendered from `.env` at install (see
§Namespace Sourcing).
Kafka services advertise `<INGRESS_IP>:<port>` as their EXTERNAL listener so
host-side producers/consumers reach them through the controller.

In **shared** mode the operator's controller serves the virtual hosts over
`http` or `https` per `DATASPOKE_KUBE_INGRESS_SCHEME` — a TLS-terminating
controller (emitting HSTS) requires `https` so browser login is not broken by
mixed-content or auto-upgrade. The TCP datastores are independent of the scheme:
they are never on the ingress. `bin/port-forward.sh` runs in the foreground and
`kubectl port-forward`s the same six services to their canonical ports on
`127.0.0.1`; Kafka EXTERNAL listeners advertise `127.0.0.1:<port>`. Integration
tests, `health-check.sh`, and `helm-charts/.env.dev`'s TCP `DATASPOKE_TEST_*`
host values all resolve to `127.0.0.1` while the port-forward holds, regardless
of the virtual-host scheme.

### Network Policy

A NetworkPolicy template allows egress from DataSpoke pods to the DataHub
namespace (GMS :8080, Kafka :9092). Controlled by `networkPolicy.enabled`
(default `false`) and `networkPolicy.datahubNamespace`. Enable in clusters
with default-deny.

---

## Secrets Management

| Family | Owner | Purpose |
|---|---|---|
| **`dataspoke-secrets`** | `install.sh` (dev auto-generate) or operator (prod pre-create) | DataSpoke's own runtime credentials — Postgres user/password/db, Redis password, Airflow user/password/webserver-secret/jwt-secret, internal-auth token, JWT signing key, OAuth state secret, Google OAuth client secret. Twelve keys; mounted `envFrom` on the API Deployment and alembic-migrate init container. |
| **`dataspoke-airflow-metadata-db`** | `install.sh` `_derive_airflow_metadata_secret` (both profiles) | Single key `connection` = full PostgreSQL URI for Airflow's metadata DB. Wired via `airflow.data.metadataSecretName`. |
| **`dataspoke-airflow-api-secret-key`**, **`dataspoke-airflow-jwt-secret`** | `install.sh` `_ensure_airflow_key_secrets` (both profiles) | Projections of the `DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY` / `DATASPOKE_AIRFLOW_JWT_SECRET` keys into the single-key shape (`api-secret-key`, `jwt-secret`) the Airflow chart expects. Wired via `airflow.apiSecretKeySecretName` / `airflow.jwtSecretName`. |
| **Out-of-band Secrets** (`dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret`) | Operator (`kubectl` / ESO) or the app on first PATCH | Tokens/keys that rotate online via `/api/v1/admin/conf` and `/api/v1/admin/peripherals/*`. Not Helm-managed — `helm upgrade` would clobber rotations. The app tolerates their absence (reads as unset). `dataspoke-smtp-secret` (key `password`) backs `/auth/password/reset/request` (see [feature/AUTH.md](AUTH.md)). Note: a Secret of the same name `dataspoke-langfuse-secret` also exists in the Langfuse namespace (`langfuse-01`) carrying the full set of Langfuse pod credentials (NextAuth, salt, ClickHouse, MinIO, Postgres, Redis, init-user); the DataSpoke-side copy holds only the project `secret_key` consumed by the API via RBAC. |
| **User-supplied source credentials** (`dataspoke-source-cred-*`) | Caller (vault path) or operator (reference path) | Credentials for *external sources* registered via ingestion confs. Documented in [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md). The `dataspoke-source-cred-` name prefix is enforced as a security boundary so callers cannot overwrite the above Secrets. |

### Dev — install-time provisioning

`install.sh _ensure_dataspoke_secrets` auto-generates `dataspoke-secrets` with
`openssl rand -hex 32` on first install. The step is idempotent — an existing Secret
is not overwritten (Postgres PV data remains decryptable across reinstalls). Subcharts
reference `dataspoke-secrets` via `auth.existingSecret`. Out-of-band Secrets are
populated only if their seed value is present in `.env`; if absent, the dependent
feature stays disabled until the operator sets it via the admin API.

### Prod

The operator pre-creates `dataspoke-secrets` out-of-band (ExternalSecrets Operator,
Vault Agent, SealedSecrets, or plain `kubectl create secret generic`), then sets
`secrets.existingSecret: <name>` in the values overlay. `install.sh` refuses to
auto-generate Secrets in the prod profile and fails fast with a clear message if the
named Secret is absent.

### API RBAC for source-credential Secrets

When `api.secretReader.enabled` is `true` (default), the umbrella renders
`templates/api-secret-reader-rbac.yaml`: a dedicated ServiceAccount on the
API Deployment, a `Role`, and a matching `RoleBinding`. The Role grants
`get`, `list`, `create`, and `patch` on `secrets` in the API release
namespace. `delete` is intentionally omitted — ingestion-config DELETE does
not auto-clean source-credential Secrets (reference counting is out of
scope; see SECRET_RESOLUTION.md §Open Questions).

The Role is shared between two distinct access patterns:

- **Source-cred reads** (`get` + `list`): the secret resolver reads and
  enumerates `dataspoke-source-cred-*` Secrets. The application-level
  prefix guard in the Kubernetes secret backend (`src/shared/secrets/k8s.py`)
  ensures the resolver never touches
  infra Secrets; RBAC cannot enforce this boundary because the Role is
  namespace-scoped without `resourceNames`.
- **Infra accessor writes** (`get` + `create` + `patch`): the admin
  peripheral accessors (`datahub_secret.py`, `llm_secret.py`,
  `langfuse_secret.py`, `smtp_secret.py`) use create-or-patch semantics
  against their respective fixed-name Secrets (`dataspoke-datahub-secret`,
  `dataspoke-llm-secret`, `dataspoke-langfuse-secret`,
  `dataspoke-smtp-secret`).

Single-namespace policy: no cross-namespace Roles or RoleBindings. Disable
to opt out entirely; the Deployment then falls back to the default
ServiceAccount and the resolver raises `SecretResolverUnavailable` on every
PUT/PATCH that touches `secret_ref`.

---

## Health Check

`bin/health-check.sh` probes each service through nginx-ingress (HTTP
endpoints) or the laptop-side TCP host (TCP services — the ingress IP in
managed mode, `127.0.0.1` via `bin/port-forward.sh` in shared mode). Required before any integration
test run per `TESTING.md §Prerequisites`. On failure, reinstall the
affected subsystem via `bin/install.sh --profile dev --components <name>`.

| Failing service | Component to reinstall |
|---|---|
| dataspoke-postgresql, redis, airflow, api | `dataspoke-infra` |
| datahub-gms, datahub-kafka | `datahub` |
| example-postgres, example-kafka | `dummy-data` |
| dev-lock | `dev-lock` |
| Langfuse | `langfuse` |
| ingress controller | `nginx-ingress` |

---

## Troubleshooting

### Pod evicted: ephemeral local storage usage exceeds limit

GKE Autopilot's 1 GiB ephemeral-storage default per container is exhausted by
chatty containers within minutes. Ensure the affected container has explicit
`ephemeral-storage` entries per §Resource Sizing → Ephemeral storage budget
(Autopilot). If the
evicted container is not in the table, verify it is not writing unexpectedly
large logs.

### OpenSearch OOM-killed during startup

Off-heap usage (Lucene cache, index recovery) spikes above the JVM heap.
Already mitigated — `dev-peripherals/datahub/prerequisites-values.yaml` sets the
memory limit to 3 Gi.

### MySQL OOM-killed on restart

`mysql_upgrade` runs on every start, briefly doubling memory beyond the chart
default. Already mitigated — memory limit set to 1536 Mi.

### Pod stuck in Pending

Insufficient cluster resources. Check `kubectl describe node`. Total memory
*requests* sum ~13 GiB and *limits* ~25 GiB; 24 GB / 8+ CPU is the
recommended headroom.

### datahub-system-update takes 5–10 minutes

Expected on first install — bootstraps all DataHub metadata schemas. The
script polls every 10s with progress logging. Not an error.

### MAE consumer stalled after restart

The embedded MAE consumer in GMS crashes when processing stale MCL messages
accumulated from previous runs. Spring Kafka's error handler shuts the
consumer down permanently, leaving timeseries aspects unindexed in
OpenSearch. Already automated in `datahub.sh` — detects the stalled consumer
group, resets offsets to latest, restarts GMS. If it recurs outside install,
manually reset offsets on `MetadataChangeLog_Timeseries_v1` and
`MetadataChangeLog_Versioned_v1` for group `generic-mae-consumer-job-client`,
then restart the GMS pod.

### Service unreachable via ingress

Target pod not yet Ready, or the nginx-ingress controller has not yet
received an external IP. Verify the controller (`kubectl get pods -n ingress-nginx`)
has an external IP (`kubectl get svc -n ingress-nginx`), then verify the
target pod is `1/1 Running`. Re-run `bin/health-check.sh` once pods are ready.

---

## References

- [DataHub — Deploying with Kubernetes](https://docs.datahub.com/docs/deploy/kubernetes)
- [DataHub Helm chart defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/datahub/values.yaml)
- [DataHub prerequisites defaults](https://github.com/acryldata/datahub-helm/blob/master/charts/prerequisites/values.yaml)
- [Migrating Graph Service Implementation](https://docs.datahub.com/docs/how/migrating-graph-service-implementation)
- [Helm — Chart Dependencies](https://helm.sh/docs/helm/helm_dependency/)
- [Bitnami PostgreSQL Chart](https://github.com/bitnami/charts/tree/main/bitnami/postgresql)
- [Bitnami Redis Chart](https://github.com/bitnami/charts/tree/main/bitnami/redis)
- [Apache Airflow Helm Chart](https://github.com/apache/airflow/tree/main/chart)
- [ARCHITECTURE.md](../ARCHITECTURE.md) — system architecture, env-var convention
- [TESTING.md](../TESTING.md) — testing conventions, dev-env lock protocol
- [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md) — source-credential Secret model
- [BACKEND_LLM.md](BACKEND_LLM.md) — LLM observability + online key rotation
