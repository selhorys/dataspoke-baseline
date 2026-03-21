#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Health check for dev-env peripherals used by integration tests.
#
# Verifies each port-forwarded service is reachable AND responding at the
# application layer — not just that a port-forward process exists.
#
# Usage:
#   ./dev_env/health-check.sh           # Check all services
#   ./dev_env/health-check.sh --quick   # TCP-only (skip deep checks)
#
# Exit codes: 0 = all healthy, 1 = one or more unhealthy
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICK=false
[[ "${1:-}" == "--quick" ]] && QUICK=true

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  echo -e "\033[0;31m[ERROR]\033[0m .env not found at $SCRIPT_DIR/.env" >&2
  exit 1
fi
source "$SCRIPT_DIR/.env"

# ---------------------------------------------------------------------------
# Port / credential variables (same defaults as port-forward scripts & conftest)
# ---------------------------------------------------------------------------

# DataSpoke infra (dataspoke-port-forward.sh)
DS_PG_HOST="localhost"
DS_PG_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT:-9201}"
DS_PG_USER="${DATASPOKE_POSTGRES_USER:-dataspoke}"
DS_PG_DB="${DATASPOKE_POSTGRES_DB:-dataspoke}"
DS_REDIS_HOST="localhost"
DS_REDIS_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT:-9202}"
DS_REDIS_PASSWORD="${DATASPOKE_REDIS_PASSWORD:-}"
DS_QDRANT_HTTP_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT:-9203}"
DS_KESTRA_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_KESTRA_PORT:-9205}"
DS_KESTRA_USER="${DATASPOKE_KESTRA_USER:-}"
DS_KESTRA_PASSWORD="${DATASPOKE_KESTRA_PASSWORD:-}"

# DataHub (datahub-port-forward.sh)
DH_GMS_PORT="${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_GMS_PORT:-9004}"
_DH_KAFKA_BROKERS="${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_KAFKA_BROKERS:-localhost:9005}"
DH_KAFKA_PORT="${_DH_KAFKA_BROKERS##*:}"

# Dummy data (dummy-data-port-forward.sh)
DD_PG_HOST="localhost"
DD_PG_PORT="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT:-9102}"
DD_PG_USER="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER:-postgres}"
DD_PG_DB="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB:-example_db}"
_DD_KAFKA_BROKERS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS:-localhost:9104}"
DD_KAFKA_PORT="${_DD_KAFKA_BROKERS##*:}"

# Lock (lock-port-forward.sh)
LOCK_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_DEV_ENV_LOCK_PORT:-9221}"

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

# TCP connect check (fastest — catches port-forward down).
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
  local label="dataspoke-postgresql (localhost:${DS_PG_PORT})"
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
  local label="example-postgres (localhost:${DD_PG_PORT})"
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
  local label="dataspoke-redis (localhost:${DS_REDIS_PORT})"
  if ! _tcp_check "$DS_REDIS_HOST" "$DS_REDIS_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  # Send AUTH + PING over raw TCP and look for +PONG
  local response
  if [[ -n "$DS_REDIS_PASSWORD" ]]; then
    response=$(printf "AUTH %s\r\nPING\r\n" "$DS_REDIS_PASSWORD" \
      | nc -w 2 "$DS_REDIS_HOST" "$DS_REDIS_PORT" 2>/dev/null) || true
  else
    response=$(printf "PING\r\n" \
      | nc -w 2 "$DS_REDIS_HOST" "$DS_REDIS_PORT" 2>/dev/null) || true
  fi

  if echo "$response" | grep -q "+PONG"; then
    _pass "$label"
  else
    _fail "$label — PING did not return PONG"
    ((FAILURES++))
  fi
}

check_dataspoke_qdrant() {
  local label="dataspoke-qdrant (localhost:${DS_QDRANT_HTTP_PORT})"
  if ! _tcp_check "localhost" "$DS_QDRANT_HTTP_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "http://localhost:${DS_QDRANT_HTTP_PORT}/healthz"; then
    _pass "$label"
  else
    _fail "$label — /healthz did not return 2xx"
    ((FAILURES++))
  fi
}

check_dataspoke_kestra() {
  local label="dataspoke-kestra (localhost:${DS_KESTRA_PORT})"
  if ! _tcp_check "localhost" "$DS_KESTRA_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  # Kestra may require basic auth; any HTTP response (even 401) means alive.
  # A 2xx on the flows endpoint means fully operational.
  local auth_args=()
  if [[ -n "$DS_KESTRA_USER" && -n "$DS_KESTRA_PASSWORD" ]]; then
    auth_args=(-u "${DS_KESTRA_USER}:${DS_KESTRA_PASSWORD}")
  fi

  if _http_ok "http://localhost:${DS_KESTRA_PORT}/api/v1/flows/search" "${auth_args[@]+"${auth_args[@]}"}"; then
    _pass "$label"
  elif _http_alive "http://localhost:${DS_KESTRA_PORT}/api/v1/flows/search" "${auth_args[@]+"${auth_args[@]}"}"; then
    _fail "$label — HTTP alive but flows endpoint unhealthy"
    ((FAILURES++))
  else
    _fail "$label — no HTTP response (pod may be restarting)"
    ((FAILURES++))
  fi
}

check_datahub_gms() {
  local label="datahub-gms (localhost:${DH_GMS_PORT})"
  if ! _tcp_check "localhost" "$DH_GMS_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "http://localhost:${DH_GMS_PORT}/health"; then
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

check_lock_service() {
  local label="lock-service (localhost:${LOCK_PORT})"
  if ! _tcp_check "localhost" "$LOCK_PORT"; then
    _fail "$label — port not reachable"
    ((FAILURES++)); return
  fi
  if $QUICK; then _pass "$label (tcp)"; return; fi

  if _http_ok "http://localhost:${LOCK_PORT}/health"; then
    _pass "$label"
  else
    _fail "$label — /health did not return 2xx"
    ((FAILURES++))
  fi
}

# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------
echo ""
echo "DataSpoke dev-env health check"
echo "=============================="
$QUICK && echo "(quick mode — TCP only, no deep checks)"
echo ""

echo "DataSpoke Infra:"
check_dataspoke_postgresql
check_dataspoke_redis
check_dataspoke_qdrant
check_dataspoke_kestra

echo ""
echo "DataHub:"
check_datahub_gms
check_kafka "datahub-kafka (localhost:${DH_KAFKA_PORT})" "localhost" "$DH_KAFKA_PORT"

echo ""
echo "Dummy Data:"
check_example_postgres
check_kafka "example-kafka (localhost:${DD_KAFKA_PORT})" "localhost" "$DD_KAFKA_PORT"

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
