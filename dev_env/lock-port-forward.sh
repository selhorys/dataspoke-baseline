#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.lock-port-forward.pid"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env — copy and edit it before running this script."
fi
source "$SCRIPT_DIR/.env"

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
LOCK_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_DEV_ENV_LOCK_PORT:-9221}"

# ---------------------------------------------------------------------------
# --stop: kill running port-forward and clean up
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    while IFS= read -r pid; do
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null && info "Stopped process $pid"
      fi
    done < "$PID_FILE"
    rm -f "$PID_FILE"
    info "Lock port-forward stopped and PID file removed."
  else
    warn "No PID file found — nothing to stop."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Guard: already running?
# ---------------------------------------------------------------------------
if [[ -f "$PID_FILE" ]]; then
  ALL_ALIVE=true
  while IFS= read -r pid; do
    if ! kill -0 "$pid" 2>/dev/null; then
      ALL_ALIVE=false
      break
    fi
  done < "$PID_FILE"
  if $ALL_ALIVE; then
    info "Lock port-forward already running (PIDs in $PID_FILE). Use --stop first."
    exit 0
  fi
  warn "Stale PID file found — cleaning up."
  while IFS= read -r pid; do
    kill "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi

# ---------------------------------------------------------------------------
# Switch context
# ---------------------------------------------------------------------------
kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}" >/dev/null 2>&1

# ---------------------------------------------------------------------------
# Start port-forward in the background
# ---------------------------------------------------------------------------
PID=$(port_forward_loop "${NS}" "svc/dev-lock" "${LOCK_PORT}:8080")
echo "$PID" > "$PID_FILE"

sleep 2

if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  error "Lock port-forward failed to start. Check that the dev-lock pod is Running in namespace '${NS}'."
fi

info "Lock port-forward started in background."
echo ""
echo "  Lock API: localhost:${LOCK_PORT}   (-> dev-lock:8080)"
echo ""
echo "  PID: ${PID} (saved to $PID_FILE)"
echo "  Stop with: $0 --stop"
echo ""
