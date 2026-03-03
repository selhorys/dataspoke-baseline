---
name: dev-env-install
description: Read `spec/feature/DEV_ENV.md` for the full development environment specification, then set up the local dev environment step by step.
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash(*), Read, Edit, Write, Glob, Grep, Skill(monitor-k8s)
---

## Step 1 — Configure `.env`

1. Read `dev_env/.env`. If it does not exist, create it from the template in `spec/feature/DEV_ENV.md` § Configuration.
2. If it already exists, read it and verify all required variables are present:
   - Dev variables: `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE`
   - Dev chart versions: `DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION`, `DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION`
   - Dev credentials: `DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_ROOT_PASSWORD`, `DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_PASSWORD`
   - Dev port-forwards: `DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT` through `..._TEMPORAL_PORT`
   - App runtime: `DATASPOKE_POSTGRES_HOST`, `DATASPOKE_POSTGRES_PORT`, `DATASPOKE_REDIS_HOST`, etc.
3. Generate secure passwords (16+ chars, mixed case, at least one special character) for any missing password variables.
4. **Show the final `.env` content to the user and ask for confirmation before writing.** Do not proceed until the user approves.

## Step 2 — Pre-flight checks

1. Verify `kubectl` and `helm` are installed.
2. Verify the Kubernetes cluster specified in `DATASPOKE_DEV_KUBE_CLUSTER` is reachable (`kubectl cluster-info`).
3. Report cluster node resources (`kubectl get nodes`) so the user can confirm the cluster meets the minimum requirements from `spec/feature/DEV_ENV.md` § Resource Sizing (8+ CPU / 16 GB RAM).
4. If any check fails, report clearly and stop.

## Step 3 — Run installation

1. Execute the top-level install script **in the background**: `bash dev_env/install.sh`
   - Note the background task ID and output file path.
2. While the script runs, **alternate between two monitoring sources every ~30 seconds**:
   a. **Script output**: read the background task output file (e.g., `tail -20 <output-file>`) to report install progress messages (namespace creation, Helm releases, pod readiness steps, job completions).
   b. **Cluster state**: invoke the `/monitor-k8s` skill to get live pod/Helm status across all namespaces.
   - After each round, summarize what changed since the last check (new pods Running, jobs Completed, etc.).
   - If a pod enters `CrashLoopBackOff`, `OOMKilled`, or `Error`, report it immediately and show recent logs.
3. Continue until the background script exits (exit code 0 = success, non-zero = failure) **and** all expected pods are `Running`/`Ready`.

## Step 4 — Verify

1. Confirm all expected components are running (per `spec/feature/DEV_ENV.md` § DataHub Installation, § DataSpoke Infrastructure Installation, and § dataspoke-example Installation).
2. Use the `k8s-helm` agent for any troubleshooting of Helm or Kubernetes issues encountered during installation.

## Step 5 — Report to user

1. Tell the user that **DataHub is ready**:
   - Show the utility script to port-forward: `./dev_env/datahub-port-forward.sh`
   - Show the URL: `http://localhost:9002`
   - Show the credentials: `datahub / datahub`
2. Tell the user that **DataSpoke infrastructure is ready** (PostgreSQL, Redis, Qdrant, Temporal):
   - Show the utility script to port-forward: `./dev_env/dataspoke-port-forward.sh`
   - PostgreSQL: `localhost:9201`
   - Redis: `localhost:9202`
   - Qdrant: `localhost:9203` (HTTP), `localhost:9204` (gRPC)
   - Temporal: `localhost:9205`
3. Tell the user that **dummy-data sources are ready** (PostgreSQL + Kafka):
   - PostgreSQL: `localhost:9102` — credentials `postgres / ExampleDev2024!`, database `example_db`
   - Kafka: `localhost:9104` — topic `example_topic`
4. Tell the user how to **run DataSpoke app services locally**:
   - `source dev_env/.env` to load environment variables
   - Frontend: `cd src/frontend && npm run dev` (http://localhost:3000)
   - API: `cd src/api && uvicorn main:app --reload --port 8000`
   - Workers: `cd src/workflows && python -m worker`
5. **Ask the user** if they want to start the port-forwarding processes now:
   - If yes for DataHub: run `./dev_env/datahub-port-forward.sh`
   - If yes for DataSpoke infra: run `./dev_env/dataspoke-port-forward.sh`
   - Both can be started independently.
