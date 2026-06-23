# HELM_CHART — DataSpoke Deployment Subsystem

## Table of Contents

1. [Overview](#overview)
2. [Repository Layout](#repository-layout)
3. [Profiles](#profiles)
4. [Installation](#installation)
5. [Uninstallation](#uninstallation)
6. [Umbrella Chart Structure](#umbrella-chart-structure)
7. [Configuration — Four-Tier Env Vars](#configuration--four-tier-env-vars)
8. [The .env File](#the-env-file)
9. [Configuration Flow](#configuration-flow)
10. [Image Builds](#image-builds)
11. [Dev-Only Peripherals](#dev-only-peripherals)
12. [Post-Install Seeding](#post-install-seeding)
13. [Resource Sizing](#resource-sizing)
14. [Ingress & Network Policy](#ingress--network-policy)
15. [Secrets Management](#secrets-management)
16. [Health Check](#health-check)
17. [Troubleshooting](#troubleshooting)
18. [References](#references)

---

## Overview

`helm-charts/` is the single deployment subsystem for DataSpoke — both production
and development. It comprises:

- `helm-charts/dataspoke/` — umbrella Helm chart packaging frontend, API,
  event-consumer, PostgreSQL, Redis, and Airflow.
- `helm-charts/langfuse/` — sibling chart for the self-hosted Langfuse LLM
  observability subsystem (own namespace, bundled Postgres/Redis/ClickHouse/MinIO).
- `helm-charts/bin/` — install/uninstall/build/health scripts that orchestrate the
  charts plus dev-only peripherals.
- `helm-charts/peripherals/` — values files and plain-K8s manifests for the
  dev-only peripheral components (nginx-ingress, DataHub, dummy data, dev-lock).

The same umbrella chart serves both profiles. The **profile** (`dev` or `prod`)
selects the values overlay and the surrounding component set; the chart itself
is profile-agnostic.

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
├── .env.example                         # dev canonical env-var listing (3 sections)
├── .env.prod.example                    # prod operator template (deployment shape only)
├── bin/
│   ├── install.sh                       # main installer
│   ├── uninstall.sh                     # main uninstaller
│   ├── health-check.sh                  # service-by-service probe
│   ├── build-image.sh                   # api | airflow | postgres | frontend (Cloud Build / ECR / local)
│   ├── port-forward.sh                  # forward TCP services to 127.0.0.1 (shared ingress mode)
│   ├── lib/helpers.sh                   # logging + kubectl/helm wrappers + ingress-mode helpers
│   ├── peripherals/                     # dev-only orchestrators
│   │   ├── nginx-ingress.sh
│   │   ├── datahub.sh
│   │   ├── langfuse.sh
│   │   ├── dummy-data.sh
│   │   └── dev-lock.sh
│   └── post-install/                    # dev-only admin-API seeding
│       ├── seed-peripheral-config.sh
│       └── seed-runtime-config.sh
├── dataspoke/                           # umbrella Helm chart
│   ├── Chart.yaml
│   ├── values.yaml                      # prod defaults
│   ├── values-dev.yaml                  # dev overlay
│   ├── templates/                       # api-deployment/service/ingress, configmap, secrets, RBAC, networkpolicy
│   ├── subcharts/{frontend,event-consumer}/
│   └── charts/                          # bitnami pg/redis, apache-airflow (resolved deps)
├── langfuse/                            # sibling chart for Langfuse subsystem
└── peripherals/                         # dev-only values + manifests
    ├── nginx-ingress/values-dev.yaml
    ├── datahub/
    │   ├── values.yaml
    │   ├── prerequisites-values.yaml
    │   ├── gms-ingress.yaml
    │   └── kafka-external-svc.yaml
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
| 2 | **Parallel bootstrap** | `build-image.sh api` ‖ `build-image.sh airflow` ‖ `build-image.sh postgres` ‖ `peripherals/datahub.sh` ‖ `peripherals/langfuse.sh` | bash `&` + `wait`. Failures of any branch abort the install. `build-image.sh frontend` is added only when `--frontend cluster`. |
| 3 | Umbrella chart | `helm upgrade --install dataspoke ./helm-charts/dataspoke -f values-dev.yaml` | Depends on phase 2: images pulled by deployment, DataHub URL/PAT/Kafka + Langfuse host/public-key fed via `--set` for downstream seeding. `frontend.enabled` is `false` unless `--frontend cluster`, which appends the frontend `--set` flags and waits for the `dataspoke-frontend` rollout. |
| 4 | **Parallel post-bootstrap** | `peripherals/dummy-data.sh` ‖ `peripherals/dev-lock.sh` | Both depend on cluster connectivity but not on each other. |
| 5 | Post-install seeding | `seed-peripheral-config.sh`, `seed-runtime-config.sh`, `seed-admin-user.sh` | PATCHes `/internal/admin/peripherals/{datahub,langfuse}`, `/internal/admin/conf`, and POSTs `/internal/admin/bootstrap` (idempotent: seeds the default `dataspoke@dataspoke.local / dataspoke` Admin only when no Admin exists). Skipped by `--skip-seed`. |

### Phases — prod profile

| # | Phase | Components | Notes |
|---|---|---|---|
| 1 | Pre-flight | tool check, context switch, namespace ensure | No nginx-ingress install — operator's controller. |
| 2 | Image build | `build-image.sh api` ‖ `build-image.sh airflow` ‖ `build-image.sh postgres` | Skipped by `--skip-build` when CI built and pushed the images. `build-image.sh frontend` runs under the default `--frontend cluster`; skipped under `--frontend none`. |
| 3 | Umbrella chart | `helm upgrade --install dataspoke ./helm-charts/dataspoke -f values.yaml -f <operator-overlay>` | Operator supplies values overlay with their own ingress hosts, TLS, registry, replica counts, source-credential references. `frontend.enabled` is set from `--frontend` (`cluster`→true, `none`→false; default `cluster`). |

Peripheral wiring (DataHub URL/token, Langfuse host/keys, LLM provider/model/key)
is the operator's responsibility post-install, via `/api/v1/admin/peripherals/*`
and `/api/v1/admin/conf`. An AI scaffold may automate this for an organization
but is out of baseline scope.

### Component names

| Component | Profiles | Source |
|---|---|---|
| `nginx-ingress` | dev | `peripherals/nginx-ingress.sh` |
| `datahub` | dev | `peripherals/datahub.sh` |
| `langfuse` | dev | `peripherals/langfuse.sh` |
| `dataspoke-infra` | dev, prod | `dataspoke/` umbrella chart (alias: `chart`, `umbrella`) |
| `api` | dev, prod | umbrella chart, `api.*` block (rebuilds api image and `helm upgrade` of the API only) |
| `frontend` | dev, prod | umbrella chart, `frontend.*` block (rebuilds frontend image and `helm upgrade` of the UI only) |
| `dummy-data` | dev | `peripherals/dummy-data.sh` |
| `dev-lock` | dev | `peripherals/dev-lock.sh` |
| `seed` | dev | `post-install/*` |

`--components api` replaces the previous standalone `dataspoke-test-mode.sh` —
it rebuilds the API image, runs `helm upgrade` against the umbrella chart, and
rolls the API deployment. `--components frontend` is the analogous code-iteration
path for the UI pod.

For a full install, `--frontend` governs the UI: `none` deploys nothing; `local`
(dev-only) writes `src/frontend/.env.local` after seeding so host `pnpm dev`
reaches the in-cluster API; `cluster` deploys the containerised UI. The `local`
and `cluster` install summaries surface the Web UI URL and the default
`dataspoke@dataspoke.local / dataspoke` login.

---

## Uninstallation

`bin/uninstall.sh --profile {dev|prod} [--components frontend] [--no-question] [--delete-pvcs] [--delete-namespaces] [--delete-all]`

`--components frontend` is a targeted teardown: `helm upgrade --reuse-values
--set frontend.enabled=false` on the `dataspoke` release, leaving all other
components in place. Only `frontend` is supported (the api subchart is the core
service — stop it with `kubectl scale --replicas=0`). Without `--components`, the
full profile is torn down.

Reverse order of install. Both profiles tear down the umbrella Helm release.
The dev profile additionally removes peripherals and dev-lock. PVCs and
namespaces are preserved by default — pass `--delete-pvcs` / `--delete-namespaces`
(or `--delete-all`) to drop them. `--no-question` suppresses every interactive
prompt (gate, PVC, namespace).

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

### Component matrix

| Component | Type | Prod | Dev | Stateful |
|---|---|---|---|---|
| frontend | Deployment | ✓ | ✗ (host) | no |
| api | Deployment | ✓ | ✓ (in-cluster; stub-mode flags seeded in RuntimeConfig) | no |
| event-consumer | Deployment | ✗ (opt-in) | ✗ | no |
| postgresql | StatefulSet | ✓ | ✓ | yes (PV) |
| redis | Deployment | ✓ | ✓ | no |
| airflow (api-server + scheduler + triggerer + dag-processor) | Deployment + StatefulSets | ✓ | ✓ | no (metadata in PG) |

Each component has a `<component>.enabled` toggle.

---

## Configuration — Four-Tier Env Vars

| Tier | Prefix | Scope | Read by |
|---|---|---|---|
| App runtime | `DATASPOKE_*` (no `KUBE` / `DEV` / `TEST`) | Both profiles | DataSpoke Python/Node code via K8s ConfigMap/Secret (`envFrom`) |
| Kube deployment | `DATASPOKE_KUBE_*` | Both profiles | `bin/*.sh` install / uninstall / build scripts |
| Dev-only inputs | `DATASPOKE_DEV_*` | Dev profile only | `bin/peripherals/*.sh`, `bin/post-install/*.sh` |
| Test access | `DATASPOKE_TEST_*` | Dev profile only | `tests/integration/{conftest.py,util/*}`; auto-populated by install.sh post-install; never read by app pods |

### Tier 1 — App runtime (`DATASPOKE_*`)

Same names in dev and prod, different values. Injected into pods via ConfigMap
(non-sensitive) or Secret (sensitive) from the `dataspoke-secrets` K8s Secret per
§Configuration Flow. Not present in `helm-charts/.env`.

- `DATASPOKE_POSTGRES_{HOST,PORT,USER,PASSWORD,DB}`
- `DATASPOKE_REDIS_{HOST,PORT,PASSWORD}`
- `DATASPOKE_AIRFLOW_{URL,USER,PASSWORD,CALLBACK_BASE_URL}`
- `DATASPOKE_INTERNAL_TOKEN` — shared secret for Airflow → API internal calls
- `DATASPOKE_JWT_SECRET_KEY` — JWT HS256 signing key
- `DATASPOKE_OAUTH_STATE_SECRET` — HMAC key for the Google-OAuth state cookie
- `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` — Google OAuth client secret (paired with the public client_id; see chart-values-only callout below)

**Chart-values-only env vars (not in `.env`)** — rendered onto the API container
directly from chart values, never sourced from `.env`:

| Env var | Chart value | Role |
|---|---|---|
| `DATASPOKE_CORS_ORIGINS` | `config.corsOrigins` | Comma-separated CORS origins the API accepts (the browser UI origin). |
| `DATASPOKE_COOKIE_SECURE` | `auth.cookieSecure` | `Secure` flag on auth cookies — `true` in `values.yaml`, `false` in `values-dev.yaml` for HTTP laptop browsers. |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_ID` | `auth.googleClientId` | Google OAuth public client id; absence disables Google login. |
| `DATASPOKE_OAUTH_POST_LOGIN_REDIRECT` | `config.oauthPostLoginRedirect` | URL the Google/OIDC callback 302-redirects to after login (the frontend origin). `install.sh` sets it per `--frontend` mode (`local`→`localhost:3000`, `cluster`→`app.<domain>`); default `"/"` only works when UI and API share a host. |

Keeping these out of `.env` removes the prod footgun of a stray line silently
disabling cookie hardening. Stub-mode wiring for the four dependency factories
lives in the `runtime_config` DB row (`stub_redis_client`, `stub_llm_client`,
`stub_pgvector_manager`, `stub_notification_service`) — see
`BACKEND_LLM.md §Test Mode` and `TESTING.md §Stub Toggles`.

> DataHub, Langfuse, and LLM provider/model/key are **not** app-runtime env
> vars. They live in the DB `peripheral_config` and `runtime_config` tables,
> updated via `/api/v1/admin/peripherals/{datahub,langfuse}` and
> `/api/v1/admin/conf`. The LLM API key is read at runtime from the
> `dataspoke-llm-secret` K8s Secret via the API's RBAC. See §Secrets
> Management and `BACKEND_LLM.md §LLM API key`.

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
- `DATASPOKE_KUBE_INGRESS_CLASS` — IngressClass name DataSpoke's Ingress
  resources reference (default `nginx`). Consulted only in shared mode, where
  the install verifies this class exists; in managed mode the installed
  controller registers the fixed `nginx` class from `values-dev.yaml`.
- `DATASPOKE_KUBE_INGRESS_IP` — managed: populated by the nginx-ingress
  install from the LoadBalancer external IP; shared: blank (no owned
  LoadBalancer); prod: operator-supplied as needed.
- `DATASPOKE_KUBE_INGRESS_DOMAIN` — managed: derived `<IP>.nip.io`; shared:
  operator-pre-set to a real cluster-published hostname (e.g.
  `dataspoke-dev.your-host.com`, DNS published by the cluster's external-dns);
  prod: operator-supplied.

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
  for DataHub-side OIDC (see §DataHub above) and seeded into
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
fields are these fixed passthrough ports, carried statically in `.env.example`;
only the host-bearing values (`_HOST`, `_HOST_PORT`, `_KAFKA_BROKERS`,
`DATASPOKE_LOCK_URL`) are written by `install.sh`.

- DataSpoke subsystem: `DATASPOKE_TEST_POSTGRES_{HOST,PORT,USER,PASSWORD,DB}`,
  `DATASPOKE_TEST_REDIS_{HOST,PORT,PASSWORD}`,
  `DATASPOKE_TEST_AIRFLOW_{URL,USER,PASSWORD}`,
  `DATASPOKE_TEST_INTERNAL_TOKEN`,
  `DATASPOKE_TEST_JWT_SECRET_KEY` (conftest promotes it to `DATASPOKE_JWT_SECRET_KEY`
  so locally-minted JWTs verify against the API pod)
- DataHub access: `DATASPOKE_TEST_DATAHUB_{GMS_URL,TOKEN,KAFKA_BROKERS}`
- Langfuse access: `DATASPOKE_TEST_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}`
- Dev-lock access: `DATASPOKE_LOCK_URL` — full base URL of the dev-env lock
  service (`http://<host>:9221`, host per the laptop-side rule above). The
  integration and E2E lock protocol uses `$DATASPOKE_LOCK_URL/lock/...`.
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

## The .env File

| Path | Tracked | Purpose |
|---|---|---|
| `helm-charts/.env` | gitignored | Per-developer / per-cluster values |
| `helm-charts/.env.example` | tracked | Dev canonical listing (three sections) |
| `helm-charts/.env.prod.example` | tracked | Prod operator template (deployment shape only) |

Dev layout: three top-level sections — Kube deployment operator inputs, Dev
profile operator inputs, Auto-populated block (ingress + `DATASPOKE_TEST_*`).
`bin/*.sh` scripts source it; `tests/integration/conftest.py` loads it for
integration tests.

Prod `.env` contains only the five `DATASPOKE_KUBE_*` deployment-shape vars.
All credentials are managed via a pre-created K8s Secret (`secrets.existingSecret`
in the values overlay) — no credentials in `.env` for prod operators.

In dev, the **auto-populated** block is written by install scripts, not edited by
hand: in managed mode `DATASPOKE_KUBE_INGRESS_{IP,DOMAIN}` (by
`peripherals/nginx-ingress.sh`; shared mode leaves `INGRESS_IP` blank and reads
the operator-pre-set `INGRESS_DOMAIN`), `DATASPOKE_TEST_DATAHUB_*` (by
`peripherals/datahub.sh`), `DATASPOKE_TEST_LANGFUSE_*` (by
`peripherals/langfuse.sh`), and the full `DATASPOKE_TEST_*` subsystem block —
including `DATASPOKE_LOCK_URL` and
`DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT` — by `install.sh`
post-install (`_sync_env_from_secret` extracts credentials from the
`dataspoke-secrets` K8s Secret; the host-bearing vars take the laptop-side
TCP host for the active ingress mode).

---

## Configuration Flow

```
.env  →  bin/install.sh (dev)
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
              │  ConfigMap (dataspoke-config) + Secret (dataspoke-secrets)
              │       │
              │       ▼
              │  Deployment envFrom → container env vars (DATASPOKE_* names)
              │
              ├─ _sync_env_from_secret
              │    Extract dataspoke-secrets values → append DATASPOKE_TEST_* block to .env
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

1. Pre-create the `dataspoke-secrets` K8s Secret with all required keys (see §Secret keys
   below). Any secrets manager (ExternalSecrets Operator, Vault Agent, SealedSecrets) or a
   plain `kubectl create secret generic` works.
2. Write a values overlay with `secrets.existingSecret: <name>` pointing at that Secret.
3. Run `./helm-charts/bin/install.sh --profile prod --values <overlay.yaml>`. The install
   fails fast with a clear message if the named Secret is missing from the cluster.

### ConfigMap keys (non-sensitive)

`DATASPOKE_POSTGRES_{HOST,PORT,DB}`,
`DATASPOKE_REDIS_{HOST,PORT}`, `DATASPOKE_AIRFLOW_{URL,CALLBACK_BASE_URL}`,
plus `DATASPOKE_CORS_ORIGINS` (sourced from chart values, not `.env`).
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

### Container env rendered from chart values (not `.env`)

- `DATASPOKE_CORS_ORIGINS` (from `config.corsOrigins`)
- `DATASPOKE_COOKIE_SECURE` (from `auth.cookieSecure`)
- `DATASPOKE_GOOGLE_OAUTH_CLIENT_ID` (from `auth.googleClientId`)
- `DATASPOKE_OAUTH_POST_LOGIN_REDIRECT` (from `config.oauthPostLoginRedirect`)

### DB-backed (no env var)

- `peripheral_config` table — non-secret connection fields for DataHub
  (`gms_url`, `kafka_brokers`), Langfuse (`host`, `public_key`), and SMTP
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
PostgreSQL image layers `pgvector` + Apache AGE on the Bitnami PG 17 runtime.

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

Each `bin/peripherals/*.sh` is an idempotent installer for one peripheral.
Run independently or as part of a full `--profile dev` install.

### nginx-ingress

`peripherals/nginx-ingress.sh` behaves per `DATASPOKE_KUBE_INGRESS_MODE`
(see §Ingress for the full mode contract).

**Managed mode** (default — GKE Autopilot / minikube):

| Aspect | Value |
|---|---|
| Namespace | `ingress-nginx` |
| Source | `peripherals/nginx-ingress/values-dev.yaml` (helm release) |
| Function | Installs and owns a single LoadBalancer for all dev namespaces — HTTP virtual hosts (port 80) + TCP passthrough (PG 9201, Redis 9202, DataHub Kafka 9005, example PG 9102, example Kafka 9104, lock 9221) |
| Writes to .env | `DATASPOKE_KUBE_INGRESS_IP`, `DATASPOKE_KUBE_INGRESS_DOMAIN`, plus the IP-derived `DATASPOKE_TEST_*` host/broker vars |

**Shared mode** (AWS/EKS, or any cluster with a pre-existing controller): the
script installs nothing. It verifies the `DATASPOKE_KUBE_INGRESS_CLASS`
IngressClass (default `nginx`) exists and that the operator has pre-set
`DATASPOKE_KUBE_INGRESS_DOMAIN`, then early-exits — leaving the controller
untouched (`uninstall.sh` likewise leaves it alone). The shared controller
serves HTTP virtual hosts only; the TCP datastores are not published on it and
are reached on `127.0.0.1` via `bin/port-forward.sh`. No `INGRESS_IP` is
written (it stays blank).

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

Service name prefixes: `datahub-prerequisites-*` for the prerequisites
release (MySQL, Kafka controller); `opensearch-cluster-master` for the
OpenSearch subchart's own release.

Writes to .env: `DATASPOKE_TEST_DATAHUB_GMS_URL`, `DATASPOKE_TEST_DATAHUB_TOKEN`
(generated PAT), `DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS`.

**Google OIDC SSO**: `helm-charts/peripherals/datahub.sh` configures DataHub's
frontend to authenticate users via the same Google OAuth client as DataSpoke,
so a user logging into DataHub natively resolves to the same corpuser URN
(`urn:li:corpuser:<email>`) that DataSpoke wrote. The peripheral install
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

Sibling Helm chart `helm-charts/langfuse/` installed in `langfuse-01`
(default) with bundled Postgres, Redis, ClickHouse, MinIO subcharts. The
`langfuse.sh` script auto-generates the NextAuth/salt/encryption/ClickHouse/
MinIO/Postgres/Redis secrets on first run, creates the `dataspoke` project +
public/secret API keys, and writes them back to `.env`.

Writes to .env: `DATASPOKE_TEST_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}` plus
the Langfuse internals (`DATASPOKE_DEV_LANGFUSE_*`) on first install.

### Dummy data

Plain Kubernetes manifests under `peripherals/dummy-data/manifests/` in the
`dataspoke-dummy-data-01` namespace.

| Component | Image | Mem Limit | Storage | Service |
|---|---|---|---|---|
| PostgreSQL | `postgres:15` | 512 Mi | 5 Gi PVC | `example-postgres:5432` |
| Kafka | `apache/kafka:3.9.0` (KRaft) | 512 Mi | 4 Gi PVC | `example-kafka:9092` (internal), `:9094` (EXTERNAL) |

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

Dev only. Runs after the umbrella chart's API deployment is Ready.

| Script | Effect |
|---|---|
| `bin/post-install/seed-peripheral-config.sh` | PATCH `/internal/admin/peripherals/datahub` with `{gms_url, kafka_brokers}` and `/internal/admin/peripherals/langfuse` with `{host, public_key}`. The token / secret_key fields are populated into K8s Secrets out-of-band by the install script (so the API reads them via RBAC); only non-secret fields go through the admin API. |
| `bin/post-install/seed-runtime-config.sh` | PATCH `/internal/admin/conf` with `{llm_provider, llm_model}` from `DATASPOKE_DEV_LLM_{PROVIDER,MODEL}`. |
| `bin/post-install/seed-admin-user.sh` | POST `/internal/admin/bootstrap` to idempotently seed the built-in `dataspoke@dataspoke.local / dataspoke` Admin user (returns `{created: false}` when any Admin already exists). Tolerates `503 DATAHUB_SYNC_FAILED` retries while DataHub finishes indexing corpuser/corpGroup aspects on a fresh install. See [feature/AUTH.md §Built-in Bootstrap Admin](AUTH.md#built-in-bootstrap-admin). |

Auth: both use the `DATASPOKE_INTERNAL_TOKEN` read from the `dataspoke-secrets` Secret
(mounted on the API pod via `envFrom`).

Skip with `--skip-seed`; useful when a previous install already seeded
peripheral config and the operator wants to preserve their PATCHes.

In prod, an operator (or an organization-specific AI scaffold) performs the
equivalent through `/api/v1/admin/peripherals/*` and `/api/v1/admin/conf` —
out of project scope.

---

## Resource Sizing

### Production defaults

| Component | Replicas | CPU Req / Limit | Mem Req / Limit | PV |
|---|---|---|---|---|
| frontend | 2 | 250m / 500m | 256 Mi / 512 Mi | — |
| api | 2 | 500m / 1000m | 512 Mi / 1024 Mi | — |
| event-consumer† | 1 | 250m / 500m | 512 Mi / 1024 Mi | — |
| postgresql | 1 | 1000m / 2000m | 2048 Mi / 6144 Mi | 50 Gi (custom image with `pgvector` + Apache AGE) |
| redis | 1 + 1 | 250m / 500m | 256 Mi / 512 Mi | — |
| airflow (api-server + scheduler + triggerer + dag-processor) | 1+1+1+1 | 250m / 500m each | 512 Mi / 1024 Mi each | DAGs baked into custom image |
| **Total** (excludes event-consumer) | | **~5000m / ~10000m** | **~9.5 Gi / ~22 Gi** | **50 Gi** |

† event-consumer disabled by default — add ~250m / 500m CPU + ~512 Mi / 1024 Mi
memory when enabled.

### Dev minimums

Cluster capacity: **8 CPU / 24 GB RAM / 150 GB storage**. Sum of memory
*limits* ≈ 25 GiB (above 24 GB); sum of *requests* ≈ 13 GiB. Pods rarely hit
limits simultaneously, so limits are generous to absorb transient spikes
(OpenSearch off-heap, `mysql_upgrade`, JVM GC).

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

### Ephemeral storage budget (Autopilot)

GKE Autopilot applies a **1 GiB ephemeral-storage request = 1 GiB limit** per
container whenever the spec omits ephemeral-storage. The webhook **forces
`requests == limits`** at admission — Helm values with a higher `limits.ephemeral-storage`
than `requests.ephemeral-storage` are silently normalized to the request value.

Chatty containers (sustained stdout, emptyDir writes including Airflow's
default `/opt/airflow/logs` emptyDir, projected-volume mounts) exhaust the
default within minutes and trigger eviction with `Pod ephemeral local storage
usage exceeds the total limit of containers`. Explicit ephemeral-storage
limits in the table below prevent that class of eviction. Low-log containers
(dev-lock, redis, frontend, Airflow logGroomer sidecars) keep the Autopilot
default.

| Component | Namespace | Limit | Why |
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
| airflow-scheduler | dataspoke-01 | 8 Gi | High-log: heartbeat + task scheduling |
| airflow-triggerer | dataspoke-01 | 8 Gi | High-log: event-loop |
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
| HTTP virtual hosts | ✓ (app/api/datahub/airflow/langfuse) | ✓ (same hosts, on the operator's controller) |
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

| Resource | Location | Routes |
|---|---|---|
| `templates/api-ingress.yaml` | umbrella chart | `api.<INGRESS_IP>.nip.io/` → `dataspoke-api:8002` |
| `subcharts/frontend/templates/ingress.yaml` | frontend subchart | `app.<INGRESS_IP>.nip.io/` → `dataspoke-frontend:3000` |
| `airflow.ingress` values | airflow chart (native) | `airflow.<INGRESS_IP>.nip.io/` → `dataspoke-airflow-api-server:8080` |
| `peripherals/datahub/gms-ingress.yaml` | kubectl manifest | `datahub.<INGRESS_IP>.nip.io/gms` → `datahub-datahub-gms:8080` |
| `datahub-frontend.ingress` values | DataHub chart (native) | `datahub.<INGRESS_IP>.nip.io/` → `datahub-frontend:9002` |

In **managed** mode, TCP passthrough (PostgreSQL :9201, Redis :9202, DataHub
Kafka :9005, example PG :9102, example Kafka :9104, lock :9221) is handled by
the nginx-ingress `tcp-services` ConfigMap (the `tcp` block in
`peripherals/nginx-ingress/values-dev.yaml`) — no Ingress resource needed.
Kafka services advertise `<INGRESS_IP>:<port>` as their EXTERNAL listener so
host-side producers/consumers reach them through the controller.

In **shared** mode the operator's controller serves only HTTP, so the TCP
datastores are not on the ingress. `bin/port-forward.sh` runs in the
foreground and `kubectl port-forward`s the same six services to their canonical
ports on `127.0.0.1`; Kafka EXTERNAL listeners advertise `127.0.0.1:<port>`.
Integration tests, `health-check.sh`, and `helm-charts/.env`'s
`DATASPOKE_TEST_*` host values all resolve to `127.0.0.1` while the
port-forward holds.

### Network Policy

A NetworkPolicy template allows egress from DataSpoke pods to the DataHub
namespace (GMS :8080, Kafka :9092). Controlled by `networkPolicy.enabled`
(default `false`) and `networkPolicy.datahubNamespace`. Enable in clusters
with default-deny.

---

## Secrets Management

| Family | Owner | Purpose |
|---|---|---|
| **`dataspoke-secrets`** | `install.sh` (dev auto-generate) or operator (prod pre-create) | DataSpoke's own runtime credentials — Postgres user/password/db, Redis password, Airflow user/password/webserver-secret/jwt-secret, internal-auth token, JWT signing key. Ten keys; mounted `envFrom` on the API Deployment and alembic-migrate init container. |
| **`dataspoke-airflow-metadata-db`** | `install.sh` `_derive_airflow_metadata_secret` (both profiles) | Single key `connection` = full PostgreSQL URI for Airflow's metadata DB. Wired via `airflow.data.metadataSecretName`. |
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
`ephemeral-storage` entries per §Resource Sizing §Ephemeral storage. If the
evicted container is not in the table, verify it is not writing unexpectedly
large logs.

### OpenSearch OOM-killed during startup

Off-heap usage (Lucene cache, index recovery) spikes above the JVM heap.
Already mitigated — `peripherals/datahub/prerequisites-values.yaml` sets the
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
- [External Secrets Operator](https://external-secrets.io/)
- [ARCHITECTURE.md](../ARCHITECTURE.md) — system architecture, env-var convention
- [TESTING.md](../TESTING.md) — testing conventions, dev-env lock protocol
- [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md) — source-credential Secret model
- [BACKEND_LLM.md](BACKEND_LLM.md) — LLM observability + online key rotation
