#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Health check for dev-env peripherals used by integration tests.
#
# Verifies each ingress-exposed service is reachable AND responding at the
# application layer — not just that the TCP port is open.
#
# Usage:
#   ./dev_env/health-check.sh                   # Check all; prompt to release held lock
#   ./dev_env/health-check.sh --quick           # TCP-only (skip deep checks)
#   ./dev_env/health-check.sh --keep-lock       # Don't touch an existing lock
#   ./dev_env/health-check.sh --force-release   # Release held lock without prompting
#
# Exit codes: 0 = all healthy, 1 = one or more unhealthy
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICK=false
KEEP_LOCK=false
FORCE_RELEASE=false
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=true ;;
    --keep-lock) KEEP_LOCK=true ;;
    --force-release) FORCE_RELEASE=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  echo -e "\033[0;31m[ERROR]\033[0m .env not found at $SCRIPT_DIR/.env" >&2
  exit 1
fi
source "$SCRIPT_DIR/.env"

# ---------------------------------------------------------------------------
# Ingress coordinates
# ---------------------------------------------------------------------------
INGRESS_IP="${DATASPOKE_DEV_INGRESS_IP:-}"
DOMAIN="${DATASPOKE_DEV_INGRESS_DOMAIN:-}"

if [[ -z "$INGRESS_IP" || -z "$DOMAIN" ]]; then
  echo -e "\033[0;31m[ERROR]\033[0m DATASPOKE_DEV_INGRESS_IP and DATASPOKE_DEV_INGRESS_DOMAIN must be set in .env." >&2
  echo "       Run dev_env/nginx-ingress/install.sh first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Tier A: HTTP services via ingress hostname
# ---------------------------------------------------------------------------
DS_API_URL="http://app.${DOMAIN}"
DH_GMS_URL="http://datahub.${DOMAIN}/gms"
DH_UI_URL="http://datahub.${DOMAIN}"
KESTRA_URL="http://kestra.${DOMAIN}"
DS_KESTRA_USER="${DATASPOKE_KESTRA_USER:-}"
DS_KESTRA_PASSWORD="${DATASPOKE_KESTRA_PASSWORD:-}"

# ---------------------------------------------------------------------------
# Tier B: TCP services via ingress IP + fixed ports
# ---------------------------------------------------------------------------
DS_PG_HOST="${INGRESS_IP}"
DS_PG_PORT=9201
DS_PG_USER="${DATASPOKE_POSTGRES_USER:-dataspoke}"
DS_PG_DB="${DATASPOKE_POSTGRES_DB:-dataspoke}"

DS_REDIS_HOST="${INGRESS_IP}"
DS_REDIS_PORT=9202
DS_REDIS_PASSWORD="${DATASPOKE_REDIS_PASSWORD:-}"

DS_QDRANT_HOST="${INGRESS_IP}"
DS_QDRANT_HTTP_PORT=9203

DH_KAFKA_HOST="${INGRESS_IP}"
DH_KAFKA_PORT=9005

DD_PG_HOST="${DATASPOKE_EXAMPLE_PG_HOST:-${INGRESS_IP}}"
DD_PG_PORT="${DATASPOKE_EXAMPLE_PG_PORT:-9102}"
DD_PG_USER="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER:-postgres}"
DD_PG_DB="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB:-example_db}"

_DD_KAFKA_BROKERS="${DATASPOKE_EXAMPLE_KAFKA_BROKERS:-${INGRESS_IP}:9104}"
DD_KAFKA_HOST="${_DD_KAFKA_BROKERS%%:*}"
DD_KAFKA_PORT="${_DD_KAFKA_BROKERS##*:}"

LOCK_HOST="${INGRESS_IP}"
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

# TCP connect check.
# Uses bash /dev/tcp which is available on macOS and Linux.
_tcp_check() {
  local host="$1" port="$2"
  (echo >/dev/tcp/"$host"/"$port") 2>/dev/null
}

# HTTP health check — expects 2xx.
_http_ok() {
  local url="$1"
  shift
  local code
  code=$(curl -sf -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" =~ ^2 ]]
}

# HTTP responds at all (even 401/403 means service is alive).
_http_alive() {
  local url="$1"
  shift
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" != "000" ]]
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
    # Fallback: lightweight SQL via uv run
    if uv run python -c "
import asyncio, asyncpg, sys
async def check():
    try:
        conn = await asyncpg.connect(host='$DS_PG_HOST', port=$DS_PG_PORT,
                                     user='$DS_PG_USER', database='$DS_PG_DB',
                                     password='${DATASPOKE_POSTGRES_PASSWORD:-}', timeout=3)
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
                                     password='${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD:-}', timeout=3)
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

  # Use redis-cli for reliable AUTH + PING (nc is unreliable over TCP passthrough)
  local auth_args=()
  if [[ -n "$DS_REDIS_PASSWORD" ]]; then
    auth_args=(-a "$DS_REDIS_PASSWORD")
  fi
  local response
  response=$(redis-cli -h "$DS_REDIS_HOST" -p "$DS_REDIS_PORT" "${auth_args[@]}" \
    PING 2>/dev/null) || true

  if [[ "$response" == "PONG" ]]; then
    _pass "$label"
  else
    _fail "$label — PING did not return PONG"
    ((FAILURES++))
  fi
}

check_dataspoke_qdrant() {
  local label="dataspoke-qdrant (${DS_QDRANT_HOST}:${DS_QDRANT_HTTP_PORT})"
  if ! _tcp_check "$DS_QDRANT_HOST" "$DS_QDRANT_HTTP_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "http://${DS_QDRANT_HOST}:${DS_QDRANT_HTTP_PORT}/healthz"; then
    _pass "$label"
  else
    _fail "$label — /healthz did not return 2xx"
    ((FAILURES++))
  fi
}

check_dataspoke_kestra() {
  local label="dataspoke-kestra (${KESTRA_URL})"
  if ! _tcp_check "${INGRESS_IP}" 80; then
    _fail "$label — ingress port 80 not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  # Kestra may require basic auth; any HTTP response (even 401) means alive.
  # A 2xx on the flows endpoint means fully operational.
  local auth_args=()
  if [[ -n "$DS_KESTRA_USER" && -n "$DS_KESTRA_PASSWORD" ]]; then
    auth_args=(-u "${DS_KESTRA_USER}:${DS_KESTRA_PASSWORD}")
  fi

  if _http_ok "${KESTRA_URL}/api/v1/flows/search" "${auth_args[@]+"${auth_args[@]}"}"; then
    _pass "$label"
  elif _http_alive "${KESTRA_URL}/api/v1/flows/search" "${auth_args[@]+"${auth_args[@]}"}"; then
    _fail "$label — HTTP alive but flows endpoint unhealthy"
    ((FAILURES++))
  else
    _fail "$label — no HTTP response (pod may be restarting)"
    ((FAILURES++))
  fi
}

check_dataspoke_api() {
  local label="dataspoke-api (${DS_API_URL})"
  if ! _tcp_check "${INGRESS_IP}" 80; then
    _fail "$label — ingress port 80 not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "${DS_API_URL}/health"; then
    _pass "$label"
  else
    _skip "$label — /health not 2xx (API may not be deployed; run dataspoke-test-mode.sh)"
  fi
}

check_datahub_gms() {
  local label="datahub-gms (${DH_GMS_URL})"
  if ! _tcp_check "${INGRESS_IP}" 80; then
    _fail "$label — ingress port 80 not reachable"
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

  # Deep check: list topics via confluent-kafka (project dependency)
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

  # Check if the dev-env lock is currently held
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
        # --force-release: release without asking
        _release_lock "$owner" "$message"
      else
        # Interactive: ask the user
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
echo "DataSpoke dev-env health check"
echo "=============================="
echo "  Ingress IP:     ${INGRESS_IP}"
echo "  Ingress domain: ${DOMAIN}"
$QUICK && echo "(quick mode — TCP only, no deep checks)"
echo ""

echo "DataSpoke Infra:"
check_dataspoke_postgresql
check_dataspoke_redis
check_dataspoke_qdrant
check_dataspoke_kestra
check_dataspoke_api

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
  echo "Hint: check pod status with 'kubectl get pods -A' or use '/dev-env' skill."
fi
echo ""

exit "$( (( FAILURES > 0 )) && echo 1 || echo 0 )"
