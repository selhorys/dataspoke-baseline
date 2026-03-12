#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.datahub-port-forward.pid"

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

NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
UI_PORT="${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_UI_PORT:-9002}"
GMS_PORT="${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_GMS_PORT:-9004}"
KAFKA_PORT="${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_KAFKA_PORT:-9005}"

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
# Find pods / services
# ---------------------------------------------------------------------------
FRONTEND_POD=$(kubectl get pods -n "${NS}" \
  -l 'app.kubernetes.io/name=datahub-frontend' \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) \
  || error "No datahub-frontend pod found in namespace '${NS}'."
[[ -z "$FRONTEND_POD" ]] && error "No datahub-frontend pod found in namespace '${NS}'."

# GMS uses a service, so we forward to the service directly
GMS_SVC="datahub-datahub-gms"
kubectl get svc "$GMS_SVC" -n "${NS}" >/dev/null 2>&1 \
  || error "Service '$GMS_SVC' not found in namespace '${NS}'."

# Kafka pod (from datahub-prerequisites) — port-forward targets the EXTERNAL
# listener (9095), which advertises localhost:9005 for host-side access.
# We forward to the pod directly (not the service) since the EXTERNAL port
# is not exposed on the ClusterIP service.
KAFKA_POD=$(kubectl get pods -n "${NS}" \
  -l 'app.kubernetes.io/name=kafka,app.kubernetes.io/component=broker' \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) \
  || error "No kafka broker pod found in namespace '${NS}'."
[[ -z "$KAFKA_POD" ]] && error "No kafka broker pod found in namespace '${NS}'."

# ---------------------------------------------------------------------------
# Start port-forwards in the background
# ---------------------------------------------------------------------------
kubectl port-forward --namespace "${NS}" "${FRONTEND_POD}" "${UI_PORT}:9002" >/dev/null 2>&1 &
UI_PID=$!

kubectl port-forward --namespace "${NS}" "svc/${GMS_SVC}" "${GMS_PORT}:8080" >/dev/null 2>&1 &
GMS_PID=$!

kubectl port-forward --namespace "${NS}" "${KAFKA_POD}" "${KAFKA_PORT}:9095" >/dev/null 2>&1 &
KAFKA_PID=$!

# Write PIDs
echo "$UI_PID" > "$PID_FILE"
echo "$GMS_PID" >> "$PID_FILE"
echo "$KAFKA_PID" >> "$PID_FILE"

# Brief pause to let port-forwards establish
sleep 1

# Verify all are still running
if ! kill -0 "$UI_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  error "Frontend port-forward failed to start."
fi
if ! kill -0 "$GMS_PID" 2>/dev/null; then
  kill "$UI_PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  error "GMS port-forward failed to start."
fi
if ! kill -0 "$KAFKA_PID" 2>/dev/null; then
  kill "$UI_PID" "$GMS_PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  error "Kafka port-forward failed to start."
fi

info "Port-forwards started in background."
echo ""
echo "  DataHub UI:       http://localhost:${UI_PORT}"
echo "  DataHub GMS:      http://localhost:${GMS_PORT}"
echo "  DataHub Kafka:    localhost:${KAFKA_PORT}"
echo "  Credentials:      datahub / datahub"
echo ""
echo "  PIDs: UI=$UI_PID, GMS=$GMS_PID, Kafka=$KAFKA_PID (saved to $PID_FILE)"
echo "  Stop with: $0 --stop"
echo ""
