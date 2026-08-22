# kubectl Skill — Operations Reference

## Helm Chart Operations

```bash
# Status and inspection
helm list --all-namespaces
helm list --all-namespaces --failed
helm status <release> -n <namespace>
helm history <release> -n <namespace>
helm get all <release> -n <namespace>
helm get values <release> -n <namespace>
helm get manifest <release> -n <namespace>
helm get notes <release> -n <namespace>

# Resources deployed by a release
kubectl get all -n <namespace> -l app.kubernetes.io/instance=<release>
kubectl describe deployment -n <namespace> -l app.kubernetes.io/instance=<release>

# Logs from all pods in a release
for pod in $(kubectl get pods -n <namespace> -l app.kubernetes.io/instance=<release> -o name); do
  echo "=== $pod ==="
  kubectl logs $pod -n <namespace> --tail=50
done

# Upgrade / rollback
helm upgrade <release> <chart> -n <namespace> -f values.yaml --dry-run
helm upgrade <release> <chart> -n <namespace> -f values.yaml
helm rollback <release> <revision> -n <namespace>

# Uninstall
helm uninstall <release> -n <namespace> --dry-run
helm uninstall <release> -n <namespace>
helm uninstall <release> -n <namespace> --keep-history
```

## Resource Creation Patterns

```bash
# Validate before applying
kubectl apply -f <file.yaml> --dry-run=server

# Apply
kubectl apply -f <file.yaml> -n <namespace>
kubectl apply -f <directory>/ -n <namespace>

# Imperative creation
kubectl create namespace <name>
kubectl create deployment <name> --image=<image> -n <namespace>
kubectl expose deployment <name> --port=<port> --type=ClusterIP -n <namespace>
kubectl create configmap <name> --from-file=<path> -n <namespace>
kubectl create secret generic <name> --from-literal=key=value -n <namespace>
```

## Deletion Patterns

```bash
# Standard deletion
kubectl delete <type> <name> -n <namespace>
kubectl delete -f <file.yaml>
kubectl delete pods -l app=<name> -n <namespace>

# Force delete stuck pods
kubectl delete pod <name> -n <namespace> --force --grace-period=0

# Helm uninstall
helm uninstall <release> -n <namespace>
```

## Capacity Planning

```bash
# Node allocation vs capacity
kubectl describe nodes | grep -A 5 "Allocated resources"

# Top consumers
kubectl top pods --all-namespaces --sort-by=cpu 2>/dev/null | head -20
kubectl top pods --all-namespaces --sort-by=memory 2>/dev/null | head -20

# Pods without resource limits
kubectl get pods --all-namespaces -o json | \
  jq -r '.items[] | select(.spec.containers[].resources.limits == null) | "\(.metadata.namespace)/\(.metadata.name)"'

# Resource requests/limits summary
kubectl get pods --all-namespaces -o custom-columns=\
NAMESPACE:.metadata.namespace,\
NAME:.metadata.name,\
CPU-REQ:.spec.containers[*].resources.requests.cpu,\
MEM-REQ:.spec.containers[*].resources.requests.memory,\
CPU-LIM:.spec.containers[*].resources.limits.cpu,\
MEM-LIM:.spec.containers[*].resources.limits.memory
```

## Debugging Commands

```bash
# Pod details and events
kubectl describe pod <name> -n <namespace>
kubectl get events -n <namespace> --sort-by='.lastTimestamp'

# Logs
kubectl logs <pod> -n <namespace> --tail=100
kubectl logs <pod> -n <namespace> --previous          # after restart
kubectl logs <pod> -n <namespace> -c <container>      # specific container
kubectl logs -f <pod> -n <namespace>                  # follow

# Exec into pod
kubectl exec -it <pod> -n <namespace> -- /bin/sh

# Port forward
kubectl port-forward <pod> <local>:<remote> -n <namespace>

# API server health
kubectl get --raw '/healthz?verbose'
```

## Error Reference

| Error | Likely Cause | Resolution |
|-------|-------------|------------|
| `connection refused` | kubeconfig not set | `kubectl config view`, set context |
| `Unauthorized` | Invalid credentials | Update kubeconfig |
| `namespace not found` | Wrong namespace | `kubectl get ns` |
| `resource not found` | Deleted or wrong name | `kubectl get <type>` |
| `ImagePullBackOff` | Bad image name or registry auth | Check image and `imagePullSecrets` |
| `CrashLoopBackOff` | Application error on startup | `kubectl logs <pod> --previous` |
| `Pending` | Unschedulable — resource or selector | `kubectl describe pod` → Events section |
| `OOMKilled` | Memory limit exceeded | Increase memory limit in values |
| `Error: UPGRADE FAILED` | Helm conflict or invalid values | `helm history` + `helm rollback` |
| `Terminating` (stuck) | Finalizer blocking deletion | Remove finalizer via `kubectl patch` |
