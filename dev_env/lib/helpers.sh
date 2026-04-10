# Shared shell helpers for dev_env scripts.
# Source this file — do not execute directly.
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/helpers.sh"
#   (adjust the relative path depending on script depth)

info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# warm_up_kestra — exercise JVM hot paths via repeated noop flow executions
#
# Usage: warm_up_kestra <kestra_url> <user> <password> <namespace> [iterations]
#
# Creates a test-noop flow, runs it N times (default 5), polls each execution
# to completion, then deletes the flow.  Non-fatal: returns 0 on success,
# 1 on failure (caller decides whether to abort).
# ---------------------------------------------------------------------------
warm_up_kestra() {
  local kestra_url="$1"
  local kestra_user="$2"
  local kestra_password="$3"
  local kestra_ns="${4:-dataspoke}"
  local iterations="${5:-5}"

  local auth_args=()
  if [[ -n "$kestra_user" && -n "$kestra_password" ]]; then
    auth_args=(-u "${kestra_user}:${kestra_password}")
  fi

  local flow_yaml
  flow_yaml=$(cat <<'FLOW_EOF'
id: test-noop
namespace: __NS__
tasks:
  - id: noop
    type: io.kestra.plugin.core.log.Log
    message: warm-up
FLOW_EOF
)
  flow_yaml="${flow_yaml//__NS__/$kestra_ns}"

  info "Warming up Kestra JVM (${iterations} iterations)..."

  # Create or update the test-noop flow
  local http_code
  http_code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
    "${kestra_url}/api/v1/flows/${kestra_ns}/test-noop" \
    -H "Content-Type: application/x-yaml" \
    -d "$flow_yaml" \
    "${auth_args[@]+"${auth_args[@]}"}" \
    -m 10) || true

  if [[ "$http_code" == "404" ]]; then
    http_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
      "${kestra_url}/api/v1/flows" \
      -H "Content-Type: application/x-yaml" \
      -d "$flow_yaml" \
      "${auth_args[@]+"${auth_args[@]}"}" \
      -m 10) || true
  fi

  if [[ "$http_code" != "200" && "$http_code" != "201" ]]; then
    warn "Kestra warm-up: failed to create test-noop flow (HTTP ${http_code})"
    return 1
  fi

  local i exec_id status elapsed_total=0
  for ((i = 1; i <= iterations; i++)); do
    local start_time=$SECONDS

    # Trigger execution
    exec_id=$(curl -s -X POST \
      "${kestra_url}/api/v1/executions/${kestra_ns}/test-noop" \
      "${auth_args[@]+"${auth_args[@]}"}" \
      -m 10 | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null) || true

    if [[ -z "$exec_id" ]]; then
      warn "Kestra warm-up: failed to trigger execution (iteration ${i})"
      continue
    fi

    # Poll until terminal state
    local poll_count=0
    while [[ $poll_count -lt 30 ]]; do
      sleep 2
      status=$(curl -s \
        "${kestra_url}/api/v1/executions/${exec_id}" \
        "${auth_args[@]+"${auth_args[@]}"}" \
        -m 10 | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',{}).get('current',''))" 2>/dev/null) || true

      if [[ "$status" == "SUCCESS" || "$status" == "FAILED" || "$status" == "KILLED" ]]; then
        break
      fi
      ((poll_count++))
    done

    local elapsed=$(( SECONDS - start_time ))
    elapsed_total=$(( elapsed_total + elapsed ))

    if [[ "$status" == "SUCCESS" ]]; then
      info "  Iteration ${i}/${iterations}: SUCCESS (${elapsed}s)"
    else
      warn "  Iteration ${i}/${iterations}: ${status:-TIMEOUT} (${elapsed}s)"
    fi
  done

  # Cleanup
  curl -s -o /dev/null -X DELETE \
    "${kestra_url}/api/v1/flows/${kestra_ns}/test-noop" \
    "${auth_args[@]+"${auth_args[@]}"}" \
    -m 10 || true

  info "Kestra warm-up complete (${elapsed_total}s total)."
  return 0
}
