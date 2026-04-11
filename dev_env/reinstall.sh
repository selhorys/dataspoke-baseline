#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Selective component reinstall for the DataSpoke dev environment.
#
# Unlike uninstall.sh + install.sh (which tears down the entire umbrella
# release), this script reinstalls a single component — deleting its pods,
# PVCs, and any stale state — then runs helm upgrade to bring it back.
#
# Usage:
#   ./dev_env/reinstall.sh --kestra     # Complete Kestra reinstall (incl. PVC + DB)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
COMPONENT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kestra) COMPONENT="kestra" ;;
    -h|--help)
      echo "Usage: $0 --kestra"
      echo ""
      echo "Options:"
      echo "  --kestra    Complete Kestra reinstall (pods, PVCs, DB data)"
      exit 0
      ;;
    *) error "Unknown option: $1. Use --help for usage." ;;
  esac
  shift
done

if [[ -z "$COMPONENT" ]]; then
  error "No component specified. Use --help for usage."
fi

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env"
fi
source "$SCRIPT_DIR/.env"

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
CHART_DIR="$SCRIPT_DIR/../helm-charts/dataspoke"

kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}" >/dev/null 2>&1

# ---------------------------------------------------------------------------
# Component: kestra
# ---------------------------------------------------------------------------
reinstall_kestra() {
  echo ""
  echo "=== Reinstalling Kestra ==="
  echo ""

  # -- 1. Scale down dataspoke-api to release Kestra DB connections --
  info "Scaling down dataspoke-api..."
  kubectl scale deployment/dataspoke-api -n "${NS}" --replicas=0 2>/dev/null || true
  kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=30s 2>/dev/null || true

  # -- 2. Delete Kestra deployment + service (keeps the rest of the release) --
  info "Deleting Kestra deployment..."
  kubectl delete deployment -n "${NS}" -l "app.kubernetes.io/instance=dataspoke,app.kubernetes.io/name=kestra" \
    --ignore-not-found --wait=false

  info "Force-killing Kestra pods..."
  kubectl delete pod -n "${NS}" -l "app.kubernetes.io/instance=dataspoke,app.kubernetes.io/name=kestra" \
    --force --grace-period=0 2>/dev/null || true

  # -- 3. Delete Kestra PVCs (storage data) --
  info "Deleting Kestra PVCs..."
  for pvc in $(kubectl get pvc -n "${NS}" \
      -l "app.kubernetes.io/instance=dataspoke,app.kubernetes.io/name=kestra" \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    kubectl delete pvc "$pvc" -n "${NS}" 2>/dev/null \
      && info "  Deleted PVC '${pvc}'." \
      || warn "  Could not delete PVC '${pvc}'."
  done

  # -- 4. Drop and recreate the kestra database in PostgreSQL --
  #    Kestra stores service_instance, queue, trigger, and flow state in
  #    Postgres.  Leaving stale trigger_execution_id rows causes schedule
  #    recovery loops (recoverMissedSchedules=ALL default) on fresh startup,
  #    leading to JVM overload.  The API is scaled down first to ensure no
  #    lingering connections block the DROP.
  info "Resetting Kestra database..."
  PG_POD=$(kubectl get pod -n "${NS}" -l "app.kubernetes.io/name=postgresql,app.kubernetes.io/instance=dataspoke" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

  if [[ -n "$PG_POD" ]]; then
    PSQL_CMD="PGPASSWORD='${DATASPOKE_POSTGRES_PASSWORD}' psql -U ${DATASPOKE_POSTGRES_USER} -d postgres"
    # Terminate remaining connections (monitor scripts, stale pools)
    kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
      "${PSQL_CMD} -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'kestra' AND pid <> pg_backend_pid();\"" \
      2>/dev/null || true
    sleep 1
    if ! kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
      "${PSQL_CMD} -c 'DROP DATABASE IF EXISTS kestra;'" 2>/dev/null; then
      warn "DROP DATABASE failed — retrying after terminating connections..."
      kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
        "${PSQL_CMD} -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'kestra' AND pid <> pg_backend_pid();\"" \
        2>/dev/null || true
      sleep 2
      kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
        "${PSQL_CMD} -c 'DROP DATABASE IF EXISTS kestra;'" \
        && info "  Dropped database 'kestra'." \
        || { error "  DROP DATABASE failed after retry. Connections may still be open."; }
    else
      info "  Dropped database 'kestra'."
    fi
    kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
      "${PSQL_CMD} -c 'CREATE DATABASE kestra;'" \
      && info "  Created database 'kestra'."
  else
    warn "PostgreSQL pod not found — skipping DB reset. Kestra may hit stale state."
  fi

  # -- 5. Helm upgrade to recreate Kestra (keep API scaled down) --
  #    We keep the API at 0 replicas during Kestra startup to prevent
  #    the API's lifespan/background tasks from hitting Kestra during
  #    JVM warm-up (which causes a GC death spiral at 2.4 cores / 4.5Gi).
  info "Running helm upgrade to recreate Kestra..."
  bash "$SCRIPT_DIR/dataspoke-infra/install.sh"

  # Scale API back down — helm upgrade may have restored it
  info "Keeping dataspoke-api scaled down during Kestra warm-up..."
  kubectl scale deployment/dataspoke-api -n "${NS}" --replicas=0 2>/dev/null || true

  # -- 6. Wait for Kestra to be fully ready --
  info "Waiting for Kestra to become ready..."
  kubectl rollout status deployment/dataspoke-kestra-standalone -n "${NS}" --timeout=300s \
    && info "Kestra is ready." \
    || warn "Kestra did not become ready in time — check pod logs."

  # -- 7. Scale API back up now that Kestra is warm --
  info "Scaling dataspoke-api back up..."
  kubectl scale deployment/dataspoke-api -n "${NS}" --replicas=1
  kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=120s \
    && info "dataspoke-api is ready." \
    || warn "dataspoke-api did not become ready in time."

  echo ""
  info "Kestra reinstall complete."
  if [[ -n "${DATASPOKE_DEV_INGRESS_DOMAIN:-}" ]]; then
    echo "  Kestra UI: http://kestra.${DATASPOKE_DEV_INGRESS_DOMAIN}/"
  fi
  echo ""
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$COMPONENT" in
  kestra) reinstall_kestra ;;
  *) error "Unknown component: $COMPONENT" ;;
esac
