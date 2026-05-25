---
name: k8s-deploy
description: Drive the helm-charts/bin/ install/uninstall/build/health scripts for both dev and prod profiles — configure, install, reinstall, uninstall, health-check, and rebuild-the-API. The dev profile installs umbrella chart + peripherals (nginx-ingress, DataHub, Langfuse, dummy data, dev-lock) and auto-seeds peripheral connection config via the admin API. The prod profile installs the umbrella chart only; operator wires peripherals via /api/v1/admin/peripherals/*.
disable-model-invocation: false
user-invocable: true
argument-hint: [configure|install|reinstall|uninstall|health-check|run-api] [--profile dev|prod] [--components <csv>] [other options...]
allowed-tools: Bash(*), Read, Edit, Write, Glob, Grep, Skill(k8s-work), AskUserQuestion
---

## Routing

Parse `$ARGUMENTS` and the user's request to determine the action. If ambiguous or no arguments given, ask the user which action they want:

| Action | Trigger keywords |
|--------|-----------------|
| **configure** | `configure`, `config`, `setup`, `env` |
| **install** | `install`, `up`, `create` |
| **health-check** | `health-check`, `health`, `check`, `status` |
| **run-api** | `run-api`, `run`, `start`, `deploy`, `test-mode`, `iterate` |
| **reinstall** | `reinstall`, `reset` |
| **uninstall** | `uninstall`, `teardown`, `down`, `remove`, `destroy` |

### Profile

Every action requires a profile. Parse `--profile dev` or `--profile prod`
from `$ARGUMENTS`. If absent, ask the user — do not default. The dev
profile installs peripherals and seeds; the prod profile is umbrella-chart
only.

### Component names

When the user specifies components, match against these names (the
`--components` flag accepts a comma-separated subset). If no components
are specified, operate on **all-for-profile**.

| Component | Profiles | Aliases |
|---|---|---|
| `nginx-ingress` | dev | `ingress` |
| `datahub` | dev | — |
| `langfuse` | dev | `lf`, `observability` |
| `dataspoke-infra` | dev, prod | `infra`, `infrastructure`, `chart`, `umbrella` |
| `api` | dev, prod | (rebuild + helm-upgrade the API only — iteration path) |
| `dummy-data` | dev | `example`, `dummy` |
| `dev-lock` | dev | `lock` |
| `seed` | dev | (post-install admin-API seeding only) |

---

## Action: configure

1. Read `helm-charts/.env`. If it does not exist, create it from `helm-charts/.env.example`.
2. If it already exists, verify the canonical variables are present per spec `spec/feature/HELM_CHART.md §Configuration — Four-Tier Env Vars`:
   - **Kube deployment** (both profiles): `DATASPOKE_KUBE_CLUSTER`, `DATASPOKE_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_KUBE_IMAGE_REGISTRY`, `DATASPOKE_KUBE_CLOUD_VENDOR`, `DATASPOKE_KUBE_INGRESS_IP` (auto-populated in dev), `DATASPOKE_KUBE_INGRESS_DOMAIN` (auto-populated in dev).
   - **App runtime** (`DATASPOKE_*`): not in `.env` — injected into pods from the `dataspoke-secrets` K8s Secret via `envFrom`. In dev, `install.sh` auto-generates the Secret; in prod, the operator pre-creates it. `DATASPOKE_CORS_ORIGINS` is rendered from chart values (`config.corsOrigins`).
   - **Dev only** (dev profile): `DATASPOKE_DEV_KUBE_{DATAHUB,LANGFUSE,DUMMY_DATA}_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATAHUB_{,PREREQUISITES_}CHART_VERSION`, `DATASPOKE_DEV_DATAHUB_MYSQL_{ROOT_,}PASSWORD`, `DATASPOKE_DEV_DUMMY_DATA_{KAFKA_INSTANCE,POSTGRES_USER,POSTGRES_PASSWORD,POSTGRES_DB}`, `DATASPOKE_DEV_LLM_{PROVIDER,API_KEY,MODEL}`. The Langfuse internals and peripheral connection outputs are auto-populated by the peripheral install scripts.
   - **Test access** (`DATASPOKE_TEST_*`): auto-populated by `install.sh` post-install via `_sync_env_from_secret`; never manually edited. Read by `tests/integration/` for laptop-side cluster access.
3. **Do NOT** add `DATASPOKE_ENABLE_STUB_AUTH` to `.env` — it's a chart value only (`api.enableStubAuth`). Stub-mode toggles for the four dependency factories live in the `runtime_config` DB row (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`) — flippable via `PATCH /api/v1/admin/conf`, not in `.env`.
4. Generate secure passwords (16+ chars, mixed case, at least one special character) for any missing password variables.
5. **Show the final `.env` content to the user and ask for confirmation before writing.** Do not proceed until the user approves. (Skip confirmation if `.env` already has all required variables.)

---

## Action: install

Run `configure` first if `helm-charts/.env` does not exist or is missing required variables.

### Pre-flight checks

1. Verify `kubectl` and `helm` are installed.
2. Verify the Kubernetes cluster specified in `DATASPOKE_KUBE_CLUSTER` is reachable (`kubectl cluster-info`).
3. Report cluster node resources (`kubectl get nodes`) so the user can confirm the cluster meets the minimum requirements from `spec/feature/HELM_CHART.md §Resource Sizing` (8+ CPU / 24 GB RAM for the full dev profile).
4. If any check fails, report clearly and stop.

### Full install (all components for the profile)

1. Execute the top-level install script **in the background**:
   - dev: `./helm-charts/bin/install.sh --profile dev`
   - prod: `./helm-charts/bin/install.sh --profile prod --values <operator-overlay>` (ask the user for the overlay path)
   - Note the background task ID and output file path.
2. While the script runs, **alternate between two monitoring sources every ~30 seconds**:
   a. **Script output**: read the background task output file (e.g., `tail -20 <output-file>`) to report install progress messages.
   b. **Cluster state**: invoke the `/k8s-work` skill to get live pod/Helm status across all namespaces.
   - After each round, summarize what changed since the last check.
   - If a pod enters `CrashLoopBackOff`, `OOMKilled`, or `Error`, report it immediately and show recent logs.
3. Continue until the background script exits (exit code 0 = success, non-zero = failure) **and** all expected pods are `Running`/`Ready`.

### Partial install (specific components)

1. **Resuming an interrupted full install** (starting component plus every component after it in dependency order): prefer `./helm-charts/bin/install.sh --profile dev --from-component <name>` — it inherits the orchestrator's step markers, error handling, and final summary.
2. **Installing one or a few specific components**: `./helm-charts/bin/install.sh --profile dev --components <csv>`. Honors phase ordering automatically.
3. **Rebuild and redeploy the API only** (code-iteration path): `./helm-charts/bin/install.sh --profile dev --components api`. This rebuilds the API image, runs helm upgrade, and rolls the deployment — replaces the previous standalone `dataspoke-test-mode.sh` workflow.
4. Monitor with `/k8s-work` after each component completes.

### Post-install

1. Confirm all expected components are running.
2. Seed dummy data and register datasets in DataHub:
   ```bash
   uv run python -m tests.integration.util --reset-seed
   ```
3. Show access information (ingress endpoints table is in `helm-charts/README.md §Ingress Endpoints`; substitute `DATASPOKE_KUBE_INGRESS_IP` / `DATASPOKE_KUBE_INGRESS_DOMAIN` from `helm-charts/.env`).
4. Inform the user that `helm-charts/.env` has been populated with ingress-derived runtime variables (hosts, URLs, ports) by `peripherals/nginx-ingress.sh`, and that they should run `source helm-charts/.env` to load them into their shell.
5. The post-install seeding step (dev profile) has already wired DataHub + Langfuse + LLM provider/model into the API's runtime config via `/internal/admin/peripherals/*` and `/internal/admin/conf` — no manual admin-API calls needed unless `--skip-seed` was set.

---

## Action: uninstall

### Show current state

1. Show what is currently deployed:
   - `helm list --all-namespaces`
   - `kubectl get pods -n <namespace>` for each namespace from `.env`
   - `kubectl get pvc -n <namespace>`
2. **Ask the user to confirm** they want to remove resources before proceeding.

### Full uninstall

1. **Ask the user** whether to also delete namespaces and their PVCs.
2. Execute the uninstall script with flags:
   - Always pass `--yes` (user already confirmed).
   - If user wants namespace deletion, also pass `--delete-namespaces`:
     `./helm-charts/bin/uninstall.sh --profile dev --yes --delete-namespaces`
   - Otherwise: `./helm-charts/bin/uninstall.sh --profile dev --yes`
3. Clean up any orphaned PersistentVolumes in `Released` state.

### Partial uninstall (specific components)

`uninstall.sh` does not support `--components` — it always tears down the
full profile. For a single-component teardown, delete the component's Helm
release (or manifests) directly with `helm uninstall <release> -n <ns>` or
`kubectl delete -f ...`, then reinstall via `install.sh --components <csv>`.
Do NOT delete namespaces during partial uninstall.

### Post-uninstall

1. Confirm cleanup with `/k8s-work`.
2. Report the clean state.

---

## Action: reinstall

There is no dedicated `reinstall.sh`, and `uninstall.sh` does not support `--components`. Reinstall by deleting the target component's Helm release / manifests directly, then re-running `install.sh --components <name>` — `install.sh` is idempotent and `helm upgrade --install` handles re-creation.

### Steps

1. Parse `$ARGUMENTS` to identify the target component (see the Component names table above). If no component is specified, ask the user which to reinstall.
2. Delete the component's existing release (look up the release/manifest set first — e.g. `helm list -A` or check the peripheral script for the manifest source), then re-run install:
   ```bash
   # Example: reinstall the dataspoke umbrella chart
   helm uninstall dataspoke -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
   ./helm-charts/bin/install.sh --profile dev --components dataspoke-infra
   ```
   For peripherals managed by plain manifests (dev-lock, dummy-data), `kubectl delete -f helm-charts/peripherals/<name>/manifests/` before reinstalling.
3. Monitor output for errors. If teardown or rollout fails, report the error and suggest remediation.
4. On success, confirm the component is running and report access URLs.

---

## Action: health-check

1. Run `./helm-charts/bin/health-check.sh` and report the results.
2. Supported flags:
   - `--quick` — TCP-only checks (skip deep application-layer probes)
   - `--keep-lock` — don't touch an existing dev-env lock
   - `--force-release` — release a held lock without prompting
3. If any service is unhealthy, show the reinstall command:
   `./helm-charts/bin/install.sh --profile dev --components <name>` (component names per the table above).
4. For deeper cluster-level diagnostics, invoke the `/k8s-work` skill.

---

## Action: run-api

Rebuild the DataSpoke API Docker image, redeploy it via `helm upgrade`, and roll the API deployment. This is the **code-iteration path**. The API is accessible via nginx-ingress — no port-forward needed. Airflow callbacks reach it via cluster DNS (`http://dataspoke-api:8002`). Stub-mode toggles are in `runtime_config` (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`); the dev-profile install seeds them all to `true` post-install, flippable via `PATCH /api/v1/admin/conf`.

### Pre-flight

1. Verify `helm-charts/.env` exists. If not, run **configure** first.
2. If the user requests it (or `--health-check` flag), run `./helm-charts/bin/health-check.sh --quick` to confirm infrastructure is reachable. If it fails, suggest `/k8s-deploy health-check` or `/k8s-deploy install` and stop.
3. Run `uv sync` to ensure Python dependencies are up to date.

### Option parsing

Parse `$ARGUMENTS` and the user's request for these options:

| Option | CLI flag | Default | Description |
|--------|----------|---------|-------------|
| `skip-build` | `--skip-build` | off | Skip Docker build, deploy existing image only |
| `image-tag` | `--image-tag <tag>` | `dev` | Override the image tag (CI-built images) |
| `health-check` | (runs `./helm-charts/bin/health-check.sh --quick` first) | off | Pre-flight infrastructure check |
| `stop` | (no flag — see Stop section) | off | Scale down the API deployment |

### Deploy

1. Run `./helm-charts/bin/install.sh --profile dev --components api` with parsed flags in the foreground. The script builds the API image (via `helm-charts/bin/build-image.sh api`), runs `helm upgrade --install`, and rolls the API deployment to pick up the new `:dev` image. First run can take 5–10 minutes due to image builds; `--skip-build` rebuilds are 1–2 minutes.
2. Monitor the output for errors. If the build or rollout fails, report the error and suggest remediation.
3. On success, report the running state to the user:
   - API URL: `http://app.${DATASPOKE_KUBE_INGRESS_DOMAIN}/api/v1/`
   - ReDoc UI: `http://app.${DATASPOKE_KUBE_INGRESS_DOMAIN}/redoc`
   - Health: `http://app.${DATASPOKE_KUBE_INGRESS_DOMAIN}/health`
   - How to run tests: `set -a && source helm-charts/.env && set +a && uv run pytest tests/integration/spot/` (spot) or `… tests/integration/api_wired/` (UC user stories) — run in separate invocations. The conftest `runtime_conf` fixture verifies the API has `stub_redis_client / stub_pgvector_manager / stub_notification_service` all `true` before the suite runs.
   - How to stop: `kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"`

### Stop

If the user asks to stop:

1. Run `kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"`.
2. Confirm the deployment has been scaled down.
