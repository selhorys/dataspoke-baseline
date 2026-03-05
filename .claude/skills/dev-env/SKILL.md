---
name: dev-env
description: Manage the kubernetes-based DataSpoke development environment — install, uninstall, port-forward, and check status.
disable-model-invocation: true
user-invocable: true
argument-hint: [install|uninstall|port-forward|status] [component...]
allowed-tools: Bash(*), Read, Edit, Write, Glob, Grep, Skill(k8s-work), AskUserQuestion
---

## Routing

Parse `$ARGUMENTS` and the user's request to determine the action. If ambiguous or no arguments given, ask the user which action they want:

| Action | Trigger keywords |
|--------|-----------------|
| **install** | `install`, `setup`, `up`, `create` |
| **uninstall** | `uninstall`, `teardown`, `down`, `remove`, `destroy` |
| **port-forward** | `port-forward`, `forward`, `pf`, `ports` |
| **status** | `status`, `check`, `health`, `monitor` |

### Component names

When the user specifies components, match against these names. If no components are specified, operate on **all**.

| Component | Install script | Uninstall script | Port-forward script |
|-----------|---------------|------------------|-------------------|
| `datahub` | `dev_env/datahub/install.sh` | `dev_env/datahub/uninstall.sh` | `dev_env/datahub-port-forward.sh` |
| `dataspoke-infra` (aliases: `infra`, `infrastructure`) | `dev_env/dataspoke-infra/install.sh` | `dev_env/dataspoke-infra/uninstall.sh` | `dev_env/dataspoke-port-forward.sh` |
| `dataspoke-example` (aliases: `example`, `dummy-data`) | `dev_env/dataspoke-example/install.sh` | `dev_env/dataspoke-example/uninstall.sh` | `dev_env/dummy-data-port-forward.sh` |
| `dataspoke-lock` (aliases: `lock`) | `dev_env/dataspoke-lock/install.sh` | `dev_env/dataspoke-lock/uninstall.sh` | `dev_env/lock-port-forward.sh` |

---

## Shared — Load configuration

All actions start here:

1. Read `dev_env/.env`. If it does not exist, create it from the template in `spec/feature/DEV_ENV.md` § Configuration.
2. If it already exists, verify all required variables are present:
   - Dev variables: `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE`
   - Dev chart versions: `DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION`, `DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION`
   - Dev credentials: `DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_ROOT_PASSWORD`, `DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_PASSWORD`
   - Dev port-forwards: `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT` through `..._TEMPORAL_PORT`
   - App runtime: `DATASPOKE_POSTGRES_HOST`, `DATASPOKE_POSTGRES_PORT`, `DATASPOKE_REDIS_HOST`, etc.
3. Generate secure passwords (16+ chars, mixed case, at least one special character) for any missing password variables.
4. **Show the final `.env` content to the user and ask for confirmation before writing.** Do not proceed until the user approves. (Skip confirmation if `.env` already has all required variables.)

---

## Action: install

### Pre-flight checks

1. Verify `kubectl` and `helm` are installed.
2. Verify the Kubernetes cluster specified in `DATASPOKE_DEV_KUBE_CLUSTER` is reachable (`kubectl cluster-info`).
3. Report cluster node resources (`kubectl get nodes`) so the user can confirm the cluster meets the minimum requirements from `spec/feature/DEV_ENV.md` § Resource Sizing (8+ CPU / 16 GB RAM).
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
2. For each requested component **in dependency order** (datahub → dataspoke-infra → dataspoke-example → dataspoke-lock), run the component's install script directly: `bash dev_env/<component>/install.sh`
3. Monitor with `/k8s-work` after each component completes.

### Post-install

1. Confirm all expected components are running.
2. Show access information:
   - **DataHub**: `./dev_env/datahub-port-forward.sh` → UI at `http://localhost:9002`, credentials `datahub / datahub`
   - **DataSpoke infra**: `./dev_env/dataspoke-port-forward.sh` → PostgreSQL `:9201`, Redis `:9202`, Qdrant `:9203`/`:9204`, Temporal `:9205`
   - **Dummy data**: `./dev_env/dummy-data-port-forward.sh` → PostgreSQL `:9102`, Kafka `:9104`
   - **Lock service**: `./dev_env/lock-port-forward.sh` → API `:9221`
3. Show how to run DataSpoke app services on the host:
   - `source dev_env/.env`
   - Frontend: `cd src/frontend && npm run dev` (http://localhost:3000)
   - API: `cd src/api && uvicorn main:app --reload --port 8000`
   - Workers: `cd src/workflows && python -m worker`
4. **Ask the user** if they want to start port-forwarding now.

---

## Action: uninstall

### Show current state

1. Show what is currently deployed:
   - `helm list` across all dev_env namespaces
   - `kubectl get pods` in each namespace
   - `kubectl get pvc` in each namespace
2. **Ask the user to confirm** they want to remove resources before proceeding.

### Stop port-forwarding

1. Check if port-forwarding processes are running by looking for PID files: `dev_env/.datahub-port-forward.pid`, `dev_env/.dataspoke-port-forward.pid`, `dev_env/.dummy-data-port-forward.pid`, `dev_env/.lock-port-forward.pid`.
2. Stop any running port-forwards for the components being uninstalled.

### Full uninstall (all components)

1. **Ask the user** whether to also delete the namespaces and their PVCs.
2. Execute the top-level uninstall script with flags:
   - Always pass `--yes` (user already confirmed).
   - If user wants namespace deletion, also pass `--delete-namespaces`: `bash dev_env/uninstall.sh --yes --delete-namespaces`
   - Otherwise: `bash dev_env/uninstall.sh --yes`
   - If the uninstall script does not exist or fails, fall back to manual teardown (run each component's uninstall.sh in reverse order).
3. Clean up any orphaned PersistentVolumes in `Released` state.

### Partial uninstall (specific components)

1. For each requested component **in reverse dependency order** (dataspoke-lock → dataspoke-example → dataspoke-infra → datahub), run: `bash dev_env/<component>/uninstall.sh`
2. Do NOT delete namespaces during partial uninstall.

### Post-uninstall

1. Confirm cleanup with `/k8s-work`.
2. Report the clean state.

---

## Action: port-forward

### Start

1. For each requested component (or all if none specified), run the port-forward script:
   - `./dev_env/datahub-port-forward.sh`
   - `./dev_env/dataspoke-port-forward.sh`
   - `./dev_env/dummy-data-port-forward.sh`
   - `./dev_env/lock-port-forward.sh`
2. After starting, verify the PID files were created and report the forwarded ports.

### Stop

If the user asks to stop port-forwarding:

1. For each requested component (or all if none specified), run with `--stop`:
   - `./dev_env/datahub-port-forward.sh --stop`
   - `./dev_env/dataspoke-port-forward.sh --stop`
   - `./dev_env/dummy-data-port-forward.sh --stop`
   - `./dev_env/lock-port-forward.sh --stop`
2. Confirm the PID files are cleaned up.

---

## Action: status

Invoke the `/k8s-work` skill, passing along any focus area from the user's request.
