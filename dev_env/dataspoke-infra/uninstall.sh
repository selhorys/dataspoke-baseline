#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$SCRIPT_DIR/../lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/../.env" ]]; then
  error ".env not found at $SCRIPT_DIR/../.env"
fi
source "$SCRIPT_DIR/../.env"

echo ""
echo "=== Uninstalling DataSpoke infrastructure ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Uninstall Helm release
# ---------------------------------------------------------------------------
if helm status dataspoke --namespace "${NS}" >/dev/null 2>&1; then
  info "Uninstalling Helm release 'dataspoke' from namespace '${NS}'..."
  helm uninstall dataspoke --namespace "${NS}" --wait --timeout 60s 2>/dev/null \
    || warn "Helm uninstall timed out — force-deleting remaining pods."
else
  warn "Helm release 'dataspoke' not found in namespace '${NS}' — skipping."
fi

# Force-kill any pods still terminating
kubectl delete pod -n "${NS}" -l app.kubernetes.io/instance=dataspoke \
  --force --grace-period=0 2>/dev/null || true

# ---------------------------------------------------------------------------
# Delete PVCs for a clean slate
#
# Stale data in PVCs (cached embeddings, Redis
# keys) causes recovery loops and test pollution on reinstall.  Deleting
# PVCs is simpler and more reliable than trying to purge data in place.
# ---------------------------------------------------------------------------
info "Deleting PVCs..."
for pvc in $(kubectl get pvc -n "${NS}" -l app.kubernetes.io/instance=dataspoke \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  kubectl delete pvc "$pvc" -n "${NS}" 2>/dev/null \
    && info "  Deleted PVC '${pvc}'." \
    || warn "  Could not delete PVC '${pvc}'."
done

# ---------------------------------------------------------------------------
# Clean up secrets
# ---------------------------------------------------------------------------
for SECRET in dataspoke-postgres-secret dataspoke-redis-secret; do
  if kubectl get secret "${SECRET}" -n "${NS}" >/dev/null 2>&1; then
    info "Deleting secret '${SECRET}'..."
    kubectl delete secret "${SECRET}" -n "${NS}"
  fi
done

echo ""
info "DataSpoke infrastructure removed."
echo ""
