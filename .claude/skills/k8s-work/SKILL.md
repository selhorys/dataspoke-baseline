---
name: k8s-work
description: Manage the Kubernetes development cluster — health checks, continuous monitoring, kubectl/helm operations, and troubleshooting.
argument-hint: "[status|monitor|<kubectl/helm operation>]"
context: fork
agent: general-purpose
allowed-tools: Bash(kubectl *), Bash(helm *), Bash(minikube *), Bash(sleep *), Bash(date *), Read
---

## Setup

1. **Read cluster config**: Read `helm-charts/.env` to get:
   - `DATASPOKE_KUBE_CLUSTER` — kube context (e.g., `docker-desktop`)
   - `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` — DataHub namespace (e.g., `datahub-01`)
   - `DATASPOKE_KUBE_DATASPOKE_NAMESPACE` — DataSpoke namespace (e.g., `dataspoke-01`)
   - `DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE` — Example sources namespace (e.g., `dataspoke-dummy-data-01`)

   **Use these variable values in all kubectl/helm commands.** Do NOT hardcode namespace names.

2. **Verify prerequisites**:
```bash
kubectl version --client
kubectl config current-context   # confirm correct context
kubectl get nodes                 # confirm cluster access
```

If tools are missing or cluster is unreachable, stop and inform the user.

---

## Routing

Parse `$ARGUMENTS` and the user's request to determine the action:

| Action | Trigger keywords |
|--------|-----------------|
| **status** | `status`, `health`, `check` — or no arguments |
| **monitor** | `monitor`, `watch`, `poll`, `track` |
| **operation** | Any kubectl/helm command or resource management request |

---

## Action: status — One-time Health Check

Run a full cluster health snapshot and produce a formatted report.

### Data collection

Substitute `$NS_DH`, `$NS_DS`, `$NS_EX` with the namespace names from `.env`. Collect, per namespace where applicable:

- Nodes: `kubectl get nodes` (name, status, CPU, memory); `kubectl top nodes` if metrics-server present.
- Pods: `kubectl get pods -n <ns> -o wide` for all three namespaces (tolerate empty/missing namespaces).
- PVCs: `kubectl get pvc -n <ns>`.
- Resource usage: `kubectl top pods -n <ns> --sort-by=cpu`.
- Helm releases: `helm list -n <ns>`; plus `helm list --all-namespaces --failed`.
- Warning events: `kubectl get events -n <ns> --field-selector type=Warning --sort-by='.lastTimestamp'` (last ~20).

If `$ARGUMENTS` specifies a **focus area** (pod, release, component), run the troubleshooting workflow in [troubleshooting.md](troubleshooting.md): describe the resource, tail logs (last 100 lines), and for Helm releases show history + values.

### Report format

Produce a Markdown report titled `## Cluster Health Report — <timestamp>` with the current kube context, then sections for Nodes, each namespace's Pods, Helm Releases, Warnings, Resource Pressure, and a final one-line Summary (✅ / ⚠️ / ❌ with brief notes and suggested next steps). Use tables for tabular data (nodes, pods, releases).

---

## Action: monitor — Continuous Polling

Use this during installations, upgrades, or any cluster modification. When any pod is not fully ready (e.g., `0/1 Running`, `Init`, `Pending`, `CrashLoopBackOff`) or any Helm release is in `pending-install`/`pending-upgrade`, poll repeatedly. Do NOT just collect a one-time snapshot and return.

**Polling procedure** — repeat up to 15 iterations (total ~5 minutes):

a. `sleep 20` (wait 20 seconds between checks)
b. `kubectl get pods -n $NS_DH` to get current pod status (repeat for `$NS_DS` and `$NS_EX`)
c. For each pod that is NOT ready (Ready != True, or status != Running/Completed):
   - `kubectl logs <pod-name> -n <namespace> --tail=15` to see latest log output
   - If pod is in `Pending` or `Init*`, run `kubectl describe pod <pod-name> -n <namespace> | tail -20` for events
d. `helm list -n $NS_DH --all` to check release status
e. Note the progress since last check (e.g., "system-update: now loading plugins...", "GMS: readiness probe passing")

**Stop conditions** — exit the loop early when ANY of these are true:
- All running pods show `Ready` and all jobs show `Completed`
- A pod enters `CrashLoopBackOff` with 3+ restarts (report the error and stop)
- A pod is stuck in `Error` or `OOMKilled` (report and stop)

**IMPORTANT**: Each iteration must sleep ≤25 seconds and make at least one `kubectl` call — the user expects incremental progress updates, not a single final report.

After monitoring completes, output the full health report (same format as the status action).

---

## Action: operation — kubectl/helm Execution

For any kubectl or helm operation requested by the user.

### Execution strategy

Classify the operation before acting:

- **Read** (safe, execute immediately): `kubectl get/describe/logs/events`, `helm status/history/get values`.
- **Create/apply**: run `kubectl apply --dry-run=server` first, then apply.
- **Modify** (restart, upgrade, scale): confirm intent with the user, then execute.
- **Delete**: confirm context (`kubectl config current-context`), describe the resource, delete, verify. **Never delete namespaces.** Confirm with the user before any namespace-level or scale-to-zero destructive operation.

### Report results

After every operation, summarize:
- What was executed
- What changed (before/after state if relevant)
- Any warnings, errors, or events triggered
- Suggested next steps if issues are found

For errors, consult [reference.md](reference.md) for common causes and resolutions.

---

See [reference.md](reference.md) for helm chart management, resource creation patterns, capacity planning, and error reference.
See [troubleshooting.md](troubleshooting.md) for deep-dive workflows and error reference.
