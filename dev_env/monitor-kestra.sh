#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Kestra load monitor for the DataSpoke dev environment.
#
# Collects and displays Kestra health signals in a single snapshot:
#   - Kubernetes pod status and resource usage (CPU / memory)
#   - JVM heap and GC indicators from pod logs
#   - PostgreSQL connection pool usage (Kestra DB connections)
#   - Kestra API health probes
#   - Running / queued / failed executions
#   - Recent error-level log lines
#
# Usage:
#   ./dev_env/kestra-monitor.sh              # Full snapshot
#   ./dev_env/kestra-monitor.sh --brief      # One-line summary (for scripting)
#   ./dev_env/kestra-monitor.sh --watch      # Repeat every 15s (Ctrl-C to stop)
#   ./dev_env/kestra-monitor.sh --watch 30   # Repeat every 30s
#
# Exit codes: 0 = healthy, 1 = warning (elevated load), 2 = critical (overloaded)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Portable timeout — GNU coreutils `timeout` or `gtimeout` on macOS.
# Falls back to a bash background-process wrapper if neither is available.
# ---------------------------------------------------------------------------
if command -v timeout &>/dev/null; then
  _timeout() { timeout "$@"; }
elif command -v gtimeout &>/dev/null; then
  _timeout() { gtimeout "$@"; }
else
  # Pure-bash fallback: run command in background, kill after deadline.
  _timeout() {
    local secs="$1"; shift
    "$@" &
    local pid=$!
    ( sleep "$secs"; kill "$pid" 2>/dev/null ) &
    local watcher=$!
    wait "$pid" 2>/dev/null
    local rc=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    return $rc
  }
fi

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
BRIEF=false
WATCH=false
WATCH_INTERVAL=15

while [[ $# -gt 0 ]]; do
  case "$1" in
    --brief) BRIEF=true ;;
    --watch)
      WATCH=true
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then
        WATCH_INTERVAL="$2"; shift
      fi
      ;;
    -h|--help)
      echo "Usage: $0 [--brief] [--watch [SECONDS]]"
      echo ""
      echo "Options:"
      echo "  --brief          One-line summary (for scripting / CI)"
      echo "  --watch [N]      Repeat every N seconds (default: 15)"
      exit 0
      ;;
    *) error "Unknown option: $1. Use --help for usage." ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env"
fi
source "$SCRIPT_DIR/.env"

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
INGRESS_IP="${DATASPOKE_DEV_INGRESS_IP:-}"
DOMAIN="${DATASPOKE_DEV_INGRESS_DOMAIN:-}"
KESTRA_URL="http://kestra.${DOMAIN}"
KESTRA_USER="${DATASPOKE_KESTRA_USER:-}"
KESTRA_PASSWORD="${DATASPOKE_KESTRA_PASSWORD:-}"
PG_USER="${DATASPOKE_POSTGRES_USER:-dataspoke}"
PG_PASSWORD="${DATASPOKE_POSTGRES_PASSWORD:-}"

# Kestra resource limits (must match values-dev.yaml)
CPU_LIMIT_MILLICORES=4000
MEM_LIMIT_MI=8192

# Thresholds (percentage of limit)
CPU_WARN_PCT=50
CPU_CRIT_PCT=75
MEM_WARN_PCT=60
MEM_CRIT_PCT=80
PG_CONN_WARN=30
PG_CONN_CRIT=45
# maximumPoolSize in values-dev.yaml
PG_POOL_MAX=50

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
_green()  { echo -e "\033[0;32m$*\033[0m"; }
_yellow() { echo -e "\033[0;33m$*\033[0m"; }
_red()    { echo -e "\033[0;31m$*\033[0m"; }
_cyan()   { echo -e "\033[0;36m$*\033[0m"; }
_bold()   { echo -e "\033[1m$*\033[0m"; }

_status_color() {
  # $1=value, $2=warn_threshold, $3=crit_threshold
  local val="$1" warn="$2" crit="$3"
  if (( val >= crit )); then
    _red "$val"
  elif (( val >= warn )); then
    _yellow "$val"
  else
    _green "$val"
  fi
}

# ---------------------------------------------------------------------------
# Kestra auth args for curl
# ---------------------------------------------------------------------------
CURL_AUTH=()
if [[ -n "$KESTRA_USER" && -n "$KESTRA_PASSWORD" ]]; then
  CURL_AUTH=(-u "${KESTRA_USER}:${KESTRA_PASSWORD}")
fi

# ---------------------------------------------------------------------------
# Collect data
# ---------------------------------------------------------------------------
EXIT_CODE=0

collect_snapshot() {
  local severity="healthy"

  # -- 1. Pod status ---------------------------------------------------------
  local pod_name pod_status pod_restarts pod_age
  pod_name=$(_timeout 10 kubectl get pods -n "$NS" -l "app.kubernetes.io/name=kestra" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -z "$pod_name" ]]; then
    if $BRIEF; then
      echo "CRITICAL: Kestra pod not found"
    else
      _red "Kestra pod not found in namespace $NS"
    fi
    EXIT_CODE=2; return
  fi

  pod_status=$(_timeout 10 kubectl get pod "$pod_name" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
  pod_restarts=$(_timeout 10 kubectl get pod "$pod_name" -n "$NS" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")
  pod_age=$(_timeout 10 kubectl get pod "$pod_name" -n "$NS" \
    -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null || echo "?")

  # -- 2. Resource usage (kubectl top) ---------------------------------------
  local cpu_m=0 mem_mi=0 cpu_pct=0 mem_pct=0
  local top_line
  top_line=$(_timeout 10 kubectl top pod "$pod_name" -n "$NS" --no-headers 2>/dev/null || echo "")
  if [[ -n "$top_line" ]]; then
    cpu_m=$(echo "$top_line" | awk '{print $2}' | sed 's/m//')
    mem_mi=$(echo "$top_line" | awk '{print $3}' | sed 's/Mi//')
    cpu_pct=$(( cpu_m * 100 / CPU_LIMIT_MILLICORES ))
    mem_pct=$(( mem_mi * 100 / MEM_LIMIT_MI ))
  fi

  # -- 3. PostgreSQL connections from Kestra ---------------------------------
  local pg_conn_count=0
  local pg_pod
  pg_pod=$(_timeout 10 kubectl get pod -n "$NS" \
    -l "app.kubernetes.io/name=postgresql,app.kubernetes.io/instance=dataspoke" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

  if [[ -n "$pg_pod" ]]; then
    pg_conn_count=$(_timeout 10 kubectl exec -n "$NS" "$pg_pod" -- bash -c \
      "PGPASSWORD='${PG_PASSWORD}' psql -U ${PG_USER} -d postgres -tAc \
       \"SELECT count(*) FROM pg_stat_activity WHERE datname = 'kestra';\"" \
      2>/dev/null || echo "?")
    pg_conn_count=$(echo "$pg_conn_count" | tr -d '[:space:]')
  fi

  # -- 4. Kestra health probes -----------------------------------------------
  # Health endpoints are on the management port (8081) which is NOT exposed
  # via ingress, so we probe them inside the pod via kubectl exec.
  # External reachability is tested via the ingress API endpoint.
  # Each probe is wrapped in `timeout 8` to guarantee we never hang — the
  # inner curl has --max-time 3 but kubectl exec itself has no timeout flag.
  local health_status="unreachable" liveness_label="unknown" readiness_label="unknown"

  # Internal probes via management port
  local mgmt_health mgmt_liveness mgmt_readiness
  mgmt_health=$(_timeout 8 kubectl exec -n "$NS" "$pod_name" -- \
    curl -sf -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8081/health 2>/dev/null || echo "000")
  mgmt_liveness=$(_timeout 8 kubectl exec -n "$NS" "$pod_name" -- \
    curl -sf -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8081/health/liveness 2>/dev/null || echo "000")
  mgmt_readiness=$(_timeout 8 kubectl exec -n "$NS" "$pod_name" -- \
    curl -sf -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8081/health/readiness 2>/dev/null || echo "000")

  [[ "$mgmt_health" =~ ^2 ]] && health_status="healthy" || { [[ "$mgmt_health" != "000" ]] && health_status="degraded (HTTP $mgmt_health)"; }
  [[ "$mgmt_liveness" =~ ^2 ]] && liveness_label="ok" || liveness_label="fail (HTTP $mgmt_liveness)"
  [[ "$mgmt_readiness" =~ ^2 ]] && readiness_label="ok" || readiness_label="fail (HTTP $mgmt_readiness)"

  # External reachability via ingress (Kestra API)
  local ingress_status="unreachable"
  local ingress_code
  ingress_code=$(_timeout 8 curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 \
    "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" "${KESTRA_URL}/api/v1/flows/search" 2>/dev/null || echo "000")
  [[ "$ingress_code" =~ ^2 ]] && ingress_status="reachable" || { [[ "$ingress_code" != "000" ]] && ingress_status="degraded (HTTP $ingress_code)"; }

  # -- 5. Kestra executions --------------------------------------------------
  local exec_running=0 exec_created=0 exec_failed_24h=0
  exec_running=$(_timeout 8 curl -s --connect-timeout 3 --max-time 5 \
    "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" \
    "${KESTRA_URL}/api/v1/executions/search?namespace=dataspoke&state=RUNNING&size=0" \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null || echo "?")
  exec_created=$(_timeout 8 curl -s --connect-timeout 3 --max-time 5 \
    "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" \
    "${KESTRA_URL}/api/v1/executions/search?namespace=dataspoke&state=CREATED&size=0" \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null || echo "?")
  exec_failed_24h=$(_timeout 8 curl -s --connect-timeout 3 --max-time 5 \
    "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" \
    "${KESTRA_URL}/api/v1/executions/search?namespace=dataspoke&state=FAILED&size=0" \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null || echo "?")

  # -- 6. Recent error logs --------------------------------------------------
  local error_lines=""
  error_lines=$(_timeout 10 kubectl logs "$pod_name" -n "$NS" --tail=500 --since=5m 2>/dev/null \
    | grep -iE '(ERROR|WARN.*deadlock|OOM|OutOfMemory|GC overhead|FATAL|killed)' \
    | tail -10 || true)

  # -- 7. JVM GC indicators from logs ----------------------------------------
  local gc_lines=""
  gc_lines=$(_timeout 10 kubectl logs "$pod_name" -n "$NS" --tail=500 --since=5m 2>/dev/null \
    | grep -iE '(GC\(|Pause|concurrent|gc,heap)' \
    | tail -5 || true)

  # ---------------------------------------------------------------------------
  # Determine severity
  # ---------------------------------------------------------------------------
  if [[ "$pod_status" != "Running" ]] || [[ "$health_status" == "unreachable" ]]; then
    severity="critical"; EXIT_CODE=2
  elif (( cpu_pct >= CPU_CRIT_PCT )) || (( mem_pct >= MEM_CRIT_PCT )); then
    severity="critical"; EXIT_CODE=2
  elif [[ "${pg_conn_count}" =~ ^[0-9]+$ ]] && (( pg_conn_count >= PG_CONN_CRIT )); then
    severity="critical"; EXIT_CODE=2
  elif (( cpu_pct >= CPU_WARN_PCT )) || (( mem_pct >= MEM_WARN_PCT )); then
    severity="warning"; (( EXIT_CODE < 1 )) && EXIT_CODE=1
  elif [[ "${pg_conn_count}" =~ ^[0-9]+$ ]] && (( pg_conn_count >= PG_CONN_WARN )); then
    severity="warning"; (( EXIT_CODE < 1 )) && EXIT_CODE=1
  fi

  # ---------------------------------------------------------------------------
  # Output: brief mode
  # ---------------------------------------------------------------------------
  if $BRIEF; then
    local sev_upper
    sev_upper=$(echo "$severity" | tr '[:lower:]' '[:upper:]')
    echo "${sev_upper}: cpu=${cpu_m}m(${cpu_pct}%) mem=${mem_mi}Mi(${mem_pct}%) pg_conn=${pg_conn_count}/${PG_POOL_MAX} running=${exec_running} queued=${exec_created} health=${health_status}"
    return
  fi

  # ---------------------------------------------------------------------------
  # Output: full mode
  # ---------------------------------------------------------------------------
  echo ""
  _bold "Kestra Load Monitor"
  echo "==================="
  echo "  Timestamp: $(date '+%Y-%m-%d %H:%M:%S')"
  echo ""

  # Pod
  _bold "Pod"
  echo "  Name:      $pod_name"
  echo "  Status:    $pod_status"
  echo "  Restarts:  $pod_restarts"
  echo "  Created:   $pod_age"
  echo ""

  # Resources
  _bold "Resources (limits: ${CPU_LIMIT_MILLICORES}m CPU / ${MEM_LIMIT_MI}Mi memory)"
  printf "  CPU:       %s / %sm  (%s%%)\n" \
    "$(_status_color "$cpu_m" $(( CPU_LIMIT_MILLICORES * CPU_WARN_PCT / 100 )) $(( CPU_LIMIT_MILLICORES * CPU_CRIT_PCT / 100 )))m" \
    "$CPU_LIMIT_MILLICORES" \
    "$(_status_color "$cpu_pct" "$CPU_WARN_PCT" "$CPU_CRIT_PCT")"
  printf "  Memory:    %s / %sMi  (%s%%)\n" \
    "$(_status_color "$mem_mi" $(( MEM_LIMIT_MI * MEM_WARN_PCT / 100 )) $(( MEM_LIMIT_MI * MEM_CRIT_PCT / 100 )))Mi" \
    "$MEM_LIMIT_MI" \
    "$(_status_color "$mem_pct" "$MEM_WARN_PCT" "$MEM_CRIT_PCT")"
  echo ""

  # PostgreSQL
  _bold "PostgreSQL Connections (pool max: ${PG_POOL_MAX})"
  if [[ "${pg_conn_count}" =~ ^[0-9]+$ ]]; then
    printf "  Active:    %s / %s\n" \
      "$(_status_color "$pg_conn_count" "$PG_CONN_WARN" "$PG_CONN_CRIT")" \
      "$PG_POOL_MAX"
  else
    echo "  Active:    $pg_conn_count"
  fi
  echo ""

  # Health
  _bold "Health Probes (management port 8081, via kubectl exec)"
  echo "  /health:            $health_status"
  echo "  /health/liveness:   $liveness_label"
  echo "  /health/readiness:  $readiness_label"
  echo "  Ingress API:        $ingress_status"
  echo ""

  # Executions
  _bold "Executions"
  echo "  Running:   $exec_running"
  echo "  Queued:    $exec_created"
  echo "  Failed:    $exec_failed_24h"
  echo ""

  # JVM / GC
  if [[ -n "$gc_lines" ]]; then
    _bold "JVM GC Activity (last 5m)"
    echo "$gc_lines" | while IFS= read -r line; do
      echo "  $line"
    done
    echo ""
  fi

  # Errors
  if [[ -n "$error_lines" ]]; then
    _bold "Recent Errors (last 5m)"
    echo "$error_lines" | while IFS= read -r line; do
      _red "  $line"
    done
    echo ""
  fi

  # Verdict
  case "$severity" in
    healthy)  _green "Verdict: HEALTHY" ;;
    warning)  _yellow "Verdict: WARNING — elevated load" ;;
    critical) _red "Verdict: CRITICAL — overloaded (consider: ./dev_env/reinstall.sh --kestra)" ;;
  esac
  echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if $WATCH; then
  trap 'echo ""; echo "Stopped."; exit 0' INT
  while true; do
    if ! $BRIEF; then
      clear 2>/dev/null || true
    fi
    EXIT_CODE=0
    collect_snapshot
    if ! $BRIEF; then
      echo "  (watching every ${WATCH_INTERVAL}s — Ctrl-C to stop)"
    fi
    sleep "$WATCH_INTERVAL"
  done
else
  collect_snapshot
  exit "$EXIT_CODE"
fi
