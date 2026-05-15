---
name: dev-env
description: Manage the kubernetes-based DataSpoke development environment — configure, install, reinstall, uninstall, health-check, and run-dataspoke-test-mode. Services are accessed via nginx-ingress (HTTP ingress for APIs/UIs, TCP passthrough for databases and message brokers).
disable-model-invocation: false
user-invocable: true
argument-hint: [configure|install|reinstall|uninstall|health-check|run-dataspoke-test-mode] [options...]
allowed-tools: Bash(*), Read, Edit, Write, Glob, Grep, Skill(k8s-work), AskUserQuestion
---

## Routing

Parse `$ARGUMENTS` and the user's request to determine the action. If ambiguous or no arguments given, ask the user which action they want:

| Action | Trigger keywords |
|--------|-----------------|
| **configure** | `configure`, `config`, `setup`, `env` |
| **install** | `install`, `up`, `create` |
| **health-check** | `health-check`, `health`, `check`, `status` |
| **run-dataspoke-test-mode** | `run-dataspoke-test-mode`, `run`, `start`, `deploy`, `test-mode` |
| **reinstall** | `reinstall`, `reset` |
| **uninstall** | `uninstall`, `teardown`, `down`, `remove`, `destroy` |

### Component names

When the user specifies components, match against these names. If no components are specified, operate on **all**.

| Component | Install script | Uninstall script |
|-----------|---------------|------------------|
| `nginx-ingress` | `dev_env/nginx-ingress/install.sh` | `dev_env/nginx-ingress/uninstall.sh` |
| `datahub` | `dev_env/datahub/install.sh` | `dev_env/datahub/uninstall.sh` |
| `langfuse` (aliases: `lf`, `observability`) | `dev_env/langfuse/install.sh` | `dev_env/langfuse/uninstall.sh` |
| `dataspoke-infra` (aliases: `infra`, `infrastructure`) | `dev_env/dataspoke-infra/install.sh` | `dev_env/dataspoke-infra/uninstall.sh` |
| `dataspoke-example` (aliases: `example`, `dummy-data`) | `dev_env/dataspoke-example/install.sh` | `dev_env/dataspoke-example/uninstall.sh` |
| `dataspoke-lock` (aliases: `lock`) | `dev_env/dataspoke-lock/install.sh` | `dev_env/dataspoke-lock/uninstall.sh` |

---

## Action: configure

1. Read `dev_env/.env`. If it does not exist, create it from `dev_env/.env.example`.
2. If it already exists, verify all required variables are present:
   - Dev variables: `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE`, `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE`
   - Dev chart versions: `DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION`, `DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION`
   - Dev credentials: `DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_ROOT_PASSWORD`, `DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_PASSWORD`
   - Dev ingress: `DATASPOKE_DEV_INGRESS_IP`, `DATASPOKE_DEV_INGRESS_DOMAIN`
   - Example service endpoints: `DATASPOKE_EXAMPLE_PG_HOST`, `DATASPOKE_EXAMPLE_PG_PORT`, `DATASPOKE_EXAMPLE_KAFKA_BROKERS`
   - App runtime: `DATASPOKE_POSTGRES_HOST`, `DATASPOKE_POSTGRES_PORT`, `DATASPOKE_REDIS_HOST`, etc.
3. Generate secure passwords (16+ chars, mixed case, at least one special character) for any missing password variables.
4. **Show the final `.env` content to the user and ask for confirmation before writing.** Do not proceed until the user approves. (Skip confirmation if `.env` already has all required variables.)

---

## Action: install

Run `configure` first if `dev_env/.env` does not exist or is missing required variables.

### Pre-flight checks

1. Verify `kubectl` and `helm` are installed.
2. Verify the Kubernetes cluster specified in `DATASPOKE_DEV_KUBE_CLUSTER` is reachable (`kubectl cluster-info`).
3. Report cluster node resources (`kubectl get nodes`) so the user can confirm the cluster meets the minimum requirements from `spec/feature/DEV_ENV.md` § Resource Budget (8+ CPU / 24 GB RAM).
4. If any check fails, report clearly and stop.

### Full install (all components)

1. Execute the top-level install script **in the background**: `bash dev_env/install.sh`
   - Note the background task ID and output file path.
2. While the script runs, **alternate between two monitoring sources every ~30 seconds**:
   a. **Script output**: read the background task output file (e.g., `tail -20 <output-file>`) to report install progress messages.
   b. **Cluster state**: invoke the `/k8s-work` skill to get live pod/Helm status across all namespaces.
   - After each round, summarize what changed since the last check.
   - If a pod enters `CrashLoopBackOff`, `OOMKilled`, or `Error`, report it immediately and show recent logs.
3. Continue until the background script exits (exit code 0 = success, non-zero = failure) **and** all expected pods are `Running`/`Ready`.

### Partial install (specific components)

1. Ensure namespaces exist (create if needed, same logic as `install.sh`).
2. **Resuming an interrupted full install** (starting component plus every component after it in dependency order): prefer `bash dev_env/install.sh --from-component <name>` — it inherits the orchestrator's step markers, error handling, and final summary.
3. **Installing one or a few specific components** out of sequence: run each component's install script directly in dependency order (nginx-ingress → datahub → langfuse → dataspoke-infra → dataspoke-example → dataspoke-lock): `bash dev_env/<component>/install.sh`.
4. Monitor with `/k8s-work` after each component completes.

### Post-install

1. Confirm all expected components are running.
2. Seed dummy data and register datasets in DataHub:
   ```bash
   uv run python -m tests.integration.util --reset-seed
   ```
3. Show access information (ingress endpoints table is in `dev_env/README.md §Ingress Endpoints`; substitute `DATASPOKE_DEV_INGRESS_IP` / `DATASPOKE_DEV_INGRESS_DOMAIN` from `dev_env/.env`).
4. Inform the user that `dev_env/.env` has been populated with ingress-derived runtime variables (hosts, URLs, ports) by `nginx-ingress/install.sh`, and that they should run `source dev_env/.env` to load them into their shell.
5. Note that the API is deployed in-cluster by `dataspoke-infra/install.sh` (via the umbrella Helm chart). To rebuild and redeploy after code changes, use `./dev_env/dataspoke-test-mode.sh`. Frontend (once `src/frontend/` is implemented — currently TBD) runs on the host: `cd src/frontend && npm run dev` (http://localhost:3000).

---

## Action: uninstall

### Show current state

1. Show what is currently deployed:
   - `helm list` across all dev_env namespaces
   - `kubectl get pods` in each namespace
   - `kubectl get pvc` in each namespace
2. **Ask the user to confirm** they want to remove resources before proceeding.

### Full uninstall (all components)

1. **Ask the user** whether to also delete the namespaces and their PVCs.
2. Execute the top-level uninstall script with flags:
   - Always pass `--yes` (user already confirmed).
   - If user wants namespace deletion, also pass `--delete-namespaces`: `bash dev_env/uninstall.sh --yes --delete-namespaces`
   - Otherwise: `bash dev_env/uninstall.sh --yes`
   - If the uninstall script does not exist or fails, fall back to manual teardown (run each component's uninstall.sh in reverse order, ending with `nginx-ingress/uninstall.sh`).
3. Clean up any orphaned PersistentVolumes in `Released` state.

### Partial uninstall (specific components)

1. For each requested component **in reverse dependency order** (dataspoke-lock → dataspoke-example → dataspoke-infra → langfuse → datahub → nginx-ingress), run: `bash dev_env/<component>/uninstall.sh`
2. Do NOT delete namespaces during partial uninstall.

### Post-uninstall

1. Confirm cleanup with `/k8s-work`.
2. Report the clean state.

---

## Action: reinstall

There is no dedicated `reinstall.sh`. Reinstall a component by running its `uninstall.sh` followed by its `install.sh` — both are idempotent and handle PVC/Helm-release teardown within their scope.

### Steps

1. Parse `$ARGUMENTS` to identify the target component (see the Component names table above). If no component is specified, ask the user which to reinstall.
2. Run the component's `uninstall.sh` in the foreground, then its `install.sh`.
   - Example for `dataspoke-infra`: `cd dev_env && bash dataspoke-infra/uninstall.sh && bash dataspoke-infra/install.sh`
3. Monitor output for errors. If teardown or rollout fails, report the error and suggest remediation.
4. On success, confirm the component is running and report access URLs.

---

## Action: health-check

1. Run `./dev_env/health-check.sh` and report the results.
2. Supported flags:
   - `--quick` — TCP-only checks (skip deep application-layer probes)
   - `--keep-lock` — don't touch an existing dev-env lock
   - `--force-release` — release a held lock without prompting
3. If any service is unhealthy, show the reinstall command from CLAUDE.md's "Integration Test Protocol" table.
4. For deeper cluster-level diagnostics, invoke the `/k8s-work` skill.

---

---

## Action: run-dataspoke-test-mode

Build a Docker image of the DataSpoke API, deploy it in-cluster via the umbrella Helm chart, and wait for the rollout. The API is accessible via nginx-ingress — no port-forward needed. `DATASPOKE_TEST_MODE=true` is baked into `values-dev.yaml` so Airflow callbacks reach the API via cluster DNS (`http://dataspoke-api:8002`).

### Pre-flight

1. Verify `dev_env/.env` exists. If not, run **configure** first.
2. If the user requests it (or `--health-check` flag), run `./dev_env/health-check.sh --quick` to confirm infrastructure is reachable. If it fails, suggest `/dev-env health-check` or `/dev-env install` and stop.
3. Run `uv sync` to ensure Python dependencies are up to date.

### Option parsing

Parse `$ARGUMENTS` and the user's request for these options:

| Option | CLI flag | Default | Description |
|--------|----------|---------|-------------|
| `skip-build` | `--skip-build` | off | Skip Docker build, deploy existing image only |
| `health-check` | `--health-check` | off | Run health check before deploying |
| `stop` | `--stop` | off | Scale down the API deployment and exit |

### Deploy

1. Run `./dev_env/dataspoke-test-mode.sh` with parsed flags in the foreground. The script builds the API image, calls `dataspoke-infra/install.sh` (which itself rebuilds the custom PostgreSQL and Airflow images unless `SKIP_POSTGRES_BUILD=1` / `SKIP_AIRFLOW_BUILD=1` are set), runs `helm upgrade`, restarts the API deployment, and verifies Airflow DAGs. First run can take 5–10 minutes due to image builds; subsequent rebuilds with `--skip-build` are 1–2 minutes.
2. Monitor the output for errors. If the build or rollout fails, report the error and suggest remediation.
3. On success, report the running state to the user:
   - API URL: `http://app.<INGRESS_DOMAIN>/api/v1/`
   - ReDoc UI: `http://app.<INGRESS_DOMAIN>/redoc`
   - Health: `http://app.<INGRESS_DOMAIN>/health`
   - How to run tests: `DATASPOKE_TEST_MODE=true uv run pytest tests/integration/spot/` (spot) or `… tests/integration/api_wired/` (UC user stories) — run in separate invocations
   - How to stop: `./dev_env/dataspoke-test-mode.sh --stop`

### Stop

If the user asks to stop:

1. Run `./dev_env/dataspoke-test-mode.sh --stop` (scales down the `dataspoke-api` deployment to 0 replicas).
2. Confirm the deployment has been scaled down.
