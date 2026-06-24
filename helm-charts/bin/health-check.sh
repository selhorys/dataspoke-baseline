#!/usr/bin/env bash
# Health check for DataSpoke services used by integration tests.
#
# Verifies each ingress-exposed service is reachable AND responding at the
# application layer — not just that the TCP port is open.
#
# Usage:
#   ./helm-charts/bin/health-check.sh                   # Check all; prompt to release held lock
#   ./helm-charts/bin/health-check.sh --quick           # TCP-only (skip deep checks)
#   ./helm-charts/bin/health-check.sh --keep-lock       # Don't touch an existing lock
#   ./helm-charts/bin/health-check.sh --force-release   # Release held lock without prompting
#   ./helm-charts/bin/health-check.sh --help            # Print this usage message
#
# Exit codes: 0 = all healthy, 1 = one or more unhealthy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(cd "$SCRIPT_DIR/.." && pwd)/.env"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

QUICK=false
KEEP_LOCK=false
FORCE_RELEASE=false
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=true ;;
    --keep-lock) KEEP_LOCK=true ;;
    --force-release) FORCE_RELEASE=true ;;
    --help|-h)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0
      ;;
    *)
      echo -e "\033[0;31m[ERROR]\033[0m Unknown option: $arg (use --help)" >&2
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  echo -e "\033[0;31m[ERROR]\033[0m .env not found at $ENV_FILE" >&2
  exit 1
fi
source "$ENV_FILE"

# ---------------------------------------------------------------------------
# Ingress coordinates
# ---------------------------------------------------------------------------
INGRESS_MODE="$(ingress_mode)"
INGRESS_IP="${DATASPOKE_KUBE_INGRESS_IP:-}"
DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"

if [[ "$INGRESS_MODE" == "shared" ]]; then
  # Shared mode: HTTP rides the pre-set domain; TCP services are reached on
  # 127.0.0.1 via `kubectl port-forward` (bin/port-forward.sh). No ingress IP.
  if [[ -z "$DOMAIN" ]]; then
    echo -e "\033[0;31m[ERROR]\033[0m DATASPOKE_KUBE_INGRESS_DOMAIN must be set in .env (shared ingress mode)." >&2
    exit 1
  fi
  TCP_HOST="127.0.0.1"
else
  if [[ -z "$INGRESS_IP" || -z "$DOMAIN" ]]; then
    echo -e "\033[0;31m[ERROR]\033[0m DATASPOKE_KUBE_INGRESS_IP and DATASPOKE_KUBE_INGRESS_DOMAIN must be set in .env." >&2
    echo "       Run helm-charts/bin/peripherals/nginx-ingress.sh first." >&2
    exit 1
  fi
  TCP_HOST="${INGRESS_IP}"
fi

# ---------------------------------------------------------------------------
# Tier A: HTTP services via ingress hostname
# ---------------------------------------------------------------------------
DS_API_URL="http://api.${DOMAIN}"
DH_GMS_URL="http://datahub.${DOMAIN}/gms"
DH_UI_URL="http://datahub.${DOMAIN}"
AIRFLOW_URL="http://airflow.${DOMAIN}"
DS_AIRFLOW_USER="${DATASPOKE_TEST_AIRFLOW_USER:-admin}"
DS_AIRFLOW_PASSWORD="${DATASPOKE_TEST_AIRFLOW_PASSWORD:-admin}"

# ---------------------------------------------------------------------------
# Tier B: TCP services — managed: ingress IP; shared: 127.0.0.1 via port-forward.
# Each prefers its DATASPOKE_TEST_* host if one was written to .env.
# ---------------------------------------------------------------------------
DS_PG_HOST="${DATASPOKE_TEST_POSTGRES_HOST:-${TCP_HOST}}"
DS_PG_PORT="${DATASPOKE_TEST_POSTGRES_PORT:-9201}"
DS_PG_USER="${DATASPOKE_TEST_POSTGRES_USER:-dataspoke}"
DS_PG_DB="${DATASPOKE_TEST_POSTGRES_DB:-dataspoke}"

DS_REDIS_HOST="${DATASPOKE_TEST_REDIS_HOST:-${TCP_HOST}}"
DS_REDIS_PORT="${DATASPOKE_TEST_REDIS_PORT:-9202}"
DS_REDIS_PASSWORD="${DATASPOKE_TEST_REDIS_PASSWORD:-}"

DH_KAFKA_BROKERS="${DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS:-${TCP_HOST}:9005}"
DH_KAFKA_HOST="${DH_KAFKA_BROKERS%%:*}"
DH_KAFKA_PORT="${DH_KAFKA_BROKERS##*:}"

DD_PG_HOST="${DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST:-${TCP_HOST}}"
DD_PG_PORT="${DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT:-9102}"
DD_PG_USER="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER:-postgres}"
DD_PG_DB="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB:-example_db}"

_DD_KAFKA_BROKERS="${DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS:-${TCP_HOST}:9104}"
DD_KAFKA_HOST="${_DD_KAFKA_BROKERS%%:*}"
DD_KAFKA_PORT="${_DD_KAFKA_BROKERS##*:}"

LOCK_HOST="${TCP_HOST}"
LOCK_PORT=9221

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
_pass() { echo -e "  \033[0;32m[PASS]\033[0m $*"; }
_fail() { echo -e "  \033[0;31m[FAIL]\033[0m $*"; }
_skip() { echo -e "  \033[0;33m[SKIP]\033[0m $*"; }
_info() { echo -e "  \033[0;36m[INFO]\033[0m $*"; }

FAILURES=0

# ---------------------------------------------------------------------------
# Check primitives
# ---------------------------------------------------------------------------

_tcp_check() {
  local host="$1" port="$2"
  (echo >/dev/tcp/"$host"/"$port") 2>/dev/null
}

_http_ok() {
  local url="$1"
  shift
  local code
  code=$(curl -sf -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" =~ ^2 ]]
}

_http_alive() {
  local url="$1"
  shift
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" != "000" ]]
}

# Gate for HTTP-service checks. In managed mode a quick probe to the ingress
# IP:80 confirms the LoadBalancer is up before the deep HTTP check. In shared
# mode there is no single ingress IP to probe (it may be an internal LB behind
# a hostname), so skip the probe and let the HTTP check itself decide.
_ingress_port_open() {
  if [[ "$INGRESS_MODE" == "shared" ]]; then return 0; fi
  _tcp_check "${INGRESS_IP}" 80
}

# ---------------------------------------------------------------------------
# Per-service checks
# ---------------------------------------------------------------------------

check_dataspoke_postgresql() {
  local label="dataspoke-postgresql (${DS_PG_HOST}:${DS_PG_PORT})"
  if ! _tcp_check "$DS_PG_HOST" "$DS_PG_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if command -v pg_isready &>/dev/null; then
    if pg_isready -h "$DS_PG_HOST" -p "$DS_PG_PORT" -U "$DS_PG_USER" -d "$DS_PG_DB" -q 2>/dev/null; then
      _pass "$label"
    else
      _fail "$label — pg_isready failed (pod may be restarting)"
      ((FAILURES++))
    fi
  else
    if uv run python -c "
import asyncio, asyncpg, sys
async def check():
    try:
        conn = await asyncpg.connect(host='$DS_PG_HOST', port=$DS_PG_PORT,
                                     user='$DS_PG_USER', database='$DS_PG_DB',
                                     password='${DATASPOKE_TEST_POSTGRES_PASSWORD:-}', timeout=3)
        await conn.execute('SELECT 1')
        await conn.close()
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
asyncio.run(check())
" 2>/dev/null; then
      _pass "$label"
    else
      _fail "$label — cannot connect (pod may be restarting)"
      ((FAILURES++))
    fi
  fi
}

check_example_postgres() {
  local label="example-postgres (${DD_PG_HOST}:${DD_PG_PORT})"
  if ! _tcp_check "$DD_PG_HOST" "$DD_PG_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if command -v pg_isready &>/dev/null; then
    if pg_isready -h "$DD_PG_HOST" -p "$DD_PG_PORT" -U "$DD_PG_USER" -d "$DD_PG_DB" -q 2>/dev/null; then
      _pass "$label"
    else
      _fail "$label — pg_isready failed"
      ((FAILURES++))
    fi
  else
    if uv run python -c "
import asyncio, asyncpg, sys
async def check():
    try:
        conn = await asyncpg.connect(host='$DD_PG_HOST', port=$DD_PG_PORT,
                                     user='$DD_PG_USER', database='$DD_PG_DB',
                                     password='${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-}', timeout=3)
        await conn.execute('SELECT 1')
        await conn.close()
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
asyncio.run(check())
" 2>/dev/null; then
      _pass "$label"
    else
      _fail "$label — cannot connect"
      ((FAILURES++))
    fi
  fi
}

check_dataspoke_redis() {
  local label="dataspoke-redis (${DS_REDIS_HOST}:${DS_REDIS_PORT})"
  if ! _tcp_check "$DS_REDIS_HOST" "$DS_REDIS_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  local auth_args=()
  if [[ -n "$DS_REDIS_PASSWORD" ]]; then
    auth_args=(-a "$DS_REDIS_PASSWORD")
  fi
  local response
  response=$(redis-cli -h "$DS_REDIS_HOST" -p "$DS_REDIS_PORT" ${auth_args[@]+"${auth_args[@]}"} \
    PING 2>/dev/null) || true

  if [[ "$response" == "PONG" ]]; then
    _pass "$label"
  else
    _fail "$label — PING did not return PONG"
    ((FAILURES++))
  fi
}

check_dataspoke_airflow() {
  local label="dataspoke-airflow (${AIRFLOW_URL})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  local health_url="${AIRFLOW_URL}/api/v2/monitor/health"
  local health_body
  health_body=$(curl -s -o - -w "" --connect-timeout 3 --max-time 5 \
    "${health_url}" 2>/dev/null) || true

  if echo "$health_body" | grep -q '"status": *"healthy"'; then
    _pass "$label"
  elif _http_alive "${health_url}"; then
    _fail "$label — HTTP alive but health endpoint reports unhealthy: ${health_body}"
    ((FAILURES++))
  else
    _fail "$label — no HTTP response (pod may be starting)"
    ((FAILURES++))
  fi
}

check_dataspoke_api() {
  local label="dataspoke-api (${DS_API_URL})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "${DS_API_URL}/health"; then
    _pass "$label"
  else
    _skip "$label — /health not 2xx (API may not be deployed; run: install.sh --profile dev --components api)"
  fi
}

check_dataspoke_frontend() {
  local fe_url="http://app.${DOMAIN}"
  local label="dataspoke-frontend (${fe_url})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  # The UI root redirects (307 → /login), so treat any HTTP response as alive.
  if _http_alive "${fe_url}/"; then
    _pass "$label"
  else
    _skip "$label — not responding (may not be deployed; run: install.sh --profile dev --components frontend)"
  fi
}

check_dataspoke_langfuse() {
  local lf_url="http://langfuse.${DOMAIN}"
  local label="langfuse-web (${lf_url})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_alive "${lf_url}/"; then
    _pass "$label"
  else
    _skip "$label — not responding (may not be installed; run: install.sh --profile dev --components langfuse)"
  fi

  local worker_ns="${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
  if kubectl get deployment/langfuse-worker -n "${worker_ns}" >/dev/null 2>&1; then
    local ready
    ready=$(kubectl get deployment/langfuse-worker -n "${worker_ns}" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null) || ready="0"
    if [[ "${ready:-0}" -ge 1 ]]; then
      _pass "langfuse-worker (${ready} replica(s) ready)"
    else
      _fail "langfuse-worker — 0 replicas ready"
      ((FAILURES++))
    fi
  else
    _skip "langfuse-worker — not deployed"
  fi
}

check_datahub_gms() {
  local label="datahub-gms (${DH_GMS_URL})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "${DH_GMS_URL}/health"; then
    _pass "$label"
  else
    _fail "$label — /health did not return 2xx"
    ((FAILURES++))
  fi
}

check_kafka() {
  local label="$1" host="$2" port="$3"
  if ! _tcp_check "$host" "$port"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if uv run python -c "
from confluent_kafka.admin import AdminClient
import sys
try:
    a = AdminClient({'bootstrap.servers': '${host}:${port}',
                     'socket.timeout.ms': '5000',
                     'request.timeout.ms': '5000'})
    md = a.list_topics(timeout=5)
    print(f'{len(md.topics)} topics', file=sys.stderr)
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
    _pass "$label"
  else
    _fail "$label — broker not responding to metadata request"
    ((FAILURES++))
  fi
}

_release_lock() {
  local owner="$1" message="$2"
  local release_resp
  release_resp=$(curl -sf -X POST --connect-timeout 3 --max-time 5 \
    -H 'Content-Type: application/json' \
    -d "{\"owner\": \"${owner}\"}" \
    "http://${LOCK_HOST}:${LOCK_PORT}/lock/release" 2>/dev/null) || true
  if echo "$release_resp" | grep -q '"locked" *: *false'; then
    _info "released lock from '${owner}' (${message})"
  else
    _fail "dev-env lock held by '${owner}' — failed to release"
    ((FAILURES++))
  fi
}

check_lock_service() {
  local label="lock-service (${LOCK_HOST}:${LOCK_PORT})"
  if ! _tcp_check "$LOCK_HOST" "$LOCK_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "http://${LOCK_HOST}:${LOCK_PORT}/health"; then
    _pass "$label"
  else
    _fail "$label — /health did not return 2xx"
    ((FAILURES++))
    return
  fi

  local lock_json
  lock_json=$(curl -sf --connect-timeout 3 --max-time 5 "http://${LOCK_HOST}:${LOCK_PORT}/lock" 2>/dev/null) || true
  if [[ -n "$lock_json" ]]; then
    local locked owner message
    locked=$(echo "$lock_json" | grep -o '"locked" *: *true' || true)
    if [[ -n "$locked" ]]; then
      owner=$(echo "$lock_json" | sed -n 's/.*"owner" *: *"\([^"]*\)".*/\1/p')
      message=$(echo "$lock_json" | sed -n 's/.*"message" *: *"\([^"]*\)".*/\1/p')
      if $KEEP_LOCK; then
        _info "dev-env lock held by '${owner}' (${message}) — kept (--keep-lock)"
      elif $FORCE_RELEASE; then
        _release_lock "$owner" "$message"
      else
        echo ""
        _info "dev-env lock held by '${owner}' (${message})"
        printf "  Release this lock? [y/N] "
        local answer
        read -r answer
        if [[ "$answer" =~ ^[Yy]$ ]]; then
          _release_lock "$owner" "$message"
        else
          _fail "dev-env lock held by '${owner}' — integration tests will skip"
          ((FAILURES++))
        fi
      fi
    else
      _info "dev-env lock is free"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------
echo ""
echo "DataSpoke health check"
echo "======================"
if [[ "$INGRESS_MODE" == "shared" ]]; then
  echo "  Ingress mode:   shared (TCP via 127.0.0.1 port-forward — run bin/port-forward.sh)"
else
  echo "  Ingress IP:     ${INGRESS_IP}"
fi
echo "  Ingress domain: ${DOMAIN}"
$QUICK && echo "(quick mode — TCP only, no deep checks)"
echo ""

echo "DataSpoke Infra:"
check_dataspoke_postgresql
check_dataspoke_redis
check_dataspoke_airflow
check_dataspoke_api
check_dataspoke_frontend
check_dataspoke_langfuse

echo ""
echo "DataHub:"
check_datahub_gms
check_kafka "datahub-kafka (${DH_KAFKA_HOST}:${DH_KAFKA_PORT})" "$DH_KAFKA_HOST" "$DH_KAFKA_PORT"

echo ""
echo "Dummy Data:"
check_example_postgres
check_kafka "example-kafka (${DD_KAFKA_HOST}:${DD_KAFKA_PORT})" "$DD_KAFKA_HOST" "$DD_KAFKA_PORT"

echo ""
echo "Dev Coordination:"
check_lock_service

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ $FAILURES -eq 0 ]]; then
  echo -e "\033[0;32mAll services healthy.\033[0m Ready to run integration tests."
else
  echo -e "\033[0;31m${FAILURES} service(s) unhealthy.\033[0m"
  echo "Fix failing services before running integration tests."
  echo "Reinstall hint: ./helm-charts/bin/install.sh --profile dev --components <name>"
  echo "Troubleshooting: see spec/feature/HELM_CHART.md §Troubleshooting"
fi
echo ""

exit "$( (( FAILURES > 0 )) && echo 1 || echo 0 )"
