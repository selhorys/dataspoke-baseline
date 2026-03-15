#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.dataspoke-port-forward.pid"

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

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
PG_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT:-9201}"
REDIS_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT:-9202}"
QDRANT_HTTP_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT:-9203}"
QDRANT_GRPC_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_GRPC_PORT:-9204}"
TEMPORAL_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_TEMPORAL_PORT:-9205}"
TEMPORAL_UI_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_TEMPORAL_UI_PORT:-9206}"

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
# Start port-forwards in the background
# ---------------------------------------------------------------------------
PIDS=()

# PostgreSQL
kubectl port-forward --namespace "${NS}" svc/dataspoke-postgresql "${PG_PORT}:5432" >/dev/null 2>&1 &
PIDS+=($!)

# Redis
kubectl port-forward --namespace "${NS}" svc/dataspoke-redis-master "${REDIS_PORT}:6379" >/dev/null 2>&1 &
PIDS+=($!)

# Qdrant HTTP
kubectl port-forward --namespace "${NS}" svc/dataspoke-qdrant "${QDRANT_HTTP_PORT}:6333" >/dev/null 2>&1 &
PIDS+=($!)

# Qdrant gRPC
kubectl port-forward --namespace "${NS}" svc/dataspoke-qdrant "${QDRANT_GRPC_PORT}:6334" >/dev/null 2>&1 &
PIDS+=($!)

# Temporal gRPC
kubectl port-forward --namespace "${NS}" svc/dataspoke-temporal-frontend "${TEMPORAL_PORT}:7233" >/dev/null 2>&1 &
PIDS+=($!)

# Temporal Web UI
kubectl port-forward --namespace "${NS}" svc/dataspoke-temporal-web "${TEMPORAL_UI_PORT}:8080" >/dev/null 2>&1 &
PIDS+=($!)

# Write PIDs
printf '%s\n' "${PIDS[@]}" > "$PID_FILE"

# Brief pause to let port-forwards establish
sleep 2

# Verify all are still running
FAILED=false
for pid in "${PIDS[@]}"; do
  if ! kill -0 "$pid" 2>/dev/null; then
    FAILED=true
    break
  fi
done

if $FAILED; then
  # Clean up any that did start
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  rm -f "$PID_FILE"
  error "One or more port-forwards failed to start. Check that all infra pods are Running in namespace '${NS}'."
fi

info "Port-forwards started in background."
echo ""
echo "  PostgreSQL:  localhost:${PG_PORT}   (-> dataspoke-postgresql:5432)"
echo "  Redis:       localhost:${REDIS_PORT}   (-> dataspoke-redis-master:6379)"
echo "  Qdrant HTTP: localhost:${QDRANT_HTTP_PORT}   (-> dataspoke-qdrant:6333)"
echo "  Qdrant gRPC: localhost:${QDRANT_GRPC_PORT}   (-> dataspoke-qdrant:6334)"
echo "  Temporal:    localhost:${TEMPORAL_PORT}   (-> dataspoke-temporal-frontend:7233)"
echo "  Temporal UI: localhost:${TEMPORAL_UI_PORT}   (-> dataspoke-temporal-web:8080)"
echo ""
echo "  PIDs: ${PIDS[*]} (saved to $PID_FILE)"
echo "  Stop with: $0 --stop"
echo ""
