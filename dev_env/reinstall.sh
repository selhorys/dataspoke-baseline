#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Selective component reinstall for the DataSpoke dev environment.
#
# Unlike uninstall.sh + install.sh (which tears down the entire umbrella
# release), this script reinstalls a single component — deleting its pods
# and any stale database state — then runs helm upgrade to bring it back.
#
# Usage:
#   ./dev_env/reinstall.sh --airflow     # Complete Airflow reinstall (incl. DB state)
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
    --airflow) COMPONENT="airflow" ;;
    -h|--help)
      echo "Usage: $0 --airflow"
      echo ""
      echo "Options:"
      echo "  --airflow    Complete Airflow reinstall (pods, DB state)"
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
# Component: airflow
# ---------------------------------------------------------------------------
reinstall_airflow() {
  echo ""
  echo "=== Reinstalling Airflow ==="
  echo ""

  # -- 1. Scale down dataspoke-api to prevent task callbacks during reinstall --
  info "Scaling down dataspoke-api..."
  kubectl scale deployment/dataspoke-api -n "${NS}" --replicas=0 2>/dev/null || true
  kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=30s 2>/dev/null || true

  # -- 2. Delete Airflow pods (webserver, scheduler, triggerer) --
  info "Deleting Airflow pods..."
  kubectl delete pod -n "${NS}" \
    -l "app.kubernetes.io/instance=dataspoke,release=dataspoke,chart=airflow" \
    --ignore-not-found 2>/dev/null || true

  # Also cover the official chart label selector
  kubectl delete pod -n "${NS}" \
    -l "release=dataspoke" \
    --field-selector='status.phase!=Running' \
    --ignore-not-found 2>/dev/null || true

  # -- 3. Drop and recreate the airflow database in PostgreSQL --
  #    Airflow stores DAG runs, task instances, connections, and variables in
  #    Postgres. Dropping the DB gives a clean slate without stale task state.
  info "Resetting Airflow database..."
  PG_POD=$(kubectl get pod -n "${NS}" \
    -l "app.kubernetes.io/name=postgresql,app.kubernetes.io/instance=dataspoke" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

  if [[ -n "$PG_POD" ]]; then
    PSQL_CMD="PGPASSWORD='${DATASPOKE_POSTGRES_PASSWORD}' psql -U ${DATASPOKE_POSTGRES_USER} -d postgres"
    # Terminate remaining connections
    kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
      "${PSQL_CMD} -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'airflow' AND pid <> pg_backend_pid();\"" \
      2>/dev/null || true
    if ! kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
      "${PSQL_CMD} -c 'DROP DATABASE IF EXISTS airflow;'" 2>/dev/null; then
      warn "DROP DATABASE failed — retrying after terminating connections..."
      kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
        "${PSQL_CMD} -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'airflow' AND pid <> pg_backend_pid();\"" \
        2>/dev/null || true
      kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
        "${PSQL_CMD} -c 'DROP DATABASE IF EXISTS airflow;'" \
        && info "  Dropped database 'airflow'." \
        || { error "  DROP DATABASE failed after retry."; }
    else
      info "  Dropped database 'airflow'."
    fi
    kubectl exec -n "${NS}" "$PG_POD" -- bash -c \
      "${PSQL_CMD} -c 'CREATE DATABASE airflow;'" \
      && info "  Created database 'airflow'."
  else
    warn "PostgreSQL pod not found — skipping DB reset. Airflow may hit stale state."
  fi

  # -- 4. Helm upgrade to recreate Airflow --
  info "Running helm upgrade to recreate Airflow..."
  bash "$SCRIPT_DIR/dataspoke-infra/install.sh"

  # -- 5. Wait for Airflow api-server to become ready (Airflow 3.x renamed webserver → api-server) --
  info "Waiting for Airflow api-server to become ready..."
  kubectl rollout status deployment/dataspoke-airflow-api-server -n "${NS}" --timeout=300s \
    && info "Airflow api-server is ready." \
    || warn "Airflow api-server did not become ready in time — check pod logs."

  # -- 6. Scale API back up --
  info "Scaling dataspoke-api back up..."
  kubectl scale deployment/dataspoke-api -n "${NS}" --replicas=1
  kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=120s \
    && info "dataspoke-api is ready." \
    || warn "dataspoke-api did not become ready in time."

  echo ""
  info "Airflow reinstall complete."
  if [[ -n "${DATASPOKE_DEV_INGRESS_DOMAIN:-}" ]]; then
    echo "  Airflow UI: http://airflow.${DATASPOKE_DEV_INGRESS_DOMAIN}/"
    echo "  Credentials: ${DATASPOKE_AIRFLOW_USER:-admin} / ${DATASPOKE_AIRFLOW_PASSWORD:-admin}"
  fi
  echo ""
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$COMPONENT" in
  airflow) reinstall_airflow ;;
  *) error "Unknown component: $COMPONENT" ;;
esac
