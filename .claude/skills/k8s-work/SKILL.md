---
name: k8s-work
description: Manage the Kubernetes development cluster — health checks, continuous monitoring, kubectl/helm operations, and troubleshooting.
argument-hint: [status|monitor|<kubectl/helm operation>]
disable-model-invocation: false
context: fork
agent: general-purpose
allowed-tools: Bash(kubectl *), Bash(helm *), Bash(minikube *), Bash(sleep *), Bash(date *), Read
---

## Setup

1. **Read cluster config**: Read `dev_env/.env` to get:
   - `DATASPOKE_DEV_KUBE_CLUSTER` — kube context (e.g., `docker-desktop`)
   - `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` — DataHub namespace (e.g., `datahub-01`)
   - `DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE` — DataSpoke namespace (e.g., `dataspoke-01`)
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

Substitute `$NS_DH`, `$NS_DS`, `$NS_EX` with the namespace names from `.env`:

```bash
# Node health
kubectl get nodes -o custom-columns=\
NAME:.metadata.name,\
STATUS:.status.conditions[-1].type,\
CPU:.status.capacity.cpu,\
MEMORY:.status.capacity.memory
kubectl top nodes 2>/dev/null || echo "metrics-server not available"

# Component status
kubectl get componentstatuses 2>/dev/null

# Pod status — all three namespaces
kubectl get pods -n $NS_DH -o wide
kubectl get pods -n $NS_DS -o wide 2>/dev/null || echo "$NS_DS namespace not found or empty"
kubectl get pods -n $NS_EX -o wide 2>/dev/null || echo "$NS_EX namespace not found or empty"

# PVC status
kubectl get pvc -n $NS_DH 2>/dev/null
kubectl get pvc -n $NS_DS 2>/dev/null
kubectl get pvc -n $NS_EX 2>/dev/null

# Resource usage
kubectl top pods -n $NS_DH --sort-by=cpu 2>/dev/null
kubectl top pods -n $NS_DS --sort-by=cpu 2>/dev/null
kubectl top pods -n $NS_EX 2>/dev/null

# Helm releases
helm list -n $NS_DH
helm list -n $NS_DS 2>/dev/null
helm list --all-namespaces --failed 2>/dev/null

# Warning events (all namespaces)
kubectl get events -n $NS_DH --field-selector type=Warning --sort-by='.lastTimestamp' | tail -20
kubectl get events -n $NS_DS --field-selector type=Warning --sort-by='.lastTimestamp' 2>/dev/null | tail -20
kubectl get events -n $NS_EX --field-selector type=Warning --sort-by='.lastTimestamp' 2>/dev/null | tail -20
```

If `$ARGUMENTS` specifies a **focus area** (e.g., a pod name, release name, or component), also run the troubleshooting workflow from [troubleshooting.md](troubleshooting.md):
- Find matching pods/releases
- Show `kubectl describe` output
- Show logs (last 100 lines)
- Show helm history and values if it's a release

### Report format

```
## Cluster Health Report — <timestamp>

**Context**: <context-name>

### Nodes
| Node | Status | CPU | Memory |
...

### DataHub Namespace ($NS_DH)
| Pod | Status | Restarts | Age | CPU | Memory |
...

### DataSpoke Namespace ($NS_DS)
(not yet deployed / pod table)

### Example Sources Namespace ($NS_EX)
| Pod | Status | Restarts | Age | CPU | Memory |
...

### Helm Releases
| Release | Chart | Status | Revision | Updated |
...

### Warnings
- <warning events, newest first>

### Resource Pressure
- <any nodes near capacity, pods without limits>

### Summary
✅ / ⚠️ / ❌  <overall status with brief notes and suggested next steps>
```

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

**Before acting**: identify the operation type — read, create, modify, or delete.

**Read operations** (safe, execute immediately):
```bash
kubectl get <type> -n <namespace>
kubectl describe <type> <name> -n <namespace>
kubectl logs <pod> -n <namespace> --tail=100
kubectl get events -n <namespace> --sort-by='.lastTimestamp'
helm status <release> -n <namespace>
helm history <release> -n <namespace>
helm get values <release> -n <namespace>
```

**Create/apply operations** (use dry-run first):
```bash
# Validate first
kubectl apply -f <file.yaml> --dry-run=server

# Then apply
kubectl apply -f <file.yaml> -n <namespace>
```

**Modify operations** (confirm intent, then execute):
```bash
kubectl rollout restart deployment/<name> -n <namespace>
helm upgrade <release> <chart> -n <namespace> -f <values.yaml>
```

**Delete operations** — always follow this safety workflow:
```bash
# 1. Confirm you're in the right context
kubectl config current-context

# 2. Describe before deleting
kubectl describe <type> <name> -n <namespace>

# 3. Delete
kubectl delete <type> <name> -n <namespace>

# 4. Verify
kubectl get <type> -n <namespace>
```

**Never delete namespaces.** For scale-to-zero or namespace-level destructive operations, confirm with the user first.

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
