---
name: dev-env
description: Manage the kubernetes-based DataSpoke development environment — configure, install, health-check, run-dataspoke-test-mode, and uninstall. Services are accessed via nginx-ingress (HTTP ingress for APIs/UIs, TCP passthrough for databases and message brokers).
disable-model-invocation: false
user-invocable: true
argument-hint: [configure|install|uninstall|health-check|run-dataspoke-test-mode] [options...]
allowed-tools: Bash(*), Read, Edit, Write, Glob, Grep, Skill(k8s-work), AskUserQuestion
---

## Routing

Parse `$ARGUMENTS` and the user's request to determine the action. If ambiguous or no arguments given, ask the user which action they want:

| Action | Trigger keywords |
|--------|-----------------|
| **configure** | `configure`, `config`, `setup`, `env` |
| **install** | `install`, `up`, `create` |
| **health-check** | `health-check`, `health`, `check`, `status`, `monitor` |
| **run-dataspoke-test-mode** | `run-dataspoke-test-mode`, `run`, `start`, `host-mode`, `backend-only` |
| **uninstall** | `uninstall`, `teardown`, `down`, `remove`, `destroy` |

### Component names

When the user specifies components, match against these names. If no components are specified, operate on **all**.

| Component | Install script | Uninstall script |
|-----------|---------------|------------------|
| `nginx-ingress` | `dev_env/nginx-ingress/install.sh` | `dev_env/nginx-ingress/uninstall.sh` |
| `datahub` | `dev_env/datahub/install.sh` | `dev_env/datahub/uninstall.sh` |
| `dataspoke-infra` (aliases: `infra`, `infrastructure`) | `dev_env/dataspoke-infra/install.sh` | `dev_env/dataspoke-infra/uninstall.sh` |
| `dataspoke-example` (aliases: `example`, `dummy-data`) | `dev_env/dataspoke-example/install.sh` | `dev_env/dataspoke-example/uninstall.sh` |
| `dataspoke-lock` (aliases: `lock`) | `dev_env/dataspoke-lock/install.sh` | `dev_env/dataspoke-lock/uninstall.sh` |

---

## Action: configure

1. Read `dev_env/.env`. If it does not exist, create it from `dev_env/.env.example`.
2. If it already exists, verify all required variables are present:
   - Dev variables: `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE`
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
2. For each requested component **in dependency order** (nginx-ingress → datahub → dataspoke-infra → dataspoke-example → dataspoke-lock), run the component's install script directly: `bash dev_env/<component>/install.sh`
3. Monitor with `/k8s-work` after each component completes.

### Post-install

1. Confirm all expected components are running.
2. Seed dummy data and register datasets in DataHub:
   ```bash
   uv run python -m tests.integration.util --reset-all
   ```
3. Show access information (read `DATASPOKE_DEV_INGRESS_IP` and `DATASPOKE_DEV_INGRESS_DOMAIN` from `dev_env/.env`):

   **Tier A — HTTP ingress (nginx virtual hosts)**:
   | Service | URL |
   |---------|-----|
   | DataSpoke UI + API | `http://app.<DOMAIN>/` and `http://app.<DOMAIN>/api/v1/…` |
   | DataHub UI + GMS | `http://datahub.<DOMAIN>/` and `http://datahub.<DOMAIN>/gms/…` |
   | Kestra UI | `http://kestra.<DOMAIN>/` |

   **Tier B — TCP passthrough (direct IP:port)**:
   | Service | Address |
   |---------|---------|
   | DataSpoke PostgreSQL | `<INGRESS_IP>:9201` |
   | DataSpoke Redis | `<INGRESS_IP>:9202` |
   | Qdrant HTTP | `<INGRESS_IP>:9203` |
   | Qdrant gRPC | `<INGRESS_IP>:9204` |
   | DataHub Kafka | `<INGRESS_IP>:9005` |
   | Example PostgreSQL | `<INGRESS_IP>:9102` |
   | Example Kafka | `<INGRESS_IP>:9104` |
   | Lock service | `<INGRESS_IP>:9221` |

4. Inform the user that `dev_env/.env` has been populated with ingress-derived runtime variables (hosts, URLs, ports) by `nginx-ingress/install.sh`, and that they should run `source dev_env/.env` to load them into their shell.
5. Show how to run DataSpoke app services on the host:
   - `source dev_env/.env`
   - Frontend: `cd src/frontend && npm run dev` (http://localhost:3000)
   - API: `uv run uvicorn src.api.main:app --reload --port 8000`
   - Workers: `uv run python -m src.workflows.worker`

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

1. For each requested component **in reverse dependency order** (dataspoke-lock → dataspoke-example → dataspoke-infra → datahub → nginx-ingress), run: `bash dev_env/<component>/uninstall.sh`
2. Do NOT delete namespaces during partial uninstall.

### Post-uninstall

1. Confirm cleanup with `/k8s-work`.
2. Report the clean state.

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

## Action: run-dataspoke-test-mode

Run DataSpoke application services on the host in test/development mode, connecting to infrastructure accessible via nginx-ingress.

### Pre-flight

1. Verify `dev_env/.env` exists. If not, run **configure** first.
2. Run `./dev_env/health-check.sh --quick` to confirm infrastructure is reachable. If it fails, suggest running `/dev-env health-check` or `/dev-env install` and stop.
3. Run `uv sync` to ensure Python dependencies are up to date.

### Option parsing

Parse `$ARGUMENTS` and the user's request for these options:

| Option | CLI flag | Default | Description |
|--------|----------|---------|-------------|
| `backend-only` | `--backend-only` | off | Start only backend (API); skip frontend when it exists |
| `skip-migrate` | `--skip-migrate` | off | Skip Alembic database migration on startup |
| `port <N>` | `--port <N>` | `8000` | API listen port |
| `no-reload` | `--no-reload` | off | Disable uvicorn auto-reload (hot-reloading) |
| `env-file <path>` | `--env-file <path>` | `dev_env/.env` | Path to `.env` file |

### Start

1. Build the command from parsed options:
   ```bash
   uv run -m src.cli [--backend-only] [--skip-migrate] [--port N] [--no-reload] [--env-file PATH]
   ```
2. Run the command **in the background** so the conversation remains interactive.
3. Wait a few seconds, then read the background task output to confirm the banner appeared and the server started successfully.
4. Report the running state to the user:
   - API URL (`http://localhost:<port>`)
   - ReDoc UI (`http://localhost:<port>/redoc`)
   - Components started
   - How to stop: user can press Ctrl+C or ask to stop

### Stop

If the user asks to stop the running DataSpoke process:

1. Find the background task and stop it.
2. Confirm the process has exited.
