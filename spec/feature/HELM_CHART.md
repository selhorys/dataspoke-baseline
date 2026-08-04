# HELM_CHART — DataSpoke Deployment Subsystem

## Table of Contents

1. [Overview](#overview)
2. [Repository Layout](#repository-layout)
3. [Profiles](#profiles)
4. [Installation](#installation)
5. [Uninstallation](#uninstallation)
6. [Umbrella Chart Structure](#umbrella-chart-structure)
7. [Configuration — Five-Tier Env Vars](#configuration--five-tier-env-vars)
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
├── .env.prod.example                    # prod operator template (full operator input set)
├── .env.prod.<name>-no-credential.example  # optional per-deployment copy source (credential-free)
├── values-prod.example.yaml             # prod values-overlay template (--values starting point)
├── bin/
│   ├── install.sh                       # main installer
│   ├── install-prod-preflight.sh        # prod: validate the three config planes + populate
│   │                                    #   credentials; mutates no Helm release
│   ├── uninstall.sh                     # main uninstaller
│   ├── health-check.sh                  # service-by-service probe (--profile {dev|prod})
│   ├── build-image.sh                   # api | airflow | postgres | frontend (Cloud Build / ECR / local)
│   ├── port-forward.sh                  # forward TCP services to 127.0.0.1 (shared ingress mode)
│   ├── lib/helpers.sh                   # logging + kubectl/helm wrappers + ingress-mode helpers + in-pod admin-API caller
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
├── prod-prereq/                         # cluster-scoped prerequisites a cluster-admin
│                                        #   applies before the release (StorageClass)
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
| Post-install peripheral seeding | ✓ (part of the install) | ✗ from the install; the operator runs the same scripts against `.env.prod`, or the admin API / UI |
| Post-install admin-user seed | ✓ | ✓ (both skippable with `--skip-seed`) |
| Airflow credential check at login | ✗ (`simple_auth_manager_all_admins: "True"`) | ✓ (`"False"` + a seeded passwords file; §Airflow authentication) |
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
| `--components <list>` | all-for-profile | **Dev only.** Comma-separated subset (e.g. `api`, `dataspoke-infra`, `datahub`). |
| `--from-component <name>` | — | **Dev only.** Resume an interrupted full install at this component. |
| `--skip-build` | false | Skip Docker image rebuild (api/airflow/postgres). |
| `--skip-seed` | false (dev) | Skip post-install admin-API seeding. |
| `--values <path>` | — | Extra values file passed to the umbrella chart (prod). **Single use** — a repeated `--values` is a hard error, so an operator layering several overlays merges them into one file first. |
| `--image-tag <tag>` | `dev` | Override the image tag for api/airflow/postgres (prod CI). Validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` before use — it flows into several `helm --set`/`--set-string` tokens, where an unvalidated comma or newline could inject an arbitrary values path. |
| `--no-digest-pin` | false | Skip image-digest resolution entirely (§Digest stamping) for the three DataSpoke-owned workloads (`dataspoke-api`, `dataspoke-event-consumer`, `dataspoke-frontend`): each renders the mutable `<repository>:<tag>` reference with `imagePullPolicy: Always` instead, and `install.sh` unconditionally issues an explicit rollout restart of all three (`dataspoke-frontend` only when deployed) after the umbrella upgrade. `postgresql` and `airflow` are never digest-stamped regardless of this flag — see §Digest stamping's "airflow and postgres are not digest-stamped" note. |
| `--frontend {none\|local\|cluster}` | `none` (dev), `cluster` (prod) | Frontend deployment mode for a full install. `none`: not deployed. `local` (dev-only): writes `src/frontend/.env.local` pointing at the in-cluster API for host `pnpm dev`. `cluster`: builds the image and deploys the UI in-cluster. |
| `--help`, `-h` | — | Print usage. |

`--profile` is the common selector across the `bin/` entry points an operator
invokes directly — `install-prod-preflight.sh`, `install.sh`, `uninstall.sh`,
and `health-check.sh` — each resolving `helm-charts/.env.<profile>` by the same
rule, with `--env-file` overriding it. `build-image.sh` and `port-forward.sh`
are driven by the `ENV_FILE` the caller exports rather than by a profile of
their own.

In prod the installer is the second half of a two-command sequence:
`bin/install-prod-preflight.sh` validates and populates, `install.sh` mutates
the release (§Prod operator workflow).

### Phases — dev profile

| # | Phase | Components | Notes |
|---|---|---|---|
| 1 | Pre-flight | tool check, context switch, namespace ensure, nginx-ingress install | nginx-ingress must complete first to provide `INGRESS_IP` / `_DOMAIN` for downstream. |
| 2 | **Parallel bootstrap** | `build-image.sh api` ‖ `build-image.sh airflow` ‖ `build-image.sh postgres` ‖ `dev-peripherals/datahub.sh` ‖ `dev-peripherals/langfuse.sh` | bash `&` + `wait`. Failures of any branch abort the install. `build-image.sh frontend` is added only when `--frontend cluster`. |
| 3 | Umbrella chart | `helm upgrade --install dataspoke ./helm-charts/dataspoke -f values-dev.yaml` | Depends on phase 2: images pulled by deployment, their resolved digests stamped into pod annotations (§Digest stamping), DataHub URL/PAT/Kafka + Langfuse host/public-key fed via `--set` for downstream seeding. `frontend.enabled` is `false` unless `--frontend cluster`, which appends the frontend `--set` flags and waits for the `dataspoke-frontend` rollout. |
| 4 | **Parallel post-bootstrap** | `dev-peripherals/dummy-data.sh` ‖ `dev-peripherals/dev-lock.sh` | Both depend on cluster connectivity but not on each other. |
| 5 | Post-install seeding | `seed-peripheral-config.sh`, `seed-runtime-config.sh`, `seed-admin-user.sh` | From inside the API pod, PATCHes `/internal/admin/peripherals/{datahub,langfuse}`, `/internal/admin/conf`, and POSTs `/internal/admin/bootstrap` (idempotent: seeds the default `dataspoke@dataspoke.local / dataspoke` Admin only when no Admin exists). Skipped by `--skip-seed`. |

### Phases — prod profile

| # | Phase | Components | Notes |
|---|---|---|---|
| 1 | Pre-flight | tool check, context switch, namespace ensure, IngressClass/StorageClass checks, then — only under `--skip-build` — image-digest resolution, then Secret checks | No nginx-ingress install — operator's controller. Digest resolution, when it runs here, lands ahead of the derived Airflow key Secrets this phase writes, only when `--skip-build` means the image already exists in the registry; otherwise it waits for phase 2's push (see §Digest stamping). The operator-owned credentials Secret is not among them — prod verifies it and never writes it (§The pre-flight). |
| 2 | Image build | `build-image.sh api` ‖ `build-image.sh airflow` ‖ `build-image.sh postgres` | Skipped by `--skip-build` when CI built and pushed the images. `build-image.sh frontend` runs under the default `--frontend cluster`; skipped under `--frontend none`. |
| 3 | Umbrella chart | `helm upgrade --install dataspoke ./helm-charts/dataspoke -f values.yaml -f <operator-overlay>` | Operator supplies values overlay with their own ingress hosts, TLS, registry, replica counts, source-credential references. Digest stamping applies in prod as well (resolved here instead of phase 1 unless `--skip-build` was passed), so the same tag name carrying new content still rolls api/frontend, and event-consumer too when the overlay enables it. `frontend.enabled` is set from `--frontend` (`cluster`→true, `none`→false; default `cluster`). |
| — | Admin seed | `post-install/seed-admin-user.sh` | Runs after the chart phase unless `--skip-seed` is passed, calling the API from inside its own pod rather than through the ingress. Idempotent; seeds the default `dataspoke@dataspoke.local / dataspoke` Admin only when no Admin exists. Carries no `step` marker of its own. |

**Post-upgrade blocking waits (phase 3).** Immediately after the `helm
upgrade` above, `install.sh` runs `kubectl rollout status --timeout=5m`
against each workload the release was actually asked to render, aborting the
whole install on a timeout:

| Workload | Waited on | Conditional on |
|---|---|---|
| `dataspoke-api` | always | `api.enabled`, which the installer pins to `true` on the upgrade so an overlay cannot switch it off underneath the wait |
| `dataspoke-event-consumer` | only if the Deployment object exists post-upgrade | `event-consumer.enabled` (chart default `false`; ships commented out in `values-prod.example.yaml` — an operator overlay is the only way to turn it on). Checked by existence, not by re-parsing the overlay, so it tracks whatever the release actually rendered regardless of how `enabled` got set. |
| `dataspoke-frontend` | only when the frontend is deployed | `--frontend cluster` (`_prod_frontend_enabled`) |
| `dataspoke-airflow-api-server` | always | `airflow.enabled`. This wait is what makes a broken passwords-file materialisation an install failure rather than a success followed by an api-server that never serves a login (§Airflow authentication). |

The API wait needs no existence check because the installer `--set`s
`api.enabled=true` on the upgrade, the same script-wins pin it uses for
`frontend.enabled`. The api Deployment template is gated on that value, so
without the pin an operator overlay could switch it off and the wait would
abort against a Deployment the release never rendered. The event-consumer wait's existence check is what keeps the
default install (`event-consumer.enabled=false`) from aborting on a `rollout
status` against an object the chart never created. Under `--no-digest-pin`,
each in-scope workload is also explicitly `kubectl rollout restart`ed ahead
of its wait (see §Digest stamping).

`_restart_airflow_key_consumers` (see §Rotation tolerance of the Airflow
projections) runs immediately after the `helm upgrade` above and strictly
before all three
`kubectl rollout status` waits — not after them. Phase 3 has already
re-projected a rotated signing key out of the credentials Secret into
`dataspoke-airflow-api-secret-key` / `dataspoke-airflow-jwt-secret` ahead of
this point (the credentials Secret itself is operator-owned in prod and never
written by the installer); placing
the restart after the waits would let a 5-minute wait timeout abort the
install with the rotated key applied but the consuming Airflow pods never
restarted to pick it up, and the next run's own comparison would then find
the Secret and the live projection already in agreement and skip the restart
silently. Every call site of the helper orders it this way.

**Two independent rotations reach that restart, at the same point in the
sequence.** A rewritten `dataspoke-airflow-metadata-db` connection URI
(§Secrets Management) triggers it on the same rule as a rotated signing key,
and for the same reason: the Secret is written earlier in the run — before the
`helm upgrade`, in both profiles — so the restart is the only thing that makes
the new value live. The silent-skip hazard is identical as well. An abort
between the rewrite and the restart leaves the four consumers on the superseded
DSN, and the next run's comparison finds the Secret already carrying the
derived value and skips the restart.

The `helm upgrade` itself still sits between the Secret write and the restart on
every path, so an upgrade failure strands a rotated key or DSN the same way.
Removing that residual means restarting the consumers directly after the write,
ahead of the upgrade.

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
| `dataspoke-infra` | dev | `dataspoke/` umbrella chart (alias: `chart`, `umbrella`) |
| `api` | dev | umbrella chart, `api.*` block (rebuilds api image and `helm upgrade` of the API only) |
| `frontend` | dev | umbrella chart, `frontend.*` block (rebuilds frontend image and `helm upgrade` of the UI only) |
| `dummy-data` | dev | `dev-peripherals/dummy-data.sh` |
| `dev-lock` | dev | `dev-peripherals/dev-lock.sh` |
| `seed` | dev | `post-install/*` |

`--components` and `--from-component` are honoured by the dev profile only; a
prod install always runs its full phase sequence. `--components api` rebuilds
the API image and runs `helm upgrade` against the umbrella chart, which rolls
the API deployment through digest stamping (§Digest stamping). `--components
frontend` is the analogous code-iteration path for the UI pod.

For a full install, `--frontend` governs the UI: `none` deploys nothing; `local`
(dev-only) writes `src/frontend/.env.local` after seeding so host `pnpm dev`
reaches the in-cluster API; `cluster` deploys the containerised UI. The `local`
and `cluster` install summaries surface the Web UI URL together with the login
for the seeded `dataspoke@dataspoke.local` Admin. The published `dataspoke`
password is printed only while it is still the live one — dev, and prod with
`DATASPOKE_PROD_ADMIN_PASSWORD` blank. Once the admin seed has rotated the
account to that input (§Prod operator workflow) the summary names the address
and points at the operator's own password instead, so no summary ever presents a
superseded credential as the way in.

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
| `dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret` | Out-of-band, not Helm-managed (see §Secrets Management) |

Three PVCs, **66 Gi** total, keep consuming storage after teardown. The uninstall
output names four Secrets as deleted — `dataspoke-airflow-metadata-db`,
`dataspoke-airflow-api-secret-key`, `dataspoke-airflow-jwt-secret`,
`dataspoke-airflow-metadata-encryption-key` — and separately logs the
credentials Secret as retained. All four derive from that retained Secret —
three are single-key projections of it, and `dataspoke-airflow-metadata-db`
composes its Postgres password into a connection URI whose every other part is
fixed — so deleting them is safe: the next install rebuilds them
byte-identically from it. The out-of-band Secrets are listed
under the retained-resources summary when present, and the prod profile never
deletes them.

**The legacy Airflow hook Secret is deleted only when it is provably
redundant.** `dataspoke-airflow-fernet-key` is the Airflow subchart's own
pre-install-hook Secret, carrying `hook-delete-policy: before-hook-creation`, so
`helm uninstall` never removes it; it exists only on a cluster whose release
predates the pinning of `airflow.fernetKeySecretName`. On such a cluster it can
be the sole live carrier of the key that decrypts the retained metadata DB, so
both profiles compare its `fernet-key` against `DATASPOKE_AIRFLOW_FERNET_KEY` in
the credentials Secret and delete it only on an exact match. A disagreement — or
a credentials Secret that carries no Fernet key at all — leaves it in place with
a warning naming the manual `kubectl delete` and the condition under which it is
safe.

**Fernet key ↔ Postgres PVC coupling.** Airflow encrypts connection secrets and
Variables in its metadata DB with the Fernet key it reads from
`dataspoke-airflow-metadata-encryption-key`, projected from
`DATASPOKE_AIRFLOW_FERNET_KEY` in the credentials Secret. Because that metadata
lives in the retained Postgres PVC, the credentials Secret and the PVC must be
kept or dropped together — a Fernet key that changes while the PVC survives
leaves every stored connection and Variable permanently undecryptable, and
Airflow reports it at decrypt time rather than at install time. In prod, teardown
deletes only the projection and never the credentials Secret that sources it, so
the pair stays aligned across an uninstall/reinstall cycle; a source key that
disagrees with the live projection aborts the install rather than re-projecting,
per [§Rotation tolerance of the Airflow
projections](#rotation-tolerance-of-the-airflow-projections). The alignment is
the operator's to maintain in the one case the script cannot decide — deleting
the credentials Secret by hand, which must happen together with the PVCs or not
at all.

**A populated `.env.prod` makes a deleted credentials Secret recoverable.** Once
the env file holds all eleven `DATASPOKE_PROD_*` credential inputs (§Tier 5 —
Prod-only inputs), `bin/install-prod-preflight.sh` rebuilds the Secret
byte-identically, Fernet key included, so the Secret is not the only surviving
copy of what the retained PVCs depend on. This **relocates** the risk
rather than removing it: the env file becomes the thing to protect, and the
whole-Secret ↔ PVC coupling above reapplies in full if the env file is lost or
if its Fernet line was never populated. The check that belongs before any
teardown is therefore on the env file, not the cluster — confirm all eleven keys
are set, **by name with set/blank verdicts only, never values**.

**Full wipe.** `--delete-pvcs` is a dev-only flag. In prod the sanctioned full
wipe is `--delete-namespaces` (or `--delete-all`), which removes the namespace
and with it the PVCs and every Secret above — including the credentials Secret
and the out-of-band ones. Recreate the credentials Secret before the next
install; because the PVCs are gone too, a freshly minted Fernet key is correct
there rather than a hazard.

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

The event-consumer subchart builds **no image of its own** — its Deployment runs
the API image with a `command:` override, so `--image-tag` selects one artifact
for both workloads and the two can never run different revisions of `src/`. Its
`image.*` values therefore default to the API's coordinates, and it carries the
same image-digest pod annotation, so one push rolls both Deployments together. See
[BACKEND §Kafka Consumers](BACKEND.md#kafka-consumers-optional-not-enabled-in-baseline).

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

Every workload component except the Airflow statsd relay ships a
PodDisruptionBudget paired with a
`cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` pod annotation:
`templates/api-pdb.yaml`, `subcharts/{frontend,event-consumer}/templates/pdb.yaml`,
and the subchart-native keys for the dependencies (bitnami redis `master.pdb` /
`replica.pdb`, bitnami postgresql `primary.pdb`, Airflow
`podDisruptionBudget.config`). This is a deliberate availability guard against
Autopilot / cluster-autoscaler evicting a pod during scale-in. On the
Airflow scheduler, triggerer, and dag-processor `safeToEvict: false` suppresses
the chart's default `safe-to-evict="true"` annotation so `podAnnotations` can set
`"false"` without rendering a conflicting duplicate key. statsd carries neither
mechanism because it is a non-critical metrics relay whose loss costs
observability, not correctness, and holding a node out of scale-down for it is
not worth the price.

**The annotation and the PDB are two independent mechanisms with different
audiences** — not a belt-and-braces pair on one path:

| Mechanism | Honoured by | Blocks | Limit |
|---|---|---|---|
| `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` | cluster-autoscaler only | node scale-down, unconditionally — the autoscaler drops the node from its candidate set without simulating a drain, so no PDB arithmetic can be relaxed into permitting it | `kubectl drain`, node-pool upgrades, the descheduler, and every other Eviction-API caller never read it |
| PodDisruptionBudget | the Eviction API — and cluster-autoscaler, which simulates the drain against live PDBs before removing a node | `kubectl drain`, node-pool and node-repair upgrades, any other Eviction-API caller — **and** scale-down, for as long as the budget admits no disruption | it is relaxable per workload: a budget widened to let drains through stops constraining scale-down at the same moment |

Both cover the same set: frontend, api, event-consumer, the postgresql primary,
the redis master and replica, and the four Airflow components.

The two overlap on scale-down and diverge on the Eviction API, so neither
substitutes for the other:

- **Relaxing or deleting a PDB unblocks drains, not scale-down** — the annotation
  still holds the node out of the autoscaler's candidate set.
- **Removing the annotation alone unblocks neither** — for the components whose
  budget admits no disruption. At the chart's single-replica budgets
  (`maxUnavailable: 0` / `minAvailable: 1`) the PDB refuses every voluntary
  disruption, and cluster-autoscaler honours it during the drain simulation, so
  scale-down stays blocked too.
- Retiring a node therefore needs both cleared.

The annotation's distinct value is holding scale-down *independently of PDB
arithmetic*: it survives a budget an operator widens to let drains through, and
it is the only scale-down guard on the three workloads whose budget already
admits one disruption — the two-replica api and frontend at `minAvailable: 1`,
and the single-replica event-consumer at `maxUnavailable: 1` (a budget chosen so
one replica does not stall drains indefinitely, since a consumer group rebalances
in seconds).

**Every single-replica component except the event-consumer permits zero
voluntary disruption** — expressed
as `maxUnavailable: 0` in the Airflow chart and `minAvailable: 1` in the Bitnami
charts (semantically identical at one replica). This covers the Airflow
api-server, scheduler, triggerer, and dag-processor, the postgresql primary, the
redis master, the redis replica (`replica.replicaCount: 1` with
`replica.pdb.minAvailable: 1`), and the dev API (`values-dev.yaml` sets
`replicaCount: 1` while inheriting `api.podDisruptionBudget.minAvailable: 1`).
For those pods the PDB is absolute: it admits no voluntary disruption at all
rather than merely rate-limiting it, so node drains and cluster upgrades **block
until an operator intervenes** — the guard trades drain automation for uptime.

### Scheduling and spread

All three application pod templates — `templates/api-deployment.yaml`,
`subcharts/frontend/templates/deployment.yaml`, and
`subcharts/event-consumer/templates/deployment.yaml` — expose `nodeSelector`,
`tolerations`, `affinity`, and `topologySpreadConstraints`, each `{{- with }}`-guarded
so an unset key renders nothing. Operators place DataSpoke on dedicated or
tainted node pools through their values overlay without patching templates.

`api` and `frontend` run `replicaCount: 2` and ship a **default spread on two
topology keys** — `kubernetes.io/hostname` and `topology.kubernetes.io/zone`,
both `maxSkew: 1`, both `whenUnsatisfiable: ScheduleAnyway`. `event-consumer`
runs one replica and ships the four knobs with no default spread.

Two decisions behind that shape are load-bearing:

- **Both keys ship, because a pod-level constraint replaces the cluster-level
  default wholesale.** Under kube-scheduler's default
  `defaultingType: System`, `defaultConstraints` apply only to pods that declare
  *no* `topologySpreadConstraints` of their own. Shipping a hostname-only
  constraint would therefore silently *remove* whatever zone spread the pods were
  getting for free from the cluster default — a regression disguised as a
  hardening change. Shipping both keys restores it explicitly.
- **`ScheduleAnyway`, not `DoNotSchedule`.** A hard constraint makes the second
  replica flatly unschedulable on a single-node or single-AZ cluster, which is
  every dev install and many small prod clusters. Soft spread degrades to
  co-location instead of leaving a pod `Pending`, so no off switch is needed.

**Each `labelSelector` mirrors its own Deployment's selector, and the two
Deployments differ.** `api-deployment.yaml` selects on
`app.kubernetes.io/name: dataspoke-api` alone — a hardcoded literal with no
instance label — while the frontend subchart selects via `frontend.selectorLabels`,
which adds `app.kubernetes.io/instance: {{ .Release.Name }}`. A selector broader
than its Deployment's counts foreign pods into the skew calculation, so the two
defaults cannot share one shape. That forces two homes for them: the api default
lives in `values.yaml` under `api.topologySpreadConstraints` because a literal
selector is fully expressible there; the frontend default lives in the subchart
template (`{{- with .Values.topologySpreadConstraints }}…{{- else }}…{{- end }}`)
because values cannot reach `.Release.Name`. An operator overrides either by
setting their own list.

---

## Configuration — Five-Tier Env Vars

| Tier | Prefix | Scope | Read by |
|---|---|---|---|
| App runtime | `DATASPOKE_*` (no `KUBE` / `DEV` / `PROD`) | Both profiles | DataSpoke Python/Node code via K8s ConfigMap/Secret (`envFrom`) |
| Kube deployment | `DATASPOKE_KUBE_*` | Both profiles | `bin/*.sh` install / uninstall / build scripts |
| Dev-only inputs | `DATASPOKE_DEV_*` | Dev profile only, **operator-supplied** | `bin/dev-peripherals/*.sh`, `bin/post-install/*.sh` |
| Dev access | `DATASPOKE_DEV_*` | Dev profile only, **auto-populated post-install; not operator-supplied** | `tests/integration/{conftest.py,util/*}`, `bin/health-check.sh`, `bin/port-forward.sh`; never read by app pods |
| Prod-only inputs | `DATASPOKE_PROD_*` | Prod profile only | `bin/install-prod-preflight.sh`, `bin/post-install/*.sh`; the credential subset is mapped into the credentials Secret, and app pods never read the env file |

**Tiers 3 and 4 share the `DATASPOKE_DEV_*` prefix and are separated by
provenance, not by name.** The prefix axis is the profile a variable belongs to,
which makes `DATASPOKE_DEV_*` and `DATASPOKE_PROD_*` symmetric peers: the same
input in the two profiles carries the same suffix, so the two env files read side
by side. Who writes a value — the operator by hand, or `install.sh` after the
install — is a property of the variable, so it is documented per tier rather than
encoded in the name. It matters in one direction only: hand-editing a tier-4
value is pointless, because the next install overwrites it from the cluster.

### Tier 1 — App runtime (`DATASPOKE_*`)

Same names in dev and prod, different values. Injected into pods via ConfigMap
(non-sensitive) or Secret (sensitive) from the `dataspoke-secrets` K8s Secret per
§Configuration Flow. Not present in `helm-charts/.env.dev` / `helm-charts/.env.prod`.
Third-party runtimes' own env names are carried in this tier unprefixed — the
API's uvicorn server reads `FORWARDED_ALLOW_IPS` under that fixed name.

- `DATASPOKE_POSTGRES_{HOST,PORT,USER,PASSWORD,DB}`
- `DATASPOKE_REDIS_{HOST,PORT,PASSWORD}`
- `DATASPOKE_AIRFLOW_{URL,USER,PASSWORD,CALLBACK_BASE_URL}`
- `DATASPOKE_INTERNAL_TOKEN` — shared secret carried by every call into
  `/internal/*`: Airflow → API callbacks and the post-install seed scripts
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
| `DATASPOKE_OAUTH_POST_LOGIN_REDIRECT` | `config.oauthPostLoginRedirect` | URL the Google/OIDC callback 302-redirects to after login (the frontend origin). `install.sh` sets it per `--frontend` mode (`local`→`localhost:3000`, `cluster`→`app.<domain>`); default `"/"` only works when UI and API share a host. Its origin also supplies the host half of the failure redirect to `/oauth-error` on both `/auth/google/*` routes, so a wrong value breaks the sign-in error page too ([API.md §OAuth browser-redirect contract](../API.md#oauth-browser-redirect-contract)). |
| `DATASPOKE_RATE_LIMIT_PER_MINUTE` | `config.rateLimitPerMinute` | Size of the default limiter's single per-caller budget, in requests per minute (default `120`). The fail-closed auth limiter's per-route limits are fixed in code and unaffected by this value ([API.md §Middleware Stack](../API.md#middleware-stack)). |
| `FORWARDED_ALLOW_IPS` | `config.trustedProxyIps` | Source addresses whose `X-Forwarded-For` and `X-Forwarded-Proto` the API's uvicorn server honours. uvicorn's own env-var name, so it carries no `DATASPOKE_` prefix. Default `"127.0.0.1"` — loopback only, i.e. no proxy trusted. Operators opt in by naming their ingress controller's pod CIDR, e.g. `"127.0.0.1,10.4.0.0/14"`. |

**Two of these are guarded at render time, and a bad value fails the install
rather than the running API.** The ConfigMap template `fail`s when
`config.rateLimitPerMinute` is not a positive integer — `0`, a negative, or a
non-numeric string would otherwise disable or break the limiter silently — and
when `config.trustedProxyIps` is empty, `"*"`, or contains `0.0.0.0/0` / `::/0`,
each of which defeats client-IP attribution outright. The proxy guard catches
only those literal all-address forms; any other over-broad range (the full
RFC1918 space, say) renders successfully and remains the operator's judgement
call.

**Trusting a proxy is opt-in, and the trust list applies to the whole forwarded
chain.** uvicorn honours the forwarded headers only from a trusted peer, then
walks `X-Forwarded-For` right to left and takes the first address *not* in the
list as the client. Every entry is therefore a party permitted to *name* the
client address, not merely to relay it. Since the client IP is the
rate-limiter's bucket key for unauthenticated traffic
([AUTH.md §Client-IP attribution for rate
limiting](AUTH.md#client-ip-attribution-for-rate-limiting)), and that limit is
the only brute-force control on `POST /auth/token`, the list is what stands between
an attacker and unbounded credential guessing:

- **`*` is never correct.** It trusts every peer, so any caller forges a fresh
  address per request and evades the limit entirely.
- **A private-range envelope (`10.0.0.0/8` and friends) is not a safe
  default** either. It admits every in-cluster pod, every VPC-CNI node, and any
  VPN or peered-network caller that can reach the API pod — each of them free
  to choose its own bucket.
- **The shipped default trusts no proxy**, so unauthenticated traffic from
  outside the pod lands in a single bucket keyed on the ingress controller's
  pod IP. That is the documented cost of configuring nothing, not a silent
  failure.

Per-client bucketing therefore requires the operator to set the value to their
ingress controller's pod CIDR and no wider. Trusting the hop is necessary but
not sufficient: every hop in front of the API must also preserve the client
address, which is topology-dependent and outside what DataSpoke configures. The
value has a second effect — the same gate governs `X-Forwarded-Proto`, so
widening it changes the scheme of the OAuth `redirect_uri` the API generates.
Both are covered in
[AUTH.md §Client-IP attribution for rate limiting](AUTH.md#client-ip-attribution-for-rate-limiting).

Keeping these out of `.env` removes the prod footgun of a stray line silently
disabling cookie hardening. Stub-mode wiring for the four dependency factories
lives in the `runtime_config` DB row (`stub_redis_client`, `stub_llm_client`,
`stub_pgvector_manager`, `stub_notification_service`) — see
`BACKEND_LLM.md §Test Mode` and `TESTING.md §Stub Toggles`.

> DataHub, Langfuse, SMTP, and LLM provider/model/key are **not** app-runtime env
> vars. Their non-secret settings live in the DB `peripheral_config` and
> `runtime_config` tables; their secret fields live in dedicated K8s Secrets read
> at runtime via the API's RBAC. The field-by-field breakdown is in
> §[DB-backed (no env var)](#db-backed-no-env-var) — the single enumeration of the
> peripheral contract in this document. See also §Secrets Management and
> `BACKEND_LLM.md §LLM API key`.

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
- `DATASPOKE_KUBE_INGRESS_CLASS` — IngressClass name, resolved by the
  `ingress_class()` helper in `bin/lib/helpers.sh`; default `nginx` in dev,
  required explicitly in prod. In shared mode the install verifies the class
  already exists. See §Ingress.
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
  `NEXT_PUBLIC_*`, the post-login redirect, the DataHub OIDC base,
  `health-check.sh` probes, the host-bearing `DATASPOKE_DEV_*`
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
  — the install half of the one `DATASPOKE_DEV_DATAHUB_*` block whose connection
  half Tier 4 auto-populates
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
  `_DUMMY_DATA_POSTGRES_PASSWORD`, `_DUMMY_DATA_POSTGRES_DB` — the credential
  half of the one `DATASPOKE_DEV_DUMMY_DATA_*` block whose address half Tier 4
  auto-populates
- LLM seed: `_LLM_PROVIDER`, `_LLM_API_KEY`, `_LLM_MODEL` — written into the
  `dataspoke-llm-secret` Secret and PATCHed into `/admin/conf`
- Google OAuth credentials: `_GOOGLE_OAUTH_CLIENT_ID`,
  `_GOOGLE_OAUTH_CLIENT_SECRET` — passed to the DataHub peripheral install
  for DataHub-side OIDC (see §DataHub) and seeded into
  `dataspoke-secrets` (`DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET`) and the API
  chart values (`auth.googleClientId`). Absence leaves OAuth disabled on
  both DataSpoke and DataHub.
- Dev-env lock: `_LOCK_PREACQUIRED` (set by outer wrappers that already hold the
  dev-env lock), beside the `_LOCK_URL` Tier 4 auto-populates

### Tier 4 — Dev access (`DATASPOKE_DEV_*`, auto-populated)

Written by `install.sh` post-install, not by the operator — credential values
out of the `dataspoke-secrets` Secret via `_sync_env_from_secret`, the Postgres
role and database name out of the app ConfigMap that owns them (§ConfigMap
keys), and the peripheral connections out of each `dev-peripherals/*.sh` install.
Read by `tests/integration/{conftest.py,util/*}`, `bin/health-check.sh`, and
`bin/port-forward.sh` for laptop-side access to in-cluster services. App pods
never read these, and a hand-edited value does not survive the next install.

The **laptop-side host** these point at is ingress-mode-dependent: in managed
mode it is the LoadBalancer external IP (TCP passthrough on the owned
controller); in shared mode it is `127.0.0.1`, reached via
`bin/port-forward.sh` on the same canonical ports. The TCP service ports
(9201/9202/9005/9102/9104/9221) are identical in both modes, so the `_PORT`
fields are fixed: `install.sh` rewrites `_POSTGRES_PORT` / `_REDIS_PORT` each
run with the same literal, and `_DUMMY_DATA_POSTGRES_PORT` is carried statically
in `.env.dev.example` and written by nothing. What actually varies is the
host-bearing values (`_HOST`, `_HOST_PORT`, `_KAFKA_BROKERS`,
`DATASPOKE_DEV_LOCK_URL`), which `install.sh` resolves from the cluster.

Three tier-4 values are generated only when blank and otherwise honoured:
`_DATAHUB_TOKEN` (`dev-peripherals/datahub.sh` validates an existing PAT against
GMS and regenerates only when absent or stale) and
`_LANGFUSE_{PUBLIC,SECRET}_KEY`. `DATASPOKE_KUBE_INGRESS_DOMAIN` is likewise
operator-set in shared ingress mode, where no LoadBalancer exists to read it
from. Every other tier-4 line is overwritten on each install.

- DataSpoke subsystem: `DATASPOKE_DEV_POSTGRES_{HOST,PORT,USER,PASSWORD,DB}`,
  `DATASPOKE_DEV_REDIS_{HOST,PORT,PASSWORD}`,
  `DATASPOKE_DEV_AIRFLOW_{URL,USER,PASSWORD}`,
  `DATASPOKE_DEV_INTERNAL_TOKEN`,
  `DATASPOKE_DEV_JWT_SECRET_KEY` (conftest promotes it to `DATASPOKE_JWT_SECRET_KEY`
  so locally-minted JWTs verify against the API pod)
- DataHub access: `DATASPOKE_DEV_DATAHUB_{GMS_URL,TOKEN,KAFKA_BROKERS,FRONTEND_URL}` —
  the connection half of the block whose install inputs Tier 3 carries.
  `FRONTEND_URL` is the browser-facing UI URL, carried separately because it is not
  derivable from `GMS_URL`; the integration reset helpers restore it into
  `peripheral_config` so a reset leaves the dev UI with a working DataHub link
- Langfuse access: `DATASPOKE_DEV_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}` — the
  connection half of the block whose install internals Tier 3 carries
- Dev-lock access: `DATASPOKE_DEV_LOCK_URL` — full base URL of the dev-env lock
  service (`http://<host>:9221`, host per the laptop-side rule above). The
  integration and E2E lock protocol uses `$DATASPOKE_DEV_LOCK_URL/lock/...`.
- Dummy data source access: `DATASPOKE_DEV_DUMMY_DATA_{POSTGRES_HOST,POSTGRES_PORT,KAFKA_BROKERS}`
  — the address half of the block whose credentials Tier 3 carries; laptop-side,
  mode-dependent host (per the rule above), used by tests that read the example
  source directly.
- `DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT` — **in-cluster**
  cluster-DNS address of the example Postgres
  (`example-postgres.<dummy-data-ns>.svc.cluster.local:5432`),
  **mode-independent**. Used by the in-cluster API pod when it builds
  ingestion source recipes, so it is always the cluster-internal address
  regardless of how a laptop reaches the same database.

### Tier 5 — Prod-only inputs (`DATASPOKE_PROD_*`)

Operator-supplied inputs for a prod install, carried in `helm-charts/.env.prod`
and read by `bin/install-prod-preflight.sh` and `bin/post-install/*.sh`. The
credential subset is mapped into the credentials Secret; application pods read
that Secret and never the env file.

**The eleven credential inputs.** `DATASPOKE_PROD_<X>` supplies Secret key
`DATASPOKE_<X>` for exactly these eleven suffixes — the same set §Secret keys
enumerates:

`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`,
`AIRFLOW_WEBSERVER_SECRET_KEY`, `AIRFLOW_JWT_SECRET`, `AIRFLOW_FERNET_KEY`,
`INTERNAL_TOKEN`, `JWT_SECRET_KEY`, `OAUTH_STATE_SECRET`,
`GOOGLE_OAUTH_CLIENT_SECRET`.

**The substitution is scoped to those eleven names, not to the prefix.**
`DATASPOKE_PROD_PERIPHERAL_*`, `DATASPOKE_PROD_LLM_*`, and
`DATASPOKE_PROD_ADMIN_PASSWORD` are not Secret keys and have no `DATASPOKE_<X>`
counterpart; a blanket rule over the whole prefix would synthesise Secret keys
that do not exist. The prefix is what keeps the unprefixed tier-1 names out of
every env file, where a stale copy would shadow the Secret for anything that
sources the file — which is why the pre-flight rejects a bare tier-1 name in
`.env.prod` (§Prod operator workflow).

**Resolution order**, applied per key by the pre-flight:

1. the operator's own value, when the line is non-empty;
2. otherwise the value this cluster's Secret already uses (**adopt**);
3. otherwise a freshly generated value, written back into the env file.

Adoption precedes generation for two reasons. It is what stops a re-install
contradicting a running deployment, and it is what makes the Airflow Fernet key
recoverable rather than regenerated against a retained Postgres PVC — the
coupling [§What a prod uninstall leaves
behind](#what-a-prod-uninstall-leaves-behind) describes. A blank line is
therefore a **request**, not an omission, and the env file becomes the
operator's copy of record after the first run.

**Identity and the seeded admin:**

- `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID` — the operator's record of the public
  half of the OAuth pair. The deployment itself reads `auth.googleClientId` from
  the values overlay, and drift between the two halves fails at the OAuth
  callback rather than at install, so the pre-flight compares them.
- `DATASPOKE_PROD_ADMIN_PASSWORD` — the password post-install rotates the
  built-in admin to. 10–128 characters, per `MePatchRequest` in
  `src/api/schemas/auth.py`, and not the literal `dataspoke`. That range is the
  API-enforced floor the pre-flight and the rotation share, so a value the API
  would reject fails before the install rather than at the PATCH; §Policies is
  the stricter operator standard the value should meet.

**LLM inference.** `DATASPOKE_PROD_LLM_{PROVIDER,MODEL,API_KEY}`, symmetric with
`DATASPOKE_DEV_LLM_*`. These are not chart values: they are applied post-install
into the `runtime_config` row via `PATCH /internal/admin/conf`, with the API
routing the key into `dataspoke-llm-secret` itself.

**Peripheral connections**, `DATASPOKE_PROD_PERIPHERAL_{DATAHUB,LANGFUSE}_*`,
applied post-install via `PATCH /internal/admin/peripherals/{datahub,langfuse}`.
**Each suffix is the API contract field it carries, upper-cased**, so the
schemas in `src/api/schemas/admin.py` stay the single authority and no
translation table exists to drift:

- DataHub — `_GMS_URL`, `_FRONTEND_URL`, `_TOKEN`, `_KAFKA_BROKERS`,
  `_KAFKA_SECURITY_PROTOCOL`, `_KAFKA_SASL_MECHANISM`, `_KAFKA_SASL_USERNAME`,
  `_KAFKA_SASL_PASSWORD`, `_KAFKA_AWS_REGION`, `_SERVICE_CORPUSER_URN`,
  `_DEFAULT_ENV`. `gms_url` addresses the GMS service and `frontend_url` is the
  browser-facing UI URL; they differ in host, port and scheme in most
  deployments and neither is derivable from the other. Dev's counterparts are the
  auto-populated `DATASPOKE_DEV_DATAHUB_{GMS_URL,FRONTEND_URL,TOKEN,KAFKA_BROKERS}`
  (§Tier 4 — Dev access); the remaining suffixes have no dev env var and take
  their peripheral defaults.
- Langfuse — `_HOST`, `_PUBLIC_KEY`, `_SECRET_KEY`, `_PROJECT_ID`,
  `_ENVIRONMENT_TAG`. Dev's counterparts are
  `DATASPOKE_DEV_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}` and
  `DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID`.

The six Kafka fields are cross-validated by the API as a **set**
(`validate_datahub_kafka_security`), not field by field, so which of them an
operator fills in follows from the security posture rather than from the list.
The four common postures:

| Posture | Fields an operator fills in |
|---|---|
| `PLAINTEXT` | brokers only |
| `SSL` | brokers, protocol — no mechanism, which the API rejects outside the `SASL_*` protocols |
| `SASL_SSL` + `AWS_MSK_IAM` | brokers, protocol, mechanism — and **no** username or password, because the consumer authenticates as its ServiceAccount's IAM role. Region is optional: blank derives it from the broker hostnames, and a supplied value must agree with the region those hostnames encode |
| `SASL_SSL` or `SASL_PLAINTEXT` with a typed mechanism | brokers, protocol, mechanism, username, password |

[`API.md` §DataHub Kafka
security](../API.md#datahub-kafka-security) carries the complete rule set,
including the constraints on the `AWS_MSK_IAM` broker hostnames that make the
pod's IAM identity non-redirectable. This table is a filling-in guide, not a
second copy of it.

**SMTP is deliberately absent from this tier.** It is set with
`PATCH /api/v1/admin/peripherals/smtp` against the running deployment and no
install step needs it, so it stays out of the env file rather than being carried
for symmetry.

### Policies

- Password policy for operator-chosen credentials: 16+ chars, mixed case, at
  least one special character. Where the API also bounds a value —
  `DATASPOKE_PROD_ADMIN_PASSWORD` at 10–128 — the API's bound is the floor a
  malformed input fails against and this policy is the target a sound one meets.
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
substitutes from `.env` via `sed`, mirroring the GMS ingress rendering in
`bin/dev-peripherals/datahub.sh` (`__DATAHUB_GMS_INGRESS_HOST__`,
`__DATAHUB_INGRESS_CLASS__`, `__DATAHUB_SSL_REDIRECT__`).

**Kafka `advertisedListeners`** is not embedded statically either:
`datahub.sh` injects it with `--set-string` from the datahub namespace plus the
resolved ingress host, so the advertised EXTERNAL listener always matches the
operator's namespace and host.

---

## The .env File

| Path | Tracked | Purpose |
|---|---|---|
| `helm-charts/.env.dev` | gitignored | Per-developer dev-profile values |
| `helm-charts/.env.prod` | gitignored | Per-cluster prod-profile values — deployment shape plus the `DATASPOKE_PROD_*` operator inputs, credential inputs included |
| `helm-charts/.env.dev.example` | tracked | Dev canonical listing (three sections) |
| `helm-charts/.env.prod.example` | tracked | Prod operator template — the full variable set with placeholders |
| `helm-charts/.env.prod.<name>-no-credential.example` | tracked, optional | Optional per-deployment copy source: one real deployment's shape, credential-free |

The runtime env file is profile-named (`.env.<profile>`); copy the matching
`.example` to create it. `install.sh`/`uninstall.sh` resolve it from `--env-file`
or default to `.env.<profile>`. No auto-rename shim is provided.

Dev layout: three top-level sections — Kube deployment operator inputs, Dev
profile operator inputs (Tier 3), and the auto-populated block written back by
the install (the ingress vars plus Tier 4). The section boundary, not the
prefix, is what tells an operator which lines are theirs to edit.
`bin/*.sh` scripts source it; `tests/integration/conftest.py` loads it for
integration tests.

Prod `.env.prod` is the single operator input file: the `DATASPOKE_KUBE_*`
deployment shape plus the `DATASPOKE_PROD_*` inputs of §Tier 5, the eleven
credential inputs among them. `bin/install-prod-preflight.sh` resolves those
into the credentials Secret the values overlay names in
`secrets.existingSecret`; the release reads the Secret and no pod ever reads the
file.

**Tracked example, gitignored copy.** The rule separating them is about which
copy, not which variables: a tracked `*.example` carries the full variable set
with placeholders and no real value, while the gitignored `.env.prod` carries
the real ones. `helm-charts/.env.prod.<name>-no-credential.example` is the
optional per-deployment form of that rule — one real deployment's shape with
every credential line left blank, so a team reproduces an install without
re-deriving the cluster context, registry and ingress hosts. `.gitignore`'s
`!.env*.example` re-include already permits the filename, so it needs no
exemption of its own. It is a **copy source only, never an `--env-file`
argument**. Credential-free is not disclosure-free: such a file still names the
cluster context, cloud project, registry and ingress hosts, so it belongs only
in a private deployment repo.

In dev, the **auto-populated** block is written by install scripts, not edited by
hand: in managed mode `DATASPOKE_KUBE_INGRESS_{IP,DOMAIN}` (by
`dev-peripherals/nginx-ingress.sh`; shared mode leaves `INGRESS_IP` blank and reads
the operator-pre-set `INGRESS_DOMAIN`), `DATASPOKE_DEV_DATAHUB_*` (by
`dev-peripherals/datahub.sh`), `DATASPOKE_DEV_LANGFUSE_*` (by
`dev-peripherals/langfuse.sh`), and the full Tier-4 subsystem block —
including `DATASPOKE_DEV_LOCK_URL` and
`DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT` — by `install.sh`
post-install (`_sync_env_from_secret` extracts credentials from the
`dataspoke-secrets` K8s Secret, `DATASPOKE_DEV_POSTGRES_{USER,DB}` come from
the app ConfigMap, and the host-bearing vars take the laptop-side
TCP host for the active ingress mode).

---

## Configuration Flow

```
.env.dev  →  bin/install.sh (dev)
              │
              ├─ _ensure_dataspoke_secrets
              │    dev: auto-generate dataspoke-secrets on first install; an existing
              │         Secret is left as-is apart from the two self-heals — Fernet-key
              │         patch-in and DATASPOKE_POSTGRES_{USER,DB} removal
              │         (see §Secrets Management)
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
              │    Extract dataspoke-secrets values → append the Tier-4 block to .env.dev
              │    DATASPOKE_DEV_POSTGRES_{USER,DB} come from the app ConfigMap
              │    Also appends DATASPOKE_DEV_DATAHUB_*, DATASPOKE_DEV_LANGFUSE_*,
              │    DATASPOKE_DEV_DUMMY_DATA_* from peripheral install outputs
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

Prod is a **two-command sequence**: `bin/install-prod-preflight.sh` validates and
populates, then `bin/install.sh --profile prod` mutates the release. Everything
the operator supplies lives in one file — `helm-charts/.env.prod` — plus the
values overlay.

| # | Step | Interface |
|---|---|---|
| 1 | Apply the cluster-scoped prerequisites — at minimum the StorageClass the overlay pins | `kubectl apply -f` against manifests derived from `helm-charts/prod-prereq/` (cluster-admin) |
| 2 | Fill in `helm-charts/.env.prod`: the deployment shape, the Google OAuth client secret, and whichever other `DATASPOKE_PROD_*` inputs are the operator's to know. A blank credential line is a request, not an omission (§Tier 5) | Copy `helm-charts/.env.prod.example`, or a committed `.env.prod.<name>-no-credential.example` when the deployment publishes one |
| 3 | Write the values overlay: `secrets.existingSecret`, ingress hosts and the published path list, TLS, registry, replica counts, storage classes. The IngressClass is *not* an overlay field — it comes from `DATASPOKE_KUBE_INGRESS_CLASS` in `.env.prod` (see §Ingress) | Start from `helm-charts/values-prod.example.yaml` |
| 4 | Pre-flight — validate the three configuration planes, resolve the eleven credentials into the env file, create the credentials Secret, derive the image tag | `bin/install-prod-preflight.sh --values <overlay.yaml> [--create-namespace]` |
| 5 | Install — the chart, then the automatic admin seed | `bin/install.sh --profile prod --image-tag <tag> --values <overlay.yaml>`, with the tag the pre-flight resolved |
| 6 | **Rotate the default admin credential — required.** Already done when `DATASPOKE_PROD_ADMIN_PASSWORD` is set, since the step-5 seed rotates and verifies it | `bin/post-install/seed-admin-user.sh`, else `PATCH /api/v1/auth/me` |
| 7 | Register peripherals and LLM settings | `bin/post-install/seed-{peripheral-config,runtime-config}.sh` under `ENV_FILE=helm-charts/.env.prod`; `/api/v1/admin/peripherals/smtp` for SMTP, which the env file deliberately does not carry |

The literal copy-paste command sequence, including the manual Secret-creation
equivalent and verification probes, lives in
[`helm-charts/README.md`](../../helm-charts/README.md).

#### The pre-flight

`bin/install-prod-preflight.sh` **validates and populates; it never installs,
upgrades, deletes, or builds.** Its only three mutations are writing resolved
credentials into the env file, creating the namespace (behind
`--create-namespace`), and creating the credentials Secret. `--verify-only`
removes all three, which is what makes it safe to run against a live prod
deployment as an audit.

Seven stages, announced `<n>/7`, with populate third:

| # | Stage | Must hold |
|---|---|---|
| 1 | env file | the deployment-shape vars are present; no unprefixed tier-1 `DATASPOKE_*` name appears; no `stub_*` toggle appears; `DATASPOKE_PROD_ADMIN_PASSWORD`, when set, is 10–128 characters and not `dataspoke`; the current kubectl context equals `DATASPOKE_KUBE_CLUSTER` |
| 2 | values overlay | the API ingress does not publish `/internal/*`; no Airflow SimpleAuthManager conflict; `secrets.existingSecret` resolves; `auth.googleClientId` agrees with `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID` |
| 3 | credential populate | the eleven keys resolve by the §Tier 5 order — the operator's value, else this cluster's Secret, else a generated value written back. Only `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_SECRET` can fail to resolve, since nothing can generate it, and it stops the run |
| 4 | cluster prerequisites | the namespace exists, or `--create-namespace` creates it; the `DATASPOKE_KUBE_INGRESS_CLASS` IngressClass exists; every StorageClass the overlay pins exists with a usable CSI driver, on the terms the storage paragraph below states |
| 5 | credentials Secret | created from the env file when absent; when present, **verified and never rewritten** — drift is reported by key name and then stops the run |
| 6 | post-install readiness | the DataHub and LLM blocks are complete, each stopping the run (`--skip-postinstall-check` overrides); the Kafka tuple is required **only when `event-consumer.enabled` resolves true** in the effective values, so a deployment that never runs the consumer is not made to supply brokers it never uses; an incomplete Langfuse block warns only, because its absence disables tracing and nothing else |
| 7 | image tag | an explicit `--image-tag`, else `git rev-parse --short HEAD` on a clean tree, with `--allow-dirty` to override; never a mutable tag |

**Two orderings are load-bearing.** The stage-1 kubectl-context check and the
stage-2 overlay resolution both precede populate: populate reads the cluster to
adopt, so resolving under the wrong context would write another deployment's
credentials into this file, and the Secret it adopts from is the one the overlay
names.

**An existing Secret is never rewritten**, drift included. It may hold the only
surviving copy of material a retained PVC depends on, so the decision belongs to
the operator and the pre-flight's job is to name the keys that differ. Refusing
a dirty tree for the git-HEAD tag is the same principle applied to images — a
tag naming a commit that does not contain what is being deployed is worse than
no tag.

**The pre-flight shares its gates with `install.sh` through
`bin/lib/helpers.sh`**, so a pass here means `install.sh`'s own pre-flight
passes. That invariant is what justifies the split into two commands: the same
predicates are evaluated first where a failure costs nothing, and `install.sh`
still evaluates every one of them itself.

**`--skip-secret`** is the path for operators who deliver the eleven keys through
ExternalSecrets, Vault or SealedSecrets. Every other stage still **validates**,
and the Secret's content contract is identical either way — which is what keeps
those operators on the same validated path rather than on a guessed one. Populate
does not *mint* under this flag: a blank line reads as "delivered out of band", so
nothing is generated and nothing is written back. Minting would put eleven
credentials on the operator's disk on the one path whose purpose is keeping them
off it, and every one of them would differ from what the external system
ultimately delivers — leaving the next run's drift check reporting all eleven
through no fault of the operator.

**No credential ever reaches argv.** The Secret is created from a `0600` `mktemp`
env file via `kubectl create secret --from-env-file`, never `--from-literal`,
which would leak every value into shell history and into `ps auxww` /
`/proc/<pid>/cmdline` for the process's lifetime. There is no interactive prompt
and no credential-bearing flag either, so the same command works in a terminal
and in CI.

**The reciprocal contract.** `install.sh --profile prod` never creates or
modifies the credentials Secret, and aborts when it is absent. That is what makes
the pre-flight a genuine prerequisite rather than a convenience wrapper, and it
is why an env file alone cannot deploy a credential.

**Cluster-scoped prerequisites of a namespace-scoped release.** The Helm release
owns namespaced objects only, so anything cluster-scoped it depends on must exist
before the install and outlive the uninstall. `helm-charts/prod-prereq/` is where
those manifests live; StorageClass is the first case, and the directory is the
convention for any that follow. The prod pre-flight resolves fifteen overlay
keys across two spellings: the Bitnami `postgresql.primary.persistence.
storageClass`, `redis.{master,replica}.persistence.storageClass`, and the
`defaultStorageClass` / `storageClass` fallbacks both at the top level and
inside the `postgresql:` and `redis:` blocks — a subchart-scoped `global:`
still reaches the child as `.Values.global`, and `common.storage.class` ranks
it ahead of the component's own key, so a pin there shadows the others; and
the apache-airflow chart's `airflow.{logs,dags,triggerer,workers,
workers.celery,redis}.persistence.storageClassName` — note the different key name
(`storageClassName`, not `storageClass`), a copy-paste trap when adapting a
Bitnami-shaped snippet to an Airflow key. It fails fast on any pinned name
that does not exist, mirroring the IngressClass probe beside it — with one
exception: a literal `-` (the Bitnami convention for "bind a pre-provisioned
PV, skip dynamic provisioning") is accepted without a cluster lookup only on
the keys whose template maps it to an empty `storageClassName` — the nine
Bitnami keys and the Airflow `logs`/`dags` keys. `triggerer`, `workers`,
`workers.celery`, and `redis` pass the value straight through with no such
mapping, so a `-` there is rejected by the pre-flight instead of reaching
Kubernetes as a literal (and invalid) class name. Existence alone is not
sufficient either: the pre-flight reads the pinned class's `.provisioner` and,
wherever a `CSIDriver` could exist for it, checks that one is registered —
a class whose driver is absent strands the PVC `Pending` exactly as a missing
class does. What a missing driver costs depends on the provisioner's shape. A
bare DNS-subdomain name (`ebs.csi.aws.com`) is an out-of-tree CSI driver and the
one unambiguous case, so an absent `CSIDriver` there **aborts**. The three
CSI-migrated in-tree names (`kubernetes.io/{aws-ebs,gce-pd,azure-disk}`) are
looked up under their CSI successors (`ebs.csi.aws.com`,
`pd.csi.storage.gke.io`, `disk.csi.azure.com`), because a class may keep
declaring the in-tree name while provisioning is delegated to a separately
installed addon — EKS's default `gp2` is exactly that — but a cluster genuinely
still running the in-tree plugin is equally legitimate, so absence there
**warns**. Every other `kubernetes.io/*` name, `kubernetes.io/no-provisioner`
included, is exempt with no lookup, and an external non-CSI provisioner in
`vendor/name` form (`rancher.io/local-path`) **warns** and skips, since no
`CSIDriver` will ever exist for it. A `Forbidden` reply is reported as itself
rather than as absence, so an installer identity lacking `get` on
`csidrivers.storage.k8s.io` (see `helm-charts/prod-prereq/`) is told to fix its
RBAC instead of to install a driver that may already be there. An overlay that pins
nothing skips the check and takes the cluster default. **Failing here is the
point**: a missing class otherwise leaves the PVC `Pending`, the owning
component never starts, the API's `wait-for-postgres` init container loops
(for the Postgres/Redis keys), and the install dies on a rollout timeout
whose symptom names the workload rather than storage. Recovery then needs
the stuck PVCs deleted, because `storageClassName` is immutable once bound.

**The namespace needs no pre-creating.** `install.sh`'s prod pre-flight calls
`ensure_namespace` before any other check, so
`DATASPOKE_KUBE_DATASPOKE_NAMESPACE` is created if absent. The credentials Secret
is namespace-scoped and is created a command earlier, so the namespace has to
exist by the time `install-prod-preflight.sh` reaches stage 5 —
`--create-namespace` is what covers that without a separate operator step. Only
the ordering is at stake: `ensure_namespace` is idempotent and adopts a namespace
the operator (or the pre-flight) made.

**`install.sh`'s own pre-flight is a hard gate.** Before touching the chart the
prod install fails
fast on: a missing `DATASPOKE_KUBE_INGRESS_CLASS` IngressClass; a StorageClass
the overlay pins that does not exist in the cluster, or that names a bare
out-of-tree CSI provisioner with no registered `CSIDriver` — the only
provisioner shape that aborts, the CSI-migrated in-tree and external
non-CSI shapes warning instead (per the storage paragraph
above); a missing credentials Secret; any of the eleven required keys absent or empty;
a credentials Secret still carrying `DATASPOKE_POSTGRES_USER` or
`DATASPOKE_POSTGRES_DB`, which belong to the app ConfigMap and would otherwise
stand as a second, silently divergent source of the Postgres identity;
`DATASPOKE_AIRFLOW_FERNET_KEY` not shaped like a Fernet key (URL-safe base64 of
exactly 32 raw bytes — 43 characters then `=`), which catches the
`openssl rand -hex 32` value every other key uses and which would otherwise fail
only the first time Airflow encrypts or decrypts a connection, long after the
install reported success; `DATASPOKE_JWT_SECRET_KEY` still set to the dev
default; `DATASPOKE_AIRFLOW_USER` equal to `admin`, or outside the charset
allowlist Airflow's user-list grammar requires; an operator overlay that sets any
of `airflow.apiServer.{extraInitContainers,extraVolumes,extraVolumeMounts}`,
which would replace the passwords-file wiring; a `DATASPOKE_AIRFLOW_PASSWORD`
of `admin`, on the branch condition §Airflow authentication states normatively
(an *empty* password is already covered by the eleven-key rule above, in either
branch); `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` still the dev
placeholder. The missing-key message for the Fernet key names the recovery read
rather than a generator, because a namespace with a retained metadata DB needs
the exact key that DB was encrypted with. An
explicit `--image-tag` is also required, so a shared registry never receives the
mutable `:dev` tag.

**The admin seed runs automatically.** After the chart phase, the prod install
invokes `seed-admin-user.sh` unless `--skip-seed` is passed. It POSTs
`/internal/admin/bootstrap` from inside the API pod (§Post-Install Seeding), so
the step needs neither DNS nor the API's public ingress and the overlay's
`api.ingress` host is a free choice. The endpoint is idempotent — it returns
`created: false` when any Admin already exists, so re-running an install is
safe — and makes no external call, so a 503 from it means the API's own Postgres
is unreachable, not a peripheral problem.

**Rotation is required, not advisory.** A first install therefore returns with
an active Admin account whose credentials — `dataspoke@dataspoke.local /
dataspoke` — are published in this repository. The same seed rotates it to
`DATASPOKE_PROD_ADMIN_PASSWORD` when that input is set, closing the window in
the same command; with the input blank the published credential stays live and
the deployment is not production-ready until the operator rotates it by hand.
Who can reach that account depends on
the operator's ingress controller and network posture, which the prod profile
does not own or configure; the chart adds no source-range restriction or
inbound policy of its own. Operators who want no default credential to exist at
all install with `--skip-seed` and seed deliberately later (see below).

**Seeding by hand.** Under `--skip-seed`, or to re-run the seed after fixing a
failure, invoke the script directly:

```
ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-admin-user.sh
```

The `ENV_FILE=` prefix is required — the script defaults it to `.env.dev`.
The install's own invocation needs no prefix because `install.sh` exports the
resolved env file for child scripts.

**Peripheral registration is the operator's, not the installer's.** DataHub
URL/token, Langfuse host/keys, and LLM provider/model/key are all registered
after the release exists, through the admin API — by hand, or by the two seed
scripts reading the `DATASPOKE_PROD_PERIPHERAL_*` and `DATASPOKE_PROD_LLM_*`
blocks of the same env file (§Post-Install Seeding). Until then the dependent
features stay inert rather than failing the install.

### ConfigMap keys (non-sensitive)

`DATASPOKE_POSTGRES_{HOST,PORT,USER,DB}`,
`DATASPOKE_REDIS_{HOST,PORT}`, `DATASPOKE_AIRFLOW_{URL,CALLBACK_BASE_URL}`,
plus the six chart-values-only keys — `DATASPOKE_CORS_ORIGINS`,
`DATASPOKE_COOKIE_SECURE`, `DATASPOKE_GOOGLE_OAUTH_CLIENT_ID`,
`DATASPOKE_OAUTH_POST_LOGIN_REDIRECT`, `DATASPOKE_RATE_LIMIT_PER_MINUTE`,
`FORWARDED_ALLOW_IPS` — which come from
chart values, not `.env`
(their source values and roles are in §Configuration — Five-Tier Env Vars).
`DATASPOKE_AIRFLOW_CALLBACK_BASE_URL` is hardcoded in the chart (`http://dataspoke-api:8002`);
it is not derived from `.env`.

**Two of these keys carry a value-shape constraint — the same shape, for
different reasons.** `DATASPOKE_POSTGRES_USER` must be a valid SQL identifier
because it is interpolated into `GRANT` statement text rather than bound as a
parameter, so a name needing quoting — or carrying a statement terminator —
turns bootstrap SQL into a syntax error or an injection point.
`DATASPOKE_POSTGRES_DB` is not SQL at all but an argv element: it reaches
`psql -d`, where a leading `-` is parsed as a flag rather than a database name,
and any shell metacharacter lands on the same command line as those `GRANT`s.

**Only the dev path checks either shape.** `install.sh` validates both against
an identifier regex immediately before it issues the AGE `GRANT` SQL, and aborts
rather than proceeding. That check and the `GRANT` it guards are dev-only, and
the prod pre-flight cannot stand in for them — the values are not in the Secret
that pre-flight reads. What holds in prod instead is that both are chart values
an operator must deliberately override, with the failure then surfacing at
initdb bootstrap rather than at pre-flight. The consistency guard below is not a
shape check — it compares the two sides for agreement, nothing more.

**The Postgres identity is asserted consistent at render time.**
`templates/configmap.yaml` `fail`s when `config.postgres.user` disagrees with
`postgresql.auth.username`, or `config.postgres.db` with
`postgresql.auth.database`. Without the guard an operator who changes one side
passes every pre-flight and deploys a healthy-looking stack whose API then
authenticates against the bundled database with a role or database name that
was never created.

**The role name is chart-pinned, and changing it is unsupported.** It occurs in
four places that would all have to agree: `postgresql.auth.username`,
`config.postgres.user`, bare literals in the subchart's `initdb` scripts in both
values files (the two AGE `GRANT`s and `CREATE DATABASE airflow OWNER <role>`),
and the Airflow metadata DSN, which hardcodes `dataspoke` because that DSN's
database is created owned by it (§Secrets Management). The render guard reaches
the first two only, so an overlay that changes both consistently still renders a
bootstrap granting AGE to a role which does not exist and failing the
Airflow-DB creation, while Airflow goes on connecting as `dataspoke`.

**The database name, by contrast, is a clean two-site change the guard fully
covers.** It has no unguarded further site: the initdb scripts never name it —
they run against `postgresql.auth.database` as their default connection — and
the Airflow metadata DSN addresses Airflow's own `airflow` database, not this
one.

Both guards are gated on `postgresql.enabled`, because with the bundled subchart
off there is no `postgresql.auth.*` for the ConfigMap to be compared against.
That gate is not by itself a complete external-database story: the derived
Airflow metadata DSN still targets the bundled `dataspoke-postgresql`
(§Secrets Management).

### Secret keys (`dataspoke-secrets`, mounted via `envFrom`)

Eleven keys, the same set in dev and prod. The whole Secret is mounted
`envFrom` on the API pods, but three of the keys are Airflow key material that
DataSpoke code never reads — `DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY`,
`DATASPOKE_AIRFLOW_JWT_SECRET`, `DATASPOKE_AIRFLOW_FERNET_KEY` reach Airflow
through single-key projections instead (see §Secrets Management):

`DATASPOKE_POSTGRES_PASSWORD`, `DATASPOKE_REDIS_PASSWORD`,
`DATASPOKE_AIRFLOW_{USER,PASSWORD}`,
`DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY`, `DATASPOKE_AIRFLOW_JWT_SECRET`,
`DATASPOKE_AIRFLOW_FERNET_KEY`,
`DATASPOKE_INTERNAL_TOKEN`, `DATASPOKE_JWT_SECRET_KEY`,
`DATASPOKE_OAUTH_STATE_SECRET`, `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET`.

The Postgres role and database names are not secrets and are not carried here —
they live in the app ConfigMap, which is also where their shape constraints and
the chart's consistency guard are described (§ConfigMap keys).

**A leftover key here shadows the ConfigMap rather than sitting inertly beside
it.** Every container that consumes both lists `configMapRef` before
`secretRef` in its `envFrom`, and duplicate keys resolve last-source-wins, so a
credentials Secret still carrying `DATASPOKE_POSTGRES_USER` or
`DATASPOKE_POSTGRES_DB` silently overrides the ConfigMap value the chart
guarded. That is why such a key is removed in dev and rejected in prod instead
of ignored, and it makes the order of those two `envFrom` entries load-bearing
for any future edit to the pod templates.

Within this Secret, `DATASPOKE_AIRFLOW_FERNET_KEY` is the one key with a
value-shape constraint rather than merely a length one: Fernet accepts only
URL-safe base64 of 32 raw bytes, so the hex encoding used for every other
generated key is rejected.

In dev, `install.sh` auto-generates this Secret — the OAuth state secret and
JWT signing key are random; the Google client secret is sourced from
`DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET` in `.env` (placeholder if absent,
which causes the OAuth callback to fail until the operator supplies a real
value). In prod, the operator pre-creates the whole Secret and points the
chart at it via `secrets.existingSecret: <name>`.

### DB-backed (no env var)

- `peripheral_config` table — non-secret connection fields for DataHub
  (`gms_url`, `frontend_url`, `kafka_brokers`, `kafka_security_protocol`,
  `kafka_sasl_mechanism`, `kafka_sasl_username`, `kafka_aws_region`,
  `kafka_sasl_password_version`, `service_corpuser_urn`, `default_env`), Langfuse
  (`host`, `public_key`, `project_id`, `environment_tag`), and SMTP
  (`host`, `port`, `username`, `from_address`, `use_tls`) — updated via
  `/api/v1/admin/peripherals/{datahub,langfuse,smtp}`. Per-peripheral secret
  fields (`datahub.token`, `datahub.kafka_sasl_password`,
  `langfuse.secret_key`, `smtp.password`) are
  routed by the PATCH handler to dedicated K8s Secrets, never to the DB —
  see Out-of-band Secrets below. [API.md](../API.md) §`/admin/peripherals` is the
  contract; this list mirrors it.
- `peripheral_health` table — last observed liveness per transport, written by
  the event consumer (`datahub`) and the hourly sync sweep (`datahub-api`) and
  read back on `GET /api/v1/admin/peripherals/datahub`.
  Not operator-configurable and not env-driven.
- `runtime_config` table — LLM provider/model, debate/RAG/iteration tunables,
  and `auth_datahub_corp_group` (string, default `dataspoke-users` — names
  the DataHub corpGroup that marks DataSpoke-managed users) — updated via
  `/api/v1/admin/conf`.

### Out-of-band Secret

`dataspoke-llm-secret` (key `api_key`) — LLM provider API key. Provisioned by
`install.sh` from `DATASPOKE_DEV_LLM_API_KEY` in dev; in prod written by the API
itself when `seed-runtime-config.sh` PATCHes
`DATASPOKE_PROD_LLM_API_KEY` through `/internal/admin/conf`, or by an operator
(`kubectl` / External Secrets Operator). The API reads it at runtime
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
`${REGISTRY}/api:dev` (or the operator-supplied tag in prod).

### Digest stamping

Image tags are mutable: a rebuild pushed to the same tag leaves the rendered pod
template byte-identical, so `helm upgrade` finds nothing to change and the
running pods keep the old image. The three DataSpoke-owned workloads — **api**,
**event-consumer**, **frontend** — close that gap by digest pinning. After the
push, `install.sh` resolves the image's `sha256:` content digest (`resolve_image_digest`
in `bin/lib/helpers.sh`) and, when resolution succeeds, both (a) renders that
workload's image reference as `<repository>@<digest>` instead of the mutable
`<repository>:<tag>` (`api.image.digest` / `frontend.image.digest` /
`event-consumer.image.digest`, consumed by the umbrella chart's
`dataspoke.imageRef` named template — the frontend and event-consumer
subcharts define their own chart-scoped `frontend.imageRef` /
`event-consumer.imageRef` with an identical body so each lints and renders
standalone) and (b) stamps the same value as a `dataspoke.io/image-digest`
annotation delivered through the workload's existing `podAnnotations` map —
`api.podAnnotations`, `event-consumer.podAnnotations`, `frontend.podAnnotations`
— composing with the `cluster-autoscaler.kubernetes.io/safe-to-evict` entry
already in those maps (§Eviction resilience) rather than replacing them. This
annotation is provenance only — useful for `kubectl get deploy -o jsonpath` —
and nothing in `install.sh` ever reads it back to decide what to deploy.
Pinning the image reference itself (not only the annotation) is what makes the
guarantee real regardless of `imagePullPolicy`: a cached `repo@sha256:X` can
only ever be content `X`, so `IfNotPresent` (the default for every chart) is
as safe as `Always`. Because the digest is part of both the image field and
the pod template, the pod-template hash changes exactly when a freshly
resolved digest changes, so Helm rolls the workload by construction.

**Two outcomes only.** Resolution for a given workload
(`_resolve_digest_or_abort` in `bin/install.sh`) is one of:

- **`resolve_image_digest` succeeds** — pin the freshly resolved digest. Helm
  rolls the workload by construction, as described above.
- **`resolve_image_digest` fails** — abort the install (exit 1), strictly
  BEFORE the umbrella `helm upgrade` runs, naming the image reference and
  carrying the underlying resolution failure (`resolve_image_digest` already
  `warn`s the registry error, missing-CLI, or network cause to stderr
  immediately above this abort message) and pointing at `--no-digest-pin` as
  the recovery path.

This is the whole contract: the installer never reads cluster state — a
Deployment's live image, a pod annotation, a prior run's outcome — to decide
what to deploy. Every input to the deployed image reference comes from THIS
run's own build/registry lookup. That is what makes a stale-content deploy
structurally impossible rather than guarded against by a second layer of
comparison logic.

**`--no-digest-pin`** is the explicit, operator-chosen escape hatch that
replaces every implicit fallback. When passed, `install.sh` skips digest
resolution entirely for that run — `resolve_image_digest` is never called, no
`image.digest` `--set` flag is emitted, and every workload renders the chart's
default mutable `<repository>:<tag>` reference. Because a same-tag rebuild
then leaves the pod template byte-identical, `install.sh` also forces that
workload's `image.pullPolicy` to `Always` (chart default: `IfNotPresent`,
safe only under the digest-pinned reference above) and unconditionally issues
an explicit `kubectl rollout restart` after the umbrella upgrade for
`dataspoke-api`, `dataspoke-event-consumer`, and `dataspoke-frontend` (the
last only when it is actually deployed — `frontend.enabled=true`). The
`pullPolicy` override is what makes the restart actually land new content: a
bare `kubectl rollout restart` does not force a re-pull, so without it a node
that already has the reused tag cached keeps serving the stale image while
the rollout still reports success. This is the pre-digest-pinning behavior,
now reached only when explicitly requested instead of by degrading
resolution.

Resolution is vendor-dependent and not equally strong everywhere: the GCP
branch (`gcloud artifacts docker images describe`) and the AWS branch (`aws
ecr describe-images`) both query the registry directly — each retrying its
call up to 3 times, 2s apart, to ride out a transient network blip rather than
aborting the install on one bad request. Each also short-circuits that retry
loop on the one response it knows is not transient — gcloud's `NOT_FOUND` and
AWS's literal `"None"` (both mean the image/tag genuinely does not exist in
the registry) — rather than waiting out the full 3 attempts on a result that
cannot change. Each vendor raises that verdict in more than one shape, so the
match covers both: on GCP a missing *repository* and a missing *image or tag*
surface as different SDK errors, and on AWS the not-found exceptions sit
alongside the exit-0 `"None"` result. Any other failure (auth, network,
malformed response) still rides out all 3 attempts. The local/no-vendor branch falls
back to the local Docker daemon's recorded `RepoDigests` for the image — i.e.
what *this host* last pushed, not necessarily what the registry's tag
currently resolves to; it is not retried, since a local daemon state check
gains nothing from repeating it 2s later. **This is a real gap, not just a
weaker guarantee:** on a `--skip-build` install run from a host whose local
Docker cache holds a STALE `<repository>:<tag>` (built and cached by an
earlier, different run, never refreshed by this run's own build step), this
branch resolves SUCCESSFULLY — it finds a `RepoDigests` entry and returns a
well-formed `sha256:...` — but to the OLD content's digest, not necessarily
what the registry's tag currently serves. A successful resolve is therefore
not by itself proof the pinned digest matches what a fresh pull of the tag
would produce on this vendor; only the GCP/AWS branches query the registry
directly and close that gap. Operators on the local/no-vendor path who deploy
from a host distinct from the one that built and pushed the image should
prefer `DATASPOKE_KUBE_CLOUD_VENDOR=GCP` or `AWS` for a registry-side
guarantee.

**airflow and postgres are not digest-stamped.** Both are third-party
subcharts whose pod templates DataSpoke does not author: Airflow renders four
workloads (api-server, scheduler, triggerer, dag-processor) from the one
image, so the one digest would have to be stamped into four separate
`podAnnotations` maps kept in lockstep — a cost paid on every install for a
component that changes only on a DAG or image edit; PostgreSQL is a
StatefulSet, where a template-driven roll is a stateful data-plane operation
rather than a code push. `install.sh` restarts neither automatically after a
rebuild: `_restart_airflow_key_consumers` rolls the four Airflow workloads
only when a signing-key Secret was rotated, not on an image update, and there
is no automated PostgreSQL restart at all. Updating a DAG or the PG image is
therefore a rebuild, the umbrella upgrade, and an operator-issued explicit
`kubectl rollout restart` of the affected Deployments (`restart statefulset`
for PostgreSQL).

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
| Writes to .env.dev | `DATASPOKE_KUBE_INGRESS_IP`, `DATASPOKE_KUBE_INGRESS_DOMAIN`, plus the IP-derived `DATASPOKE_DEV_*` host/broker vars |

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
- **Frontend ingress takes the class through `className`** — the subchart
  (0.3.4) uses `className`, not `ingressClassName`; the wrong key is silently
  dropped and GKE falls back to provisioning a GCE LoadBalancer. The value is
  the resolved `ingress_class()`.
- **`datahub-gms` and `datahub-frontend` Services are `ClusterIP`** — the chart
  defaults both to `LoadBalancer`, which on AWS/EKS provisions redundant NLBs
  beside the nginx Ingress and exposes the GMS metadata API plus jmx/prometheus
  ports. DataSpoke overrides both to `ClusterIP`; external access is solely
  through the Ingress (GMS at `datahub-gms.<domain>/`, frontend at
  `datahub.<domain>/`).
- **GMS has its own hostname, not a path on the frontend host** — a plain
  host-root route (`datahub-gms.<domain>`, path `/`, `pathType: Prefix`) needs
  no regex path match and no rewrite annotation, so it behaves identically on
  community ingress-nginx (`k8s.io/ingress-nginx`) and NGINX Inc
  (`nginx.org/ingress-controller`), and leaves no route-level obstacle for other
  controllers, which supply their own class-specific annotations (an ALB class,
  for instance, needs `target-type: ip` for the `ClusterIP` backend). Sharing
  the frontend's host would require a second Ingress on an already-claimed host
  plus rewrite annotations — a combination only the community controller honors.
  The hostname derivation is centralized in the `datahub_gms_host()` helper in
  `bin/lib/helpers.sh`. The split is for laptop-side
  test, tooling, and install access only: in-cluster callers reach GMS over
  cluster DNS (`datahub-datahub-gms.<ns>.svc.cluster.local:8080`) and are
  unaffected by the ingress topology.

Service name prefixes: `datahub-prerequisites-*` for the prerequisites
release (MySQL, Kafka controller); `opensearch-cluster-master` for the
OpenSearch subchart's own release.

Writes to .env.dev: `DATASPOKE_DEV_DATAHUB_GMS_URL`
(`<SCHEME>://datahub-gms.<INGRESS_DOMAIN>`), `DATASPOKE_DEV_DATAHUB_TOKEN`
(generated PAT), `DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS`,
`DATASPOKE_DEV_DATAHUB_FRONTEND_URL` (browser-facing UI URL).

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

Writes to .env.dev: `DATASPOKE_DEV_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY}` plus
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

Runs after the umbrella chart's API deployment is Ready. Each script is
standalone and reads whichever env file `ENV_FILE=` names, **selecting its source
from the prefix that file declares** — `DATASPOKE_PROD_*` or `DATASPOKE_DEV_*`,
by name presence and never by value, so a placeholder line still names its
profile. One shared resolver (`seed_profile` in `bin/lib/helpers.sh`) answers for
every seed, so no two of them can disagree about which profile they are running
against; a file declaring both prefixes is ambiguous and aborts the seed rather
than being guessed at, and one declaring neither names no source and is skipped.
`ENV_FILE=` alone therefore picks the profile and no script carries a profile
flag. `install.sh` exports the resolved env file, so its
own invocations need no prefix; a hand-run against prod does
(`ENV_FILE=helm-charts/.env.prod`), because the default is `.env.dev`.

| Script | Effect |
|---|---|
| `bin/post-install/seed-peripheral-config.sh` | One script over two sources, selected by which prefix the env file declares. **Dev** derives the connection from the dev peripheral topology: PATCH `/internal/admin/peripherals/datahub` with `{gms_url, frontend_url, kafka_brokers}` and `/internal/admin/peripherals/langfuse` with `{host, public_key}`, plus the optional non-secret fields set in `.env.dev` — DataHub `service_corpuser_urn` and `default_env`; Langfuse `project_id` (from `DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID`) and `environment_tag`. The DataHub Kafka security tuple is not seeded — the dev broker is plaintext, which is the field's default. The dev secret fields — DataHub PAT `token` and Langfuse `secret_key` — are placed into K8s Secrets by `install.sh` before the API pod starts, and the dev path never sends them through the admin API. **Prod** takes the operator's connection verbatim from `DATASPOKE_PROD_PERIPHERAL_*`, **secret fields included**, and lets the API route the DataHub PAT, the Kafka SASL password and the Langfuse secret key into `dataspoke-{datahub,langfuse}-secret` itself — so the prod path creates no Secret, and those Secrets must not be pre-created. |
| `bin/post-install/seed-runtime-config.sh` | **Dev** PATCHes `/internal/admin/conf` with `{llm_provider, llm_model}` from `DATASPOKE_DEV_LLM_{PROVIDER,MODEL}`, then a second PATCH setting the four `stub_*` dependency flags (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`) to `true`. **Prod** seeds `DATASPOKE_PROD_LLM_{PROVIDER,MODEL,API_KEY}` — refusing a provider without a model, since the inference loop needs both, and warning on an empty key — and sets **no `stub_*` flag at all**. |
| `bin/post-install/seed-admin-user.sh` | POST `/internal/admin/bootstrap` to idempotently seed the built-in `dataspoke@dataspoke.local / dataspoke` Admin user (returns `{created: false}` when any Admin already exists). The endpoint makes no DataHub call, so this step has no ordering dependency on peripheral seeding and succeeds on a fresh install before DataHub is wired. When `DATASPOKE_PROD_ADMIN_PASSWORD` is set, the script then rotates that account to it. See [feature/AUTH.md §Built-in Bootstrap Admin](AUTH.md#built-in-bootstrap-admin). |

**The prod path sets no `stub_*` flag.** The four toggles are a dev mechanism
(`BACKEND_LLM.md §Test Mode`); a production deployment silently running on a stub
Redis, LLM, pgvector manager or notification service answers `200` and delivers
none of it, which fails invisibly. A deployment that fails loudly is the better
outcome, so the prod path leaves the flags unwritten rather than asserting them
`false`.

**Admin rotation closes the published-credential window.** When
`DATASPOKE_PROD_ADMIN_PASSWORD` is set, `seed-admin-user.sh` rotates
`dataspoke@dataspoke.local` to it immediately after bootstrap. The whole exchange
runs inside the API pod in one `kubectl exec`: the target password arrives on
stdin, the access token is obtained and discarded in-process, and only a one-word
verdict comes back — `ROTATED`, `ALREADY_ROTATED`, `NO_KNOWN_PASSWORD`,
`PATCH_FAILED_<code>`, `VERIFY_FAILED`, `UNREACHABLE`. It is idempotent by
construction because it tries the target password first. `NO_KNOWN_PASSWORD` —
neither the target nor the published default authenticates — warns and leaves the
account alone rather than guessing at it. The address is not configurable:
`PATCH /auth/me` sets name and password only.

**Payload construction.** Both paths of both config seeds build their JSON with a
serialiser rather than by string concatenation — a quote or backslash in a
DataHub PAT otherwise produces a `422` naming a field the operator never typed —
and treat an absent value and an empty value identically as "leave unchanged",
since an empty string is a *clearing* write for a secret field.

**Transport and auth.** The shared
`api_internal_request <namespace> <METHOD> <path> <json-body> [timeout]` helper
in `bin/lib/helpers.sh` carries every `/internal/*` call the installer makes —
all three seed scripts, plus the dev `--components api` fast path's
`POST /internal/admin/dags/verify`. It `kubectl exec`s into the `dataspoke-api` pod and
runs a stdlib `urllib.request` call against `http://127.0.0.1:8002`, the API's
own container port. The call therefore never leaves the cluster: no ingress
host, no DNS, and no `curl` in the `python:3.13-slim` API image. The in-pod
script reads `DATASPOKE_INTERNAL_TOKEN` from its own environment — mounted from
the `dataspoke-secrets` Secret via `envFrom` — and sends it as
`X-Internal-Token`, so the seed path itself never copies the token out of the
pod. A prod install exports it nowhere; dev's Tier-4 sync deliberately does,
writing `DATASPOKE_DEV_INTERNAL_TOKEN` into `.env.dev` for the integration
tests (§Tier 4 — Dev access). The namespace comes from `ENV_FILE`. The helper prints the HTTP status on the
first line and the body on the rest, with `000` standing for a connection
failure; only that case is retried (5 attempts, 3s apart), while any HTTP
response, 4xx and 5xx included, returns immediately. `timeout` bounds each
attempt's in-pod request at 10s by default; the DAG-verification call raises it
to 70 because that endpoint's `AirflowClient.list_dags()` carries its own 60s
client timeout, and a warming Airflow under the default would be misread as a
connection failure. A retried worst case is therefore bounded by five timeouts
plus the four sleeps between them, not by the sleeps alone — an immediately
refused connection is the fast end of that range. Setting
`API_INTERNAL_REQUEST_QUIET=1` downgrades a `kubectl exec` failure from an abort
to a warning plus a non-zero return, which the best-effort DAG-verification call
site does and the seed scripts do not.

**Credential-bearing payloads reach the in-pod caller through stdin, not argv**,
so a DataHub PAT, a Kafka SASL password, a Langfuse secret key, an LLM API key or
an admin password never appears in `ps auxww` or `/proc/<pid>/cmdline` — on the
operator's machine or inside the pod.

Skip with `--skip-seed`; useful when a previous install already seeded
peripheral config and the operator wants to preserve their PATCHes.

**Profile coverage.** The dev install runs all three scripts. The prod install
runs `seed-admin-user.sh` automatically after the chart phase; the other two are
the operator's to run once the peripheral and LLM blocks of `.env.prod` are
filled in, and both are re-runnable at any time against a live deployment.

---

## Resource Sizing

### Production defaults

Per-pod figures. Multiply by the replica count to reach the row's contribution.

| Component | Replicas | CPU Req / Limit | Mem Req / Limit | PV |
|---|---|---|---|---|
| frontend | 2 | 250m / 500m | 256 Mi / 512 Mi | — |
| api | 2 | 500m / 1000m | 512 Mi / 1024 Mi | — |
| event-consumer† | 1 | 250m / 500m | 512 Mi / 1024 Mi | — |
| postgresql | 1 | 1000m / 2000m | 2048 Mi / 6144 Mi | 50 Gi (custom image with `pgvector` + Apache AGE) |
| redis master | 1 | 250m / 500m | 256 Mi / 512 Mi | 8 Gi |
| redis replica | 1 | 250m / 500m | 256 Mi / 512 Mi | 8 Gi |
| airflow api-server | 1 | 250m / 1000m | 512 Mi / 1024 Mi | — |
| airflow scheduler | 1 | 500m / 1500m | 1536 Mi / 3072 Mi | — |
| airflow triggerer | 1 | 200m / 750m | 768 Mi / 1536 Mi | — |
| airflow dag-processor | 1 | 200m / 500m | 512 Mi / 1024 Mi | — |
| airflow logGroomer sidecar | ×3 (one per scheduler / triggerer / dag-processor pod) | 50m / 100m each | 128 Mi / 512 Mi each | — |
| airflow statsd | 1 | 50m / 100m | 64 Mi / 128 Mi | — |
| airflow db-migrate‡ | hook Job | 200m / 500m | 512 Mi / 1024 Mi | — |
| **Total** (steady state; excludes event-consumer and the db-migrate hook) | | **4350m / 10150m** | **7.7 Gi / 18.1 Gi** | **66 Gi** (postgresql 50 + redis 2×8) |

† event-consumer disabled by default — add ~250m / 500m CPU + ~512 Mi / 1024 Mi
memory when enabled.

‡ `db-migrate` is the Airflow subchart's **`post-install`/`post-upgrade`** hook
Job (`templates/jobs/migrate-database-job.yaml`, hook-weight 1, delete policy
`before-hook-creation,hook-succeeded`). It is not steady-state load, but it runs
*after* every workload has been applied, so an install or upgrade transiently
needs its 200m / 512 Mi of request on top of the Total. Because it is a post
hook, its failure leaves a partially live release: the manifests are all applied
and each Airflow pod sits in its own `wait-for-airflow-migrations` init container
waiting for a schema that never arrives — a different recovery story from a
`pre-install` failure, which creates nothing and leaves the cluster untouched.

**Redis master and replica are sized identically, deliberately.** A replica holds
the same dataset as its master, so a smaller replica OOMKills, full-resyncs, and
OOMKills again while the master stays healthy and the failure reads as a replica
bug. The Bitnami chart's `resourcesPreset` mechanism makes the asymmetry easy to
introduce by accident: an explicit `resources:` map **replaces** the preset
wholesale rather than merging with it, so setting `master.resources` alone leaves
the replica on the `nano` preset. The same replace-not-merge rule is why both
pods carry an explicit `ephemeral-storage` pair rather than inheriting the
preset's.

Airflow components differ from one another because their failure modes do. The
**triggerer** holds every deferred task's trigger instance in a single asyncio
loop, so its memory scales with deferred tasks in flight and losing it strands
all of them — it is the tightest allocation in the chart and the one whose
memory floor is raised furthest. Its CPU request rises proportionally less than
its memory, because on nodes where allocatable CPU is the scarce dimension a
memory-request increase is often free while every CPU-request increase shrinks
the set of nodes the pod can land on.

**A raised scheduler CPU limit has a derived side effect.** The Airflow subchart
computes `config.celery.sync_parallelism` from the `cpu_count` of the scheduler's
CPU **limit**, so changing that limit moves the rendered `airflow.cfg` and its
config checksum. Inert under the baseline `LocalExecutor`, live under any Celery
executor an operator switches to.

The Total row's scope is every container of every steady-state pod the umbrella
chart renders from `values.yaml`, including the three logGroomer sidecars. It
excludes the `db-migrate` hook Job per the note above, and all init containers,
which never raise a pod's effective request — Kubernetes takes `max(largest init
container, sum of app containers)`, and in this chart every pod's app containers
request at least as much as its largest init container. The Airflow subchart
applies each component's own `resources` block to its `wait-for-airflow-migrations`
init container, so on those pods the two sides are equal rather than the app side
being larger; the conclusion is unchanged either way. The passwords-file init
container the umbrella adds to the Airflow api-server pod (§Airflow
authentication) is sized within that component's envelope for the same reason, so
it does not become the `max()` winner.

### Chart invariant — every container is sized

**No container in the rendered chart lacks both requests and limits.** The rule
covers init containers, sidecars, and hook Jobs, not just the main container of
each workload. A container with neither lands in the BestEffort QoS class, which
the kubelet evicts first under node pressure; for the `post-install`/`post-upgrade`
`db-migrate` hook Job that means the whole install or upgrade fails with every
workload already applied and blocked on its migration wait, and for a sidecar it
means the pod loses a component while the main container survives.

The invariant is enforced by review, not by CI: a render walk over `helm template`
output that enumerates every container across every workload kind. The failure
mode it guards is a **subchart version bump introducing a new unsized
container** — the Airflow, PostgreSQL, and Redis subcharts each add components
across minor releases, and a new one arrives unsized unless the umbrella pins it.
Re-run the walk whenever a `Chart.yaml` dependency version changes.

### Redis memory policy

The chart pins `redis.commonConfiguration` with `maxmemory` and
`maxmemory-policy noeviction`, applied to master and replica alike.

**`noeviction` because this Redis is not a *pure* cache.** Alongside the API
response cache it carries refresh-token revocation keys
(`src/backend/auth/tokens.py`) and distributed locks (`src/shared/cache/client.py`
— `SET NX` plus a Lua compare-and-delete). Under any LRU or LFU policy Redis
would drop those keys silently and, by its own logic, *correctly*: they look
exactly like cold cache entries. A dropped revocation key un-revokes a refresh
token; a dropped lock key releases a lock nobody holds. `noeviction` converts
both into a loud write failure instead.

**The rate limiter's keyspace is what makes the `maxmemory` headroom
load-bearing.** It is the one tenant that grows without bound and is reachable
without authentication: the limiter derives one bucket key per distinct
caller-supplied bearer or client address, and for an API-token-prefixed bearer it
hashes the string into a key *without resolving the token*, so an unauthenticated
caller mints a fresh key per request. The cached-read keyspace is the opposite
shape — entity-scoped, one key per dataset URN or ontology entity id (see
[BACKEND.md §Cache Key Conventions](BACKEND.md#cache-key-conventions)) — bounded
by catalog size and written only behind authenticated routes. `maxmemory` is an **instance-wide** budget with no
per-logical-DB isolation, and the limiter runs in its own logical DB
(`RATE_LIMIT_REDIS_DB`), so filling that DB starves writes on the auth-critical
keys — revocation entries and distributed locks — sharing the same budget.

**One known asymmetry.** The rate limiter (`src/api/middleware/rate_limit.py`)
shares this Redis, and its application-wide default limiter runs with
`in_memory_fallback_enabled=True` — on any `RedisError`, OOM included, it degrades
silently to per-process counting rather than surfacing the failure. The
fail-closed limiter on the credential-accepting auth routes is the opposite by
design: no fallback, errors not swallowed, so it answers 503 (see [API.md §Middleware
Stack](../API.md#middleware-stack)). A Redis at `maxmemory` therefore weakens the
general limit quietly while the brute-force control fails closed.

**`maxmemory` sits well below the container memory limit, not at it — but that
gap is headroom, not a safety guarantee.** Redis's own accounting covers the
dataset but excludes replica output buffers and part of the AOF buffer, while
the cgroup counts all of it. Setting `maxmemory` at the container limit hands
the kernel an OOMKill before Redis ever reports OOM to a client — which defeats
the point of `noeviction`, since the loud failure never reaches the caller. Two
residual risks remain even with the gap in place:

- **BGREWRITEAOF fork copy-on-write is the actual consumer of the headroom.**
  `appendonly yes` means AOF rewrites fork a child process; pages the parent
  does not modify after the fork stay shared, but pages either process writes
  afterward are duplicated, and both processes are charged to the same cgroup.
  If that copy-on-write growth exceeds what the gap leaves, the outcome is a
  kernel OOMKill of the whole pod — exactly the silent, non-client-visible
  failure mode `noeviction` is chosen to avoid for ordinary write traffic.
- **The tightened `client-output-buffer-limit replica` cap is a trade, not a
  free reduction.** The chart sets `64mb 32mb 60`, tighter than the upstream
  default's `256mb 64mb 60`. This swaps the OOMKill risk the wider default
  would pose (a replica buffer large enough to fit alongside the dataset
  *inside* the container limit during a resync) for a master-side forced
  disconnect and replica resync-retry loop if write volume during a full
  resync fills the 64mb buffer before the replica drains it. The application
  connects only to `dataspoke-redis-master`, never the replica, so this trade
  costs standby durability during a resync window — not the request path.

`commonConfiguration` is a plain scalar in the Bitnami chart, so setting it
replaces the upstream default rather than extending it. The upstream directives —
the two `loadmodule` lines, `appendonly yes`, and `save ""` — must be carried
forward verbatim alongside the additions, or AOF durability is silently switched
off.

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
(dev-lock, frontend, Airflow logGroomer sidecars) keep the Autopilot default.

Redis is the one low-log component that carries an explicit pair anyway in the
production defaults — 50 Mi request and 2 Gi limit on master and replica alike.
The figures are a restoration rather than a sizing decision: the Bitnami `nano`
preset supplies them, and the chart's explicit `resources:` map replaces that
preset wholesale (see §Production defaults). On Autopilot the effective ceiling
is therefore the 50 Mi request rather than the 1 GiB default, which suffices
because Redis logs sparsely and its AOF and RDB files live on the PVC, not on
ephemeral storage.

The table lists the **configured `limits.ephemeral-storage`**. On Autopilot the
effective ceiling is the paired `requests.ephemeral-storage`, which the umbrella
chart sets to half the limit for the six entries it owns below — 4 Gi for the
four Airflow components, 2 Gi for `dataspoke-api` and `postgresql`. The
datahub-\*, OpenSearch, Kafka, MySQL and example-\* rows are peripheral-chart
values, and the redis pair above is a preset restoration rather than a half-split.
Size against the request, not the limit; the Airflow
`logs.emptyDirConfig.sizeLimit` of 2 Gi is chosen to stay under the 4 Gi Airflow
request for that reason.

Airflow-side containers the umbrella chart sizes but the table does not list —
statsd, the `db-migrate` hook Job, and the api-server pod's passwords-file init
container (§Airflow authentication) — carry an `ephemeral-storage`
request/limit pair on the same per-component convention, scaled to their much
smaller footprint.

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
| Virtual hosts | ✓ (app/api/datahub/datahub-gms/airflow/langfuse) | ✓ (same hosts, on the operator's controller) |
| TCP datastores | exposed via LoadBalancer TCP passthrough on the IP | not exposed — reached on `127.0.0.1` via `bin/port-forward.sh` |
| Kafka EXTERNAL listener advertises | `<INGRESS_IP>:<port>` | `127.0.0.1:<port>` |
| Teardown | `uninstall.sh` removes the controller | controller left untouched |

Shared mode is the path for clusters where another system owns the ingress
controller and DNS, so DataSpoke must not install or modify them. Prod follows
the same reuse posture: the operator's controller serves the hosts, with
`values.yaml` ingress hosts and TLS secrets operator-supplied.

Frontend, API, and Airflow each have an `ingress` block in their values owning
the host, path, and TLS (cert-manager annotations, `tls:` blocks) of their rule.
The class key in those blocks — `api`/`frontend.ingress.className`,
`airflow.ingress.apiServer.ingressClassName` — holds a chart default; the
effective class is supplied by the install.

**One class, one source.** Every Ingress in the table below binds to the class
resolved by `ingress_class()` — `DATASPOKE_KUBE_INGRESS_CLASS`, default `nginx`
in dev and required explicitly in prod, where a default would silently
republish onto whatever controller happens to be named `nginx`:
`install.sh` supplies it to the umbrella chart's API, frontend, and Airflow
ingresses; each `bin/dev-peripherals/*.sh` supplies it to its own chart (DataHub
frontend, Langfuse); and it is substituted into the GMS kubectl manifest. All
three paths set it by `--set`/substitution, which outranks any values file, so a
class written into a values overlay has no effect — an operator on `alb` or
`traefik` changes the env var. In managed mode the same value is the name the
owned controller registers as its own `ingressClassResource`, so a mismatch
between controller and resource is not representable.

**Route correctness depends on no annotation.** No rule carries a regex match or
a rewrite annotation, because path-splitting and rewriting are honored only by
specific controllers. The chart's own default rule for every host is the root
path (`/`), which matches everything and leaves the rendered `pathType`
immaterial — whichever value a chart defaults to behaves identically. The prod
example overlay replaces the API's rule with an explicit path list, and there
`pathType: Prefix` is load-bearing rather than immaterial: the published
prefixes are the boundary of what the ingress exposes, so the list and the
`pathType` beside it are read together.

**Annotation spellings are vendor-specific, and the chart's reach differs by
knob.** `DATASPOKE_KUBE_INGRESS_CLASS` selects a class *name* only, and in
shared mode a class named `nginx` may be served by either the community
controller (`k8s.io/ingress-nginx`, which reads
`nginx.ingress.kubernetes.io/*`) or NGINX Inc./F5's
(`nginx.org/ingress-controller`, which reads `nginx.org/*`); each ignores the
other's namespace entirely. For routing that is immaterial — an unrecognised
annotation falls back to that controller's own default for the knob, and no
such default changes where a request is routed. For functional knobs the
foreign default is not the chart's intent, and the two vendors differ in key
name as well as prefix:

- **Body size is dual-spelled.** `nginx.ingress.kubernetes.io/proxy-body-size`
  and `nginx.org/client-max-body-size` (there is no
  `nginx.org/proxy-body-size`) both pin `50m` on every rule that raises the
  limit. The NGINX Inc. default is `1m`, so a single spelling would let a
  1 MB–50 MB payload fail `413` at the proxy under the other controller —
  load-bearing on the GMS host, the metadata-push path.
- **The HTTPS redirect is community-spelled only.**
  `nginx.ingress.kubernetes.io/ssl-redirect` is pinned off wherever the chart
  sets it, except on the GMS rule, which derives the value from
  `DATASPOKE_KUBE_INGRESS_SCHEME` and so refuses the plaintext hop under
  `https` — that host carries the DataHub personal access token on every call.
  There is no `nginx.org/` counterpart, so under a shared NGINX Inc.
  controller the redirect follows that controller's own default
  (`nginx.org/ssl-redirect`, `True`) rather than the chart's setting. Both
  controllers gate the redirect on the server actually holding a certificate,
  which confines the divergence to hosts that terminate TLS. That is the
  boundary of the chart's control over the redirect; deriving it deliberately
  across both controllers is tracked separately.

`nginx.org/redirect-to-https` (default `False`) is the separate knob for TLS
terminated *upstream* of the controller; DataSpoke does not set it, leaving
that case to the operator's controller configuration. See the [NGINX Inc.
annotation
reference](https://docs.nginx.com/nginx-ingress-controller/configuration/ingress-resources/advanced-configuration-with-annotations/).

**The prod example overlay publishes the API's public surface only.** Its API
rule publishes five paths — `/api/v1`, `/health`, `/ready`, `/redoc`, and
`/openapi.json`; `/internal/*` is not published. The two documentation paths are
in that list because they expose only the already-public surface: both internal
routers are registered `include_in_schema=False` in `src/api/main.py`, so the
schema never describes `/internal/*`, and the frontend's "API docs" navigation
item links to `/redoc`. Dropping them is available as a hardening step at the
cost of that link. Where a
host-root rule is in force instead, `/internal/*` is reachable through the
ingress and the `X-Internal-Token` shared secret is the only control on it.

**The GMS host is public and relies on GMS's own token auth.** No Ingress
DataSpoke creates carries an allow-list, source-range restriction, or auth
annotation; `datahub-gms.<domain>` is reachable by anyone who resolves it, and
authentication is enforced inside GMS on every path except its fixed unauthenticated
allow-list (`/health`, `/config`, `/schema-registry/*`, `/actuator/prometheus`).
Because it accepts a long-lived personal access token, an operator publishing it
on a shared controller gives it the same certificate-SAN and WAF treatment as the
API host, not just a DNS record.

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
| `dev-peripherals/datahub/gms-ingress.yaml` | kubectl manifest | `datahub-gms.<INGRESS_IP>.nip.io/` → `datahub-datahub-gms:8080` |
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
tests, `health-check.sh`, and `helm-charts/.env.dev`'s TCP `DATASPOKE_DEV_*`
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
| **`dataspoke-secrets`** | `install.sh` (dev auto-generate) or operator (prod pre-create) | DataSpoke's own runtime credentials — Postgres password, Redis password, Airflow user/password/webserver-secret/jwt-secret/fernet-key, internal-auth token, JWT signing key, OAuth state secret, Google OAuth client secret. Eleven keys; mounted `envFrom` on the API Deployment and alembic-migrate init container. The Postgres role and database names are non-secret and live in the app ConfigMap instead (§ConfigMap keys). |
| **`dataspoke-airflow-metadata-db`** | `install.sh` `_derive_airflow_metadata_secret` (both profiles) | Single key `connection` = full PostgreSQL URI for Airflow's metadata DB, wired via `airflow.data.metadataSecretName`. It composes the credentials Secret's `DATASPOKE_POSTGRES_PASSWORD` into a fixed `dataspoke@dataspoke-postgresql:5432/airflow` — **the password is the only part that varies**. The role is the literal `dataspoke`, read from neither the Secret nor the ConfigMap: the `airflow` database is created `OWNER dataspoke` by the bundled subchart's initdb, so the DSN's role must be that owner, and setting `config.postgres.user` does not reach Airflow. That `airflow` database is Airflow's own metadata store, unrelated to `DATASPOKE_POSTGRES_DB`. Compare-and-rotate: the URI is re-derived on every run and the Secret rewritten whenever the derived value differs, so the Secret tracks the derived value rather than its first-install content. A change to a **non-empty** prior value additionally restarts the four Airflow consumers (api-server, scheduler, dag-processor, triggerer) at the point §Installation fixes for the signing-key restart. That restart is mandatory rather than incidental: with `data.metadataSecretName` set the subchart renders no metadata Secret of its own, so the `checksum/metadata-secret` annotation on its Deployments is a constant and Helm never rolls them on a DSN change. |
| **`dataspoke-airflow-api-secret-key`**, **`dataspoke-airflow-jwt-secret`** | `install.sh` `_ensure_airflow_key_secrets` (both profiles) | Projections of the `DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY` / `DATASPOKE_AIRFLOW_JWT_SECRET` keys into the single-key shape (`api-secret-key`, `jwt-secret`) the Airflow chart expects. Wired via `airflow.apiSecretKeySecretName` / `airflow.jwtSecretName`. |
| **`dataspoke-airflow-metadata-encryption-key`** | `install.sh` `_ensure_airflow_fernet_secret` (both profiles) | Projection of `DATASPOKE_AIRFLOW_FERNET_KEY` into the single-key shape (`fernet-key`) the Airflow chart expects. Wired via `airflow.fernetKeySecretName`, which is pinned in the chart's own `values.yaml` so it applies to every profile; pinning it also suppresses the subchart's pre-install hook that would otherwise generate and own a key of its own. The name states the key's job — it encrypts the contents of the database whose connection `dataspoke-airflow-metadata-db` carries. |
| **Out-of-band Secrets** (`dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret`) | Operator (`kubectl` / ESO) or the app on first PATCH | Tokens/keys that rotate online via `/api/v1/admin/conf` and `/api/v1/admin/peripherals/*`. Not Helm-managed — `helm upgrade` would clobber rotations. The app tolerates their absence (reads as unset). `dataspoke-datahub-secret` carries two keys — `token` for GMS and `kafka_sasl_password` for the event consumer's Kafka credential — and is the only one of these Secrets read by a workload other than the API. `dataspoke-smtp-secret` (key `password`) backs `/auth/password/reset/request` (see [feature/AUTH.md](AUTH.md)). Note: a Secret of the same name `dataspoke-langfuse-secret` also exists in the Langfuse namespace (`langfuse-01`) carrying the full set of Langfuse pod credentials (NextAuth, salt, ClickHouse, MinIO, Postgres, Redis, init-user); the DataSpoke-side copy holds only the project `secret_key` consumed by the API via RBAC. |
| **User-supplied source credentials** (`dataspoke-source-cred-*`) | Caller (vault path) or operator (reference path) | Credentials for *external sources* registered via ingestion confs. Documented in [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md). The `dataspoke-source-cred-` name prefix is enforced as a security boundary so callers cannot overwrite the above Secrets. |

### Dev — install-time provisioning

`install.sh _ensure_dataspoke_secrets` auto-generates `dataspoke-secrets` with
`openssl rand -hex 32` on first install. The step is idempotent — an existing Secret
is not overwritten (Postgres PV data remains decryptable across reinstalls). Subcharts
reference `dataspoke-secrets` via `auth.existingSecret`. Out-of-band Secrets are
populated only if their seed value is present in `.env`; if absent, the dependent
feature stays disabled until the operator sets it via the admin API.

For `DATASPOKE_AIRFLOW_FERNET_KEY`, generation is the last resort: the step first
adopts whatever key is already live on the cluster, searching the Secret the
deployed release names in `airflow.fernetKeySecretName`, then the
`dataspoke-airflow-metadata-encryption-key` projection, then the legacy
`dataspoke-airflow-fernet-key` hook Secret — so a credentials Secret rebuilt or
re-generated while the release is live keeps the Postgres PVC's Airflow
connections and Variables decryptable. What it generates when there is nothing to
adopt follows the encoding constraint in §Secret keys, not `openssl rand -hex`.

Dev writes into an existing credentials Secret in exactly two self-heal cases,
and nothing else in the Secret is touched:

- **A missing `DATASPOKE_AIRFLOW_FERNET_KEY` is patched in** by the same
  adoption path, rather than the Secret being left on a shape that would
  hard-fail the projection step later with no remediation.
- **A `DATASPOKE_POSTGRES_USER` or `DATASPOKE_POSTGRES_DB` still present is
  removed**, so the app ConfigMap stays the single source of the Postgres
  identity and the two cannot drift apart unnoticed.

Prod takes neither path — `install.sh` never mutates an operator-owned Secret —
so there both a missing Fernet key and a lingering `DATASPOKE_POSTGRES_{USER,DB}`
are pre-flight failures instead. The asymmetry is the same in both cases: dev
owns the Secret it generated and may repair it; prod does not.

Adoption reads in-cluster state only, so it does not span a `bin/uninstall.sh`
dev teardown: that deletes the credentials Secret and its projection together,
while the PVCs survive by default. The legacy `dataspoke-airflow-fernet-key`
hook Secret is the exception in both profiles — it is dropped only when it
provably duplicates the credentials Secret's key, per [§What a prod uninstall
leaves behind](#what-a-prod-uninstall-leaves-behind). The keep-or-drop-together rule of [§What a
prod uninstall leaves behind](#what-a-prod-uninstall-leaves-behind) then governs
the whole Secret, not just the Fernet key — the reinstall auto-generates
a fresh credential set, `DATASPOKE_POSTGRES_PASSWORD` included, so retained
volumes are stranded on a rotated database password as much as on an encryption
key that no longer reads their Fernet-encrypted rows. The sanctioned dev reset is
therefore a teardown that drops the PVCs too: `--delete-pvcs`, or `y` at the PVC
prompt (both dev-only).

### Prod

The Secret exists before `install.sh` runs, and the values overlay names it in
`secrets.existingSecret: <name>`. `bin/install-prod-preflight.sh` creates it from
the eleven `DATASPOKE_PROD_*` credential inputs (§Prod operator workflow);
operators who deliver those keys through ExternalSecrets Operator, Vault Agent or
SealedSecrets pass `--skip-secret` and provision it themselves, against the same
content contract. `install.sh` refuses to auto-generate Secrets in the prod
profile and fails fast with a clear message if the named Secret is absent.

### Rotation tolerance of the Airflow projections

All three key-material projections are re-asserted on every install, but they
differ in what a changed source key means. The two signing keys tolerate
rotation: a mismatch is re-projected and the affected pods restarted, costing
only live Airflow sessions. The Fernet key does not — re-projecting a new value
would leave the metadata DB's encrypted rows unreadable with no recovery path.
A disagreement between `DATASPOKE_AIRFLOW_FERNET_KEY` and the live projection
therefore aborts the install and names the command that restores the key, in
both profiles.

What counts as "the live projection" is decided on the value read, not on a
Secret existing: a Secret that is absent, or present with an empty `fernet-key`,
falls through to the next candidate name. A cluster installed before
`airflow.fernetKeySecretName` was pinned therefore still gets a real comparison
instead of an unchecked create.

**Reads resolve the Secret name from the release; the write target is fixed.**
The name to read is taken from the deployed release's
`airflow.fernetKeySecretName` and searched ahead of the two known literals
(`dataspoke-airflow-metadata-encryption-key`, then the legacy
`dataspoke-airflow-fernet-key`). That order governs both the rotation comparison
and the recovery command the failure message names, so each addresses the Secret
Airflow actually mounts. Writes always target
`dataspoke-airflow-metadata-encryption-key` — the name the chart's own
`values.yaml` pins `airflow.fernetKeySecretName` to. **Repointing that value in
an operator overlay is unsupported**: the projection would land in one Secret
while Airflow mounts another, and the install would report success with Airflow
holding no key at all.

### Airflow authentication

Airflow's UI and REST API are guarded by its built-in
[`SimpleAuthManager`](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/auth-manager/simple.html)
in both profiles (`airflow.config.core.auth_manager`), which is also why the
chart disables the subchart's `createUserJob` — that hook runs the FAB-only
`airflow users create`. What differs between profiles is whether the manager
checks a credential.

| | dev | prod |
|---|---|---|
| `core.simple_auth_manager_all_admins` (`values-dev.yaml` / `values.yaml`) | `"True"` | `"False"` |
| Credential checked at login | ✗ — any credentials mint an admin JWT | ✓ — `DATASPOKE_AIRFLOW_{USER,PASSWORD}` |
| Passwords file | none | init-container-materialised (below) |

Neither the user list nor the passwords-file path is a values key. Both reach
Airflow as env vars — `AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_{USERS,PASSWORDS_FILE}`
— emitted by `install.sh`'s `_build_airflow_extra_env_file` under a flag the prod
call site sets and both dev call sites clear. Gating the emission rather than
letting one shared code path apply it everywhere is what keeps the dev render
identical: dev's all-admins mode reads neither variable, so emitting them there
would roll the Airflow pods for two values Airflow never consults.

An `AIRFLOW__*` env var outranks `airflow.cfg`, so
`airflow.config.core.simple_auth_manager_users` set in an operator overlay is
rendered into the config file and then ignored. The env var is the only place
either value takes effect.

Dev trades the credential check for a zero-friction local loop; prod defaults to
the credentialed path. The same credential pair is what the DataSpoke API's own
Airflow client presents (tier-1 `DATASPOKE_AIRFLOW_{USER,PASSWORD}`,
§Configuration — Five-Tier Env Vars), so the user list and the API's identity are
necessarily the same account — a user list naming anyone else leaves every
workflow trigger unauthenticated.

**The user list is a composed value, and its grammar constrains the username.**
Its content is `<DATASPOKE_AIRFLOW_USER>:ADMIN`, so the username must be
*resolved at install time* rather than referenced from the credentials Secret at
pod start — the role suffix makes it a string the installer builds, not a value a
pod can dereference. Airflow parses it as a comma-separated list of
`username:role` pairs, so a username carrying either delimiter parses into a
different user set than the operator wrote.

**The username is therefore held to a positive charset allowlist.** The accepted
shape is `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, enforced
in the prod pre-flight and again inside the emitting helper. Scanning for the two
delimiters instead would not hold in front of a template evaluator: the value
transits Helm's `tpl`,
where a template expression can manufacture a `,` or `:` that no literal-character
scan sees, and a YAML escape (`\x3a`) reaches the same result through the values
parser. Only a positive charset — checked at both the gate and the point of
composition, so neither path can be reached without it — closes that off.

**The passwords file needs a writable volume, not a read-only Secret mount.**
`SimpleAuthManager` keeps its password material in the JSON file named by
`core.simple_auth_manager_passwords_file`. Projecting that file from the
credentials Secret does not work: `SimpleAuthManager.init()` opens it with mode
`a+` and catches only `BlockingIOError`, so a read-only mount raises an uncaught
`OSError` and crashes api-server startup. An init container on the api-server pod
therefore materialises the single-entry mapping `{"<user>": "<password>"}` into
an `emptyDir` mounted at that path, which the api-server container mounts
writable. Pre-seeding — rather than letting Airflow create the file — is what
pins the password to the operator's value: Airflow generates a random password
only for a username *absent* from the file and preserves an entry already
present.

Three properties of that container are load-bearing rather than incidental:

- **The volume is memory-backed (`emptyDir.medium: Memory`) and the file is
  written `0600`.** The file holds a plaintext credential; a default `emptyDir`
  would persist it on the node's filesystem for the pod's lifetime.
- **It carries an explicit request/limit pair**, sized inside the api-server
  component's own envelope, per §Chart invariant — every container is sized. Both
  halves matter on Autopilot, which forces requests equal to limits and injects
  defaults into an unsized container: an unsized or over-requested init container
  would raise the pod's effective request through Kubernetes'
  `max(largest init container, sum of app containers)` rule (see §Production
  defaults).
- **It carries the same hardening as its siblings** —
  `allowPrivilegeEscalation: false` and a dropped `ALL` capability set — so the
  one container in the pod that handles the plaintext credential is not the one
  running with the widest posture.

**Rotation rolls the pod, by construction.** The api-server pod carries an
annotation hashing the effective user/password pair, so a rewritten
`DATASPOKE_AIRFLOW_PASSWORD` changes the pod template and Helm restarts the pod,
which re-runs the init container against the new value. Without it the file is
materialised once at pod creation and never revisited: `dataspoke-api` would roll
onto the new password (it reads the Secret via `envFrom`) while the api-server
kept serving the old file, and every workflow trigger would 401 permanently. This
is the same class of coupling as the signing-key and metadata-DSN restarts in
§Installation, reached by a pod-template change rather than by an explicit
restart.

**Both pieces are install-time injections, not chart content.** The init
container, its volume, and its mount arrive as an extra `-f` values fragment on
the prod `helm upgrade` (`airflow.apiServer.{extraInitContainers,extraVolumes,
extraVolumeMounts}`), and the two env vars arrive via `--set-file
airflow.extraEnv=`. Neither can live in `values.yaml`: Helm always loads a
chart's own `values.yaml` as the base layer even when only `values-dev.yaml` is
passed with `-f`, and `values-dev.yaml` does not reset those `apiServer` keys, so
a static default there would deep-merge into the dev release as well. The
consequence for operators is that **`install.sh` is the only supported path to a
prod upgrade**: a hand-run `helm upgrade -f <overlay>` that bypasses it drops the
init container and both env vars, and Airflow falls back to its own config
defaults — an `admin` user with a password it generates itself, which no one
holds.

**An overlay that touches those three `apiServer` keys replaces the wiring
wholesale.** Helm deep-merges maps but *replaces* lists, so an operator setting
any of `extraInitContainers`, `extraVolumes`, or `extraVolumeMounts` drops
DataSpoke's entry from that list rather than appending to it. The failure is
quiet in exactly the wrong way — a surviving `volumeMount` with no matching
volume renders cleanly through `helm template` and `helm lint`, and is rejected
only by the API server at apply time. The prod pre-flight therefore aborts when
an overlay sets any of the three.

**Only the api-server carries the file.** Airflow calls `init_auth_manager` from
exactly one place (`api_fastapi/app.py`), so the scheduler, dag-processor, and
triggerer need neither the init container nor the volume; they reach the metadata
DB directly and never authenticate through the manager.

**Pre-flight decides which credential is load-bearing — normatively, here.**
`_check_airflow_credentials_prod` reads the *effective*
`airflow.config.core.simple_auth_manager_all_admins` — chart values merged with
the operator overlay — rather than assuming the chart default, because the
overlay is free to turn it back on. Two rules are **unconditional in both
branches** and are not what the effective value governs:

- **Presence.** The eleven-key rule of §Prod operator workflow stands unchanged:
  a `DATASPOKE_AIRFLOW_PASSWORD` that is absent or empty aborts the install
  regardless of the branch, exactly as every other required key does.
- **Username shape.** A `DATASPOKE_AIRFLOW_USER` of `admin`, or one outside the
  allowlist above, aborts in either branch — that account is also DataSpoke's own
  client identity, so its shape is never irrelevant.

What the effective value governs is the **password value check** layered on top
of the presence rule:

- **unset or `False`** (the default posture): the password gates every login, so
  the literal `admin` is a hard error.
- **explicitly `True`**: the credential pair is not consulted at login, so an
  `admin` password is no longer an error. This branch warns **unconditionally**,
  whatever the password happens to be — the exposure is the branch itself, not
  the credential in it: anyone who can reach `airflow.<domain>` is an Airflow
  admin. The chart ships no source-range restriction or inbound policy of its own
  (§Ingress & Network Policy), so that exposure is bounded only by the operator's
  controller and network posture.

**Credentialed `SimpleAuthManager` is still not a production IdP.** Airflow's own
docstring states it "should not be used in production". DataSpoke's use of it is
narrower still — a single account, chosen by this chart rather than imposed by
Airflow, whose password lives in one file on one pod. It is a real improvement
over an unauthenticated admin surface, not an authentication system. Operators
wanting more front the Airflow host with an authenticating proxy: that is neutral
to DataSpoke, whose own client reaches Airflow over cluster DNS and never
traverses the ingress. Replacing `core.auth_manager` outright is the sharper
option and carries a constraint — the replacement must still mint a token for
`DATASPOKE_AIRFLOW_{USER,PASSWORD}`, or every DataSpoke workflow trigger stops
working.

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

### Event-consumer identity and RBAC

The event-consumer subchart renders its own ServiceAccount, Role, and
RoleBinding, governed by `event-consumer.serviceAccount.{create,name,annotations}`.
Two distinct things depend on it.

**Secret access.** The consumer resolves the Kafka SASL credential from
`dataspoke-datahub-secret` at connect time. Its Role is modelled on the API's
but is deliberately much narrower: `get` only, restricted by
`resourceNames: [dataspoke-datahub-secret]`. The API's Role must stay broad
because the source-credential resolver enumerates a whole prefix of Secrets it
cannot name in advance; the consumer has exactly one Secret and never lists.
Same single-namespace policy — no cross-namespace Role or RoleBinding.

**Cloud identity.** `serviceAccount.annotations` is the attachment point for
workload identity. For `kafka_sasl_mechanism = AWS_MSK_IAM` the operator sets
`eks.amazonaws.com/role-arn` there; the MSK signer in the API image then
resolves the projected role at runtime. This is the reason MSK IAM is a
two-plane feature: selecting the mechanism is a DB-plane click, but the identity
it authenticates with can only be granted here, at install time.

An overlay enabling the consumer against MSK therefore sets
`event-consumer.enabled: true` plus the ServiceAccount block, and the operator
must have provisioned two things outside the chart:

| Prerequisite | Requirement |
|---|---|
| IAM role | Trusted by the cluster's OIDC provider, granting `kafka-cluster:Connect`, `DescribeTopic`, `ReadData`, `DescribeGroup`, and `JoinGroup` on the cluster, topic, and consumer-group ARNs |
| Network | MSK security-group ingress from the EKS pod network on the IAM listener port (`9098`) |

Neither is chart-installable — both live in the operator's cloud account. When
either is missing the consumer starts, fails to authenticate, and reports
`status: error` on the DataHub peripheral's `health` field, which is the
intended diagnostic path (see
[BACKEND §Health reporting](BACKEND.md#health-reporting)). See AWS's
[MSK IAM access control](https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html)
for the policy and ARN forms.

---

## Health Check

`bin/health-check.sh --profile {dev|prod}` probes each service through
nginx-ingress (HTTP endpoints) or the laptop-side TCP host (TCP services — the
ingress IP in managed mode, `127.0.0.1` via `bin/port-forward.sh` in shared
mode). `--profile` resolves `helm-charts/.env.<profile>` by the same rule
`install.sh` uses, with `--env-file` overriding it, and the script echoes the
resolved env file and ingress domain before its first probe — so a confident
verdict is never read off a deployment other than the one intended. Required
before any integration test run per `TESTING.md §Prerequisites`. On failure,
reinstall the affected subsystem via
`bin/install.sh --profile dev --components <name>`; `--components` is dev-only,
so a prod fix is a full pre-flight-plus-install cycle.

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
- [Kubernetes — Pod Topology Spread Constraints](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/) — `defaultConstraints` and `defaultingType`
- [cluster-autoscaler FAQ — what types of pods can prevent scale-down](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#what-types-of-pods-can-prevent-ca-from-removing-a-node)
- [Redis — key eviction policies](https://redis.io/docs/latest/develop/reference/eviction/)
- [Bitnami PostgreSQL Chart](https://github.com/bitnami/charts/tree/main/bitnami/postgresql)
- [Bitnami Redis Chart](https://github.com/bitnami/charts/tree/main/bitnami/redis)
- [Apache Airflow Helm Chart](https://github.com/apache/airflow/tree/main/chart)
- [ARCHITECTURE.md](../ARCHITECTURE.md) — system architecture, env-var convention
- [TESTING.md](../TESTING.md) — testing conventions, dev-env lock protocol
- [SECRET_RESOLUTION.md](SECRET_RESOLUTION.md) — source-credential Secret model
- [BACKEND_LLM.md](BACKEND_LLM.md) — LLM observability + online key rotation
