#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.dummy-data-port-forward.pid"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env — copy and edit it before running this script."
fi
source "$SCRIPT_DIR/.env"

NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"
PG_PORT="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT:-9102}"
KAFKA_PORT="${DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARD_PORT:-9104}"

# ---------------------------------------------------------------------------
# --stop: kill running port-forwards and clean up
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    while IFS= read -r pid; do
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null && info "Stopped process $pid"
      fi
    done < "$PID_FILE"
    rm -f "$PID_FILE"
    info "Port-forward stopped and PID file removed."
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
    info "Port-forwards already running (PIDs in $PID_FILE). Use --stop first."
    exit 0
  fi
  # Stale PID file — clean up and continue
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
# Find services
# ---------------------------------------------------------------------------
kubectl get svc example-postgres -n "${NS}" >/dev/null 2>&1 \
  || error "Service 'example-postgres' not found in namespace '${NS}'."

kubectl get svc example-kafka -n "${NS}" >/dev/null 2>&1 \
  || error "Service 'example-kafka' not found in namespace '${NS}'."

# ---------------------------------------------------------------------------
# Start port-forwards in the background
# ---------------------------------------------------------------------------
kubectl port-forward --namespace "${NS}" svc/example-postgres "${PG_PORT}:5432" >/dev/null 2>&1 &
PG_PID=$!

# Forward to the EXTERNAL listener (9094), which advertises localhost:9104
# for host-side access.  The internal PLAINTEXT listener (9092) advertises
# example-kafka:9092, which is unresolvable from the host.
kubectl port-forward --namespace "${NS}" svc/example-kafka "${KAFKA_PORT}:9094" >/dev/null 2>&1 &
KAFKA_PID=$!

# Write PIDs
echo "$PG_PID" > "$PID_FILE"
echo "$KAFKA_PID" >> "$PID_FILE"

# Brief pause to let port-forwards establish
sleep 1

# Verify both are still running
if ! kill -0 "$PG_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  error "PostgreSQL port-forward failed to start."
fi
if ! kill -0 "$KAFKA_PID" 2>/dev/null; then
  kill "$PG_PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  error "Kafka port-forward failed to start."
fi

info "Port-forwards started in background."
echo ""
echo "  PostgreSQL:  localhost:${PG_PORT}  (-> example-postgres:5432)"
echo "  Kafka:       localhost:${KAFKA_PORT}  (-> example-kafka:9094 EXTERNAL)"
echo ""
echo "  PIDs: PostgreSQL=$PG_PID, Kafka=$KAFKA_PID (saved to $PID_FILE)"
echo "  Stop with: $0 --stop"
echo ""
