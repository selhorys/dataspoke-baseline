# Kubernetes Troubleshooting Reference

## Troubleshooting a Failed Helm Release

```bash
# 1. Check release status and history
helm status <release> -n <namespace>
helm history <release> -n <namespace>

# 2. Compare current vs previous values
helm get values <release> -n <namespace> --revision <prev>
helm get values <release> -n <namespace>

# 3. Check deployed resources
kubectl get all -n <namespace> -l app.kubernetes.io/instance=<release>

# 4. Identify problematic pods
kubectl get pods -n <namespace> -l app.kubernetes.io/instance=<release>

# 5. Examine pod details
kubectl describe pod <pod-name> -n <namespace>

# 6. Check logs (including previous container if restarted)
kubectl logs <pod-name> -n <namespace> --tail=100
kubectl logs <pod-name> -n <namespace> --previous 2>/dev/null

# 7. Review events (newest last)
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | tail -20
```

## Cluster Health Deep Dive

```bash
# API server health
kubectl get --raw '/healthz?verbose'

# Node resource allocation vs capacity
kubectl describe nodes | grep -A 5 "Allocated resources"

# Pods without resource limits (risk factor)
kubectl get pods --all-namespaces -o json | \
  jq -r '.items[] | select(.spec.containers[].resources.limits == null) | "\(.metadata.namespace)/\(.metadata.name)"'

# Top resource consumers
kubectl top pods --all-namespaces --sort-by=cpu 2>/dev/null | head -20
kubectl top pods --all-namespaces --sort-by=memory 2>/dev/null | head -20

# System pod health
kubectl get pods -n kube-system
```

## Helm Deep Dive Commands

```bash
# Full release details
helm get all <release-name> -n <namespace>
helm get manifest <release-name> -n <namespace>
helm get notes <release-name> -n <namespace>

# Failed/pending releases across all namespaces
helm list --all-namespaces --failed
helm list --all-namespaces --pending

# Logs from all pods in a release
for pod in $(kubectl get pods -n <namespace> -l app.kubernetes.io/instance=<release> -o name); do
  echo "=== $pod ==="
  kubectl logs $pod -n <namespace> --tail=50
done
```

## Common Error Reference

| Error | Likely Cause | Resolution |
|-------|-------------|------------|
| `connection refused` | kubeconfig not set | `kubectl config view` and set context |
| `Unauthorized` | Invalid credentials | Update kubeconfig |
| `namespace not found` | Wrong namespace | `kubectl get ns` |
| `resource not found` | Deleted or wrong name | `kubectl get <type>` |
| `ImagePullBackOff` | Bad image or registry auth | Check image name and `imagePullSecrets` |
| `CrashLoopBackOff` | App error | `kubectl logs <pod> --previous` |
| `Pending` | Resource constraints or unschedulable | Check events and `kubectl top nodes` |
| `OOMKilled` | Memory limit exceeded | Increase memory limit in values |
| `Error: UPGRADE FAILED` | Helm conflict or bad values | `helm history` + rollback |
