#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Start the DataSpoke host-mode server in test mode for api-wired
# integration testing.
#
# Enables DATASPOKE_TEST_MODE so Kestra activity endpoints use stub
# implementations for LLM, Qdrant, cache, and notification — avoiding
# real external API calls.  DataHub and PostgreSQL use real dev-env
# connections.
#
# Usage:
#   ./dev_env/dataspoke-test-mode.sh                     # Backend-only (default)
#   ./dev_env/dataspoke-test-mode.sh --skip-migrate       # Skip Alembic migration
#   ./dev_env/dataspoke-test-mode.sh --no-reload          # Disable uvicorn auto-reload
#   ./dev_env/dataspoke-test-mode.sh --port 9000          # Custom API port
#   ./dev_env/dataspoke-test-mode.sh --health-check       # Run health check first
#   ./dev_env/dataspoke-test-mode.sh --health-check-only  # Health check without starting
#   ./dev_env/dataspoke-test-mode.sh --stop               # Stop running instance and exit
#
# After the server is running, in a second terminal:
#   uv run pytest tests/integration/api_wired/ -v
#
# Exit: Ctrl+C (graceful shutdown)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SKIP_MIGRATE=false
NO_RELOAD=false
CUSTOM_PORT=""
HEALTH_CHECK=false
HEALTH_CHECK_ONLY=false
STOP_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-migrate)      SKIP_MIGRATE=true ;;
    --no-reload)         NO_RELOAD=true ;;
    --port)              CUSTOM_PORT="$2"; shift ;;
    --health-check)      HEALTH_CHECK=true ;;
    --health-check-only) HEALTH_CHECK_ONLY=true; HEALTH_CHECK=true ;;
    --stop)              STOP_ONLY=true ;;
    *) warn "Unknown option: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env"
fi
source "$SCRIPT_DIR/.env"

PORT="${CUSTOM_PORT:-${DATASPOKE_API_PORT:-8000}}"

# ---------------------------------------------------------------------------
# --stop: kill running instance and exit
# ---------------------------------------------------------------------------
if [[ "$STOP_ONLY" == "true" ]]; then
  EXISTING_PIDS=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "$EXISTING_PIDS" ]]; then
    info "Stopping test-mode server on port $PORT (PIDs: $(echo $EXISTING_PIDS | tr '\n' ' '))"
    echo "$EXISTING_PIDS" | xargs kill 2>/dev/null || true
  else
    info "No test-mode server running on port $PORT"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Health check (optional)
# ---------------------------------------------------------------------------
if [[ "$HEALTH_CHECK" == "true" ]]; then
  info "Running dev-env health check..."
  if ! bash "$SCRIPT_DIR/health-check.sh"; then
    error "Health check failed — fix the failing services before starting test mode."
  fi
  if [[ "$HEALTH_CHECK_ONLY" == "true" ]]; then
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Build CLI arguments
# ---------------------------------------------------------------------------
CLI_ARGS=(--backend-only)

if [[ "$SKIP_MIGRATE" == "true" ]]; then
  CLI_ARGS+=(--skip-migrate)
fi

if [[ "$NO_RELOAD" == "true" ]]; then
  CLI_ARGS+=(--no-reload)
fi

CLI_ARGS+=(--port "$PORT")

# ---------------------------------------------------------------------------
# Kill previous instance (if any)
# ---------------------------------------------------------------------------
EXISTING_PIDS=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [[ -n "$EXISTING_PIDS" ]]; then
  info "Killing previous process(es) on port $PORT (PIDs: $(echo $EXISTING_PIDS | tr '\n' ' '))"
  echo "$EXISTING_PIDS" | xargs kill 2>/dev/null || true
  sleep 1
fi

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
info "Starting DataSpoke in test mode (port=$PORT)"
info "  DATASPOKE_TEST_MODE=true"
info "  DATASPOKE_KESTRA_CALLBACK_BASE_URL=http://host.docker.internal:$PORT"
echo ""

export DATASPOKE_TEST_MODE=true
export DATASPOKE_KESTRA_CALLBACK_BASE_URL="http://host.docker.internal:$PORT"

# ---------------------------------------------------------------------------
# Resolve DataHub token (if not already set)
# ---------------------------------------------------------------------------
if [[ -z "${DATASPOKE_DATAHUB_TOKEN:-}" ]]; then
  FRONTEND_URL="${DATASPOKE_DATAHUB_FRONTEND_URL:-http://localhost:9002}"
  TOKEN=$(python3 -c "
import requests, base64, json, sys
try:
    r = requests.post('$FRONTEND_URL/logIn',
        json={'username': 'datahub', 'password': 'datahub'}, timeout=5)
    r.raise_for_status()
    cookie = r.headers.get('Set-Cookie', '')
    if 'PLAY_SESSION=' not in cookie: sys.exit(0)
    ps = cookie.split('PLAY_SESSION=')[1].split(';')[0]
    payload = ps.split('.')[1]
    payload += '=' * (4 - len(payload) % 4)
    data = json.loads(base64.b64decode(payload))
    print(data.get('data', {}).get('token', ''))
except Exception:
    pass
" 2>/dev/null || true)
  if [[ -n "$TOKEN" ]]; then
    export DATASPOKE_DATAHUB_TOKEN="$TOKEN"
    info "Resolved DataHub token via frontend login"
  else
    warn "Could not resolve DataHub token — DataHub endpoints may return 401"
  fi
fi

cd "$PROJECT_ROOT"
exec uv run -m src.cli "${CLI_ARGS[@]}"
