---
name: dev-env-uninstall
description: Tear down the local DataSpoke development environment.
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash(*), Read, Glob, Grep, Skill(monitor-k8s)
---

## Step 1 — Load configuration

1. Read `dev_env/.env` to get namespace names and cluster context.
2. If `.env` does not exist, ask the user for the cluster context and namespace names.

## Step 2 — Stop port-forwarding

1. Check if port-forwarding processes are running by looking for PID files: `dev_env/.datahub-port-forward.pid`, `dev_env/.dataspoke-port-forward.pid`, and `dev_env/.dummy-data-port-forward.pid`.
2. If any are running, **ask the user** to confirm stopping them before proceeding.
3. If confirmed, stop them:
   - `./dev_env/datahub-port-forward.sh --stop`
   - `./dev_env/dataspoke-port-forward.sh --stop`
   - `./dev_env/dummy-data-port-forward.sh --stop` (if exists)

## Step 3 — Show current state

1. Show what is currently deployed:
   - `helm list` across all dev_env namespaces (`$DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`, `$DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`)
   - `kubectl get all` in each namespace
   - `kubectl get pvc` in each namespace
2. **Ask the user to confirm** they want to remove all dev_env resources before proceeding.

## Step 4 — Uninstall

1. **Ask the user** whether to also delete the namespaces and their PVCs (in addition to removing Helm releases and workloads).
2. Execute the top-level uninstall script with flags based on the user's answer:
   - Always pass `--yes` (user already confirmed in Step 3).
   - If user wants namespace deletion, also pass `--delete-namespaces`: `bash dev_env/uninstall.sh --yes --delete-namespaces`
   - Otherwise: `bash dev_env/uninstall.sh --yes`
   - If the uninstall script does not exist or fails, fall back to manual teardown:
     a. Run `dev_env/dataspoke-example/uninstall.sh` (or `kubectl delete -f dev_env/dataspoke-example/manifests/`)
     b. Run `dev_env/dataspoke-infra/uninstall.sh` (or `helm uninstall dataspoke -n $DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE`)
     c. Run `dev_env/datahub/uninstall.sh` (or `helm uninstall` the datahub and datahub-prerequisites releases)
3. Clean up any orphaned PersistentVolumes in `Released` state that were bound to dev_env PVCs.

## Step 5 — Verify

1. Confirm namespaces are gone (or cleaned, if user chose to keep them).
2. Confirm no orphaned PVs remain.
3. Use `/monitor-k8s` to do a final cluster health check and report the clean state.
