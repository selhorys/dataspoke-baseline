#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$SCRIPT_DIR/../lib/helpers.sh"

# ---------------------------------------------------------------------------
# Parse flags
#   --drop-db   also drop the `langfuse` database from shared Postgres
# ---------------------------------------------------------------------------
DROP_DB=false
for arg in "$@"; do
  case "$arg" in
    --drop-db) DROP_DB=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE"
fi
source "$ENV_FILE"

echo ""
echo "=== Uninstalling DataSpoke Langfuse subsystem ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Uninstall Helm release
# ---------------------------------------------------------------------------
if helm status dataspoke-langfuse --namespace "${NS}" >/dev/null 2>&1; then
  info "Uninstalling Helm release 'dataspoke-langfuse' from namespace '${NS}'..."
  helm uninstall dataspoke-langfuse --namespace "${NS}" --wait --timeout 60s 2>/dev/null \
    || warn "Helm uninstall timed out — force-deleting remaining pods."
else
  warn "Helm release 'dataspoke-langfuse' not found in namespace '${NS}' — skipping."
fi

# Force-kill any pods still terminating
kubectl delete pod -n "${NS}" \
  -l app.kubernetes.io/instance=dataspoke-langfuse \
  --force --grace-period=0 2>/dev/null || true

# ---------------------------------------------------------------------------
# Delete PVCs
# ---------------------------------------------------------------------------
info "Deleting Langfuse PVCs..."
for pvc in $(kubectl get pvc -n "${NS}" \
    -l app.kubernetes.io/instance=dataspoke-langfuse \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  kubectl delete pvc "$pvc" -n "${NS}" 2>/dev/null \
    && info "  Deleted PVC '${pvc}'." \
    || warn "  Could not delete PVC '${pvc}'."
done

# ---------------------------------------------------------------------------
# Delete secret
# ---------------------------------------------------------------------------
if kubectl get secret dataspoke-langfuse-secret -n "${NS}" >/dev/null 2>&1; then
  info "Deleting secret 'dataspoke-langfuse-secret'..."
  kubectl delete secret dataspoke-langfuse-secret -n "${NS}"
else
  info "Secret 'dataspoke-langfuse-secret' not found — skipping."
fi

# ---------------------------------------------------------------------------
# Optionally drop the langfuse database from shared Postgres
# ---------------------------------------------------------------------------
if [[ "${DROP_DB}" == true ]]; then
  info "Dropping langfuse database from shared Postgres..."
  kubectl exec -n "${NS}" dataspoke-postgresql-0 -- \
    env PGPASSWORD="${DATASPOKE_POSTGRES_PASSWORD}" \
    psql -U "${DATASPOKE_POSTGRES_USER:-dataspoke}" -d postgres \
    -c 'DROP DATABASE IF EXISTS langfuse;' \
    && info "  langfuse database dropped." \
    || warn "  Could not drop langfuse database — drop it manually if needed."
fi

echo ""
info "DataSpoke Langfuse subsystem removed."
echo ""
