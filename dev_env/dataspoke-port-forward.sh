#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_PID_FILE="$SCRIPT_DIR/.dataspoke-port-forward-infra.pid"
API_PID_FILE="$SCRIPT_DIR/.dataspoke-port-forward-api.pid"

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
PG_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT:-9201}"
REDIS_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT:-9202}"
QDRANT_HTTP_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT:-9203}"
QDRANT_GRPC_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_GRPC_PORT:-9204}"
KESTRA_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_KESTRA_PORT:-9205}"
API_PORT="${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_API_PORT:-8002}"

# ---------------------------------------------------------------------------
# Helpers: start/stop a group by PID file
# ---------------------------------------------------------------------------
stop_group() {
  local pid_file="$1"
  local label="$2"
  if [[ -f "$pid_file" ]]; then
    while IFS= read -r pid; do
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null && info "Stopped process $pid"
      fi
    done < "$pid_file"
    rm -f "$pid_file"
    info "${label} port-forward stopped."
  else
    warn "${label}: no PID file found — nothing to stop."
  fi
}

is_group_running() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi
  while IFS= read -r pid; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
  done < "$pid_file"
  return 0
}

cleanup_stale_pids() {
  local pid_file="$1"
  if [[ -f "$pid_file" ]]; then
    warn "Stale PID file found — cleaning up."
    while IFS= read -r pid; do
      kill "$pid" 2>/dev/null || true
    done < "$pid_file"
    rm -f "$pid_file"
  fi
}

# ---------------------------------------------------------------------------
# Start infra port-forwards
# ---------------------------------------------------------------------------
start_infra() {
  if is_group_running "$INFRA_PID_FILE"; then
    info "Infra port-forwards already running. Use --infra-stop first."
    return 0
  fi
  cleanup_stale_pids "$INFRA_PID_FILE"

  kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}" >/dev/null 2>&1

  local PIDS=()
  PIDS+=( $(port_forward_loop "${NS}" "svc/dataspoke-postgresql" "${PG_PORT}:5432") )
  PIDS+=( $(port_forward_loop "${NS}" "svc/dataspoke-redis-master" "${REDIS_PORT}:6379") )
  PIDS+=( $(port_forward_loop "${NS}" "svc/dataspoke-qdrant" "${QDRANT_HTTP_PORT}:6333") )
  PIDS+=( $(port_forward_loop "${NS}" "svc/dataspoke-qdrant" "${QDRANT_GRPC_PORT}:6334") )
  PIDS+=( $(port_forward_loop "${NS}" "svc/dataspoke-kestra" "${KESTRA_PORT}:8080") )

  printf '%s\n' "${PIDS[@]}" > "$INFRA_PID_FILE"
  sleep 2

  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
      rm -f "$INFRA_PID_FILE"
      error "Infra port-forward failed to start. Check that pods are Running in namespace '${NS}'."
    fi
  done

  info "Infra port-forwards started."
  echo ""
  echo "  PostgreSQL:  localhost:${PG_PORT}   (-> dataspoke-postgresql:5432)"
  echo "  Redis:       localhost:${REDIS_PORT}   (-> dataspoke-redis-master:6379)"
  echo "  Qdrant HTTP: localhost:${QDRANT_HTTP_PORT}   (-> dataspoke-qdrant:6333)"
  echo "  Qdrant gRPC: localhost:${QDRANT_GRPC_PORT}   (-> dataspoke-qdrant:6334)"
  echo "  Kestra:      localhost:${KESTRA_PORT}   (-> dataspoke-kestra:8080)"
  echo ""
}

# ---------------------------------------------------------------------------
# Start API port-forward
# ---------------------------------------------------------------------------
start_api() {
  if is_group_running "$API_PID_FILE"; then
    info "API port-forward already running. Use --api-stop first."
    return 0
  fi
  cleanup_stale_pids "$API_PID_FILE"

  kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}" >/dev/null 2>&1

  local PID
  PID=$(port_forward_loop "${NS}" "svc/dataspoke-api" "${API_PORT}:8002")
  echo "$PID" > "$API_PID_FILE"
  sleep 2

  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$API_PID_FILE"
    error "API port-forward failed to start. Check that dataspoke-api pod is Running in namespace '${NS}'."
  fi

  info "API port-forward started."
  echo ""
  echo "  API:   localhost:${API_PORT}   (-> dataspoke-api:8002)"
  echo "  ReDoc: http://localhost:${API_PORT}/redoc"
  echo ""
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  echo "Usage: $0 [--all-start|--all-stop|--infra-start|--infra-stop|--api-start|--api-stop]"
  echo ""
  echo "  --all-start     Start infra + API port-forwards"
  echo "  --all-stop      Stop infra + API port-forwards"
  echo "  --infra-start   Start infra port-forwards (PostgreSQL, Redis, Qdrant, Kestra)"
  echo "  --infra-stop    Stop infra port-forwards"
  echo "  --api-start     Start API port-forward"
  echo "  --api-stop      Stop API port-forward"
  echo ""
  echo "  No arguments:   same as --infra-start (backward compatible)"
  echo "  --stop:          same as --all-stop (backward compatible)"
}

# ---------------------------------------------------------------------------
# Main: parse action
# ---------------------------------------------------------------------------
ACTION="${1:-infra-start}"

case "$ACTION" in
  --all-start)
    start_infra
    start_api
    ;;
  --all-stop)
    stop_group "$API_PID_FILE" "API"
    stop_group "$INFRA_PID_FILE" "Infra"
    ;;
  --infra-start)
    start_infra
    ;;
  --infra-stop)
    stop_group "$INFRA_PID_FILE" "Infra"
    ;;
  --api-start)
    start_api
    ;;
  --api-stop)
    stop_group "$API_PID_FILE" "API"
    ;;
  # Backward compatible: no args = start infra, --stop = stop all
  --stop)
    stop_group "$API_PID_FILE" "API"
    stop_group "$INFRA_PID_FILE" "Infra"
    ;;
  --help|-h)
    usage
    ;;
  *)
    error "Unknown option: $ACTION. Run $0 --help for usage."
    ;;
esac
