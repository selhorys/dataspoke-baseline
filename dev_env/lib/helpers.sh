# Shared shell helpers for dev_env scripts.
# Source this file — do not execute directly.
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/helpers.sh"
#   (adjust the relative path depending on script depth)

info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# step <n> <total> <name>
# Print a green [INFO] step-boundary header with elapsed time.
# Reads START_TIME from the environment (exported by the parent script).
step() {
  local n="$1" total="$2" name="$3"
  local elapsed=$(( SECONDS - ${START_TIME:-0} ))
  info "==> [${n}/${total}] ${name} (t+${elapsed}s)"
}

# require_tools <cmd> [<cmd>...]
# Verify each command is on PATH; error if any are missing.
require_tools() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || error "'${cmd}' is not installed or not in PATH."
  done
}

# use_context <cluster>
# Switch the active kubectl context.
use_context() {
  local cluster="$1"
  info "Switching to Kubernetes context: ${cluster}"
  kubectl config use-context "${cluster}"
}

# ensure_namespace <ns>
# Get-or-create a Kubernetes namespace, idempotent.
ensure_namespace() {
  local ns="$1"
  if kubectl get namespace "${ns}" >/dev/null 2>&1; then
    info "Namespace '${ns}' already exists."
  else
    info "Creating namespace '${ns}'..."
    kubectl create namespace "${ns}"
  fi
}

# helm_repo_add_if_missing <name> <url>
# Idempotent helm repo add. Does NOT run helm repo update — callers manage
# that themselves (some update a specific repo, some update all).
helm_repo_add_if_missing() {
  local name="$1" url="$2"
  if helm repo list 2>/dev/null | grep -q "^${name}"; then
    info "Helm repo '${name}' already added."
  else
    info "Adding Helm repo '${name}' (${url})..."
    helm repo add "${name}" "${url}"
  fi
}

# upsert_env_var <key> <value> [env_file]
# Portable .env upsert: update existing KEY= line or append if absent.
# Note: uses '|' as sed delimiter — safe for hex secrets, URLs, and hostnames
# that do not contain literal pipe characters.
upsert_env_var() {
  local key="$1" value="$2"
  local file="${3:-${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)/../.env}}"
  if grep -q "^${key}=" "${file}" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "${file}" && rm -f "${file}.bak"
  else
    printf '\n%s=%s\n' "${key}" "${value}" >> "${file}"
  fi
}

# wait_for_pod <name> <ns> <timeout_secs>
# Poll until the named pod reports Ready=True or timeout.
wait_for_pod() {
  local name="$1" ns="$2" timeout_secs="$3"
  info "  Waiting for pod $name to be Ready (up to ${timeout_secs}s)..."
  local elapsed=0
  while (( elapsed < timeout_secs )); do
    # kubectl wait fails instantly if pod is in CrashLoopBackOff, so we
    # poll manually to tolerate transient restarts during startup.
    local ready
    ready=$(kubectl get "pod/$name" -n "$ns" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
    if [[ "$ready" == "True" ]]; then
      info "  Pod $name is Ready."
      return 0
    fi
    if (( elapsed % 30 == 0 && elapsed > 0 )); then
      local phase restarts
      phase=$(kubectl get "pod/$name" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
      restarts=$(kubectl get "pod/$name" -n "$ns" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")
      info "  [$name] ${elapsed}s — phase=$phase restarts=$restarts"
    fi
    sleep 10
    (( elapsed += 10 ))
  done
  error "Pod $name not ready after ${timeout_secs}s"
}

# wait_for_job <name> <ns> <timeout_secs>
# Poll until the job's pod phase is Succeeded or timeout.
wait_for_job() {
  local name="$1" ns="$2" timeout_secs="$3"
  info "  Waiting for job $name to complete (up to ${timeout_secs}s)..."
  local elapsed=0
  while (( elapsed < timeout_secs )); do
    local phase
    phase=$(kubectl get pod -l "job-name=$name" -n "$ns" \
      -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Pending")
    if [[ "$phase" == "Succeeded" ]]; then
      info "  Job $name completed."
      return 0
    elif [[ "$phase" == "Failed" ]]; then
      error "Job $name failed. Check logs: kubectl logs -l job-name=$name -n $ns"
    fi
    # Print progress every 30s
    if (( elapsed % 30 == 0 && elapsed > 0 )); then
      local tail
      tail=$(kubectl logs -l "job-name=$name" -n "$ns" --tail=1 2>/dev/null || echo "...")
      info "  [$name] ${elapsed}s elapsed — ${tail}"
    fi
    sleep 10
    (( elapsed += 10 ))
  done
  error "Job $name timed out after ${timeout_secs}s"
}

# wait_for_pod_by_label <label> <ns> <timeout_secs>
# Resolve the first pod matching <label>, then delegate to wait_for_pod.
wait_for_pod_by_label() {
  local label="$1" ns="$2" timeout_secs="$3"
  local name
  name=$(kubectl get pod -l "$label" -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -z "$name" ]]; then
    info "  No pod found with label $label yet, waiting..."
    local waited=0
    while (( waited < timeout_secs )); do
      sleep 10; (( waited += 10 ))
      name=$(kubectl get pod -l "$label" -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
      [[ -n "$name" ]] && break
    done
    [[ -z "$name" ]] && error "No pod found for label $label after ${timeout_secs}s"
  fi
  wait_for_pod "$name" "$ns" "$timeout_secs"
}
