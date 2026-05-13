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
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE"
fi
source "$ENV_FILE"

echo ""
echo "=== Uninstalling Langfuse subsystem ==="
echo ""

LANGFUSE_NS="${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
DATASPOKE_NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Uninstall Helm release
# ---------------------------------------------------------------------------
if helm status langfuse --namespace "${LANGFUSE_NS}" >/dev/null 2>&1; then
  info "Uninstalling Helm release 'langfuse' from namespace '${LANGFUSE_NS}'..."
  helm uninstall langfuse --namespace "${LANGFUSE_NS}" --wait --timeout 60s 2>/dev/null \
    || warn "Helm uninstall timed out — force-deleting remaining pods."
else
  warn "Helm release 'langfuse' not found in namespace '${LANGFUSE_NS}' — skipping."
fi

# Force-kill any pods still terminating
kubectl delete pod -n "${LANGFUSE_NS}" \
  -l app.kubernetes.io/instance=langfuse \
  --force --grace-period=0 2>/dev/null || true

# ---------------------------------------------------------------------------
# Delete PVCs in langfuse-01
# ---------------------------------------------------------------------------
info "Deleting Langfuse PVCs in ${LANGFUSE_NS}..."
for pvc in $(kubectl get pvc -n "${LANGFUSE_NS}" \
    -l app.kubernetes.io/instance=langfuse \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  kubectl delete pvc "$pvc" -n "${LANGFUSE_NS}" 2>/dev/null \
    && info "  Deleted PVC '${pvc}'." \
    || warn "  Could not delete PVC '${pvc}'."
done

# ---------------------------------------------------------------------------
# Delete dataspoke-langfuse-secret in langfuse-01
# ---------------------------------------------------------------------------
if kubectl get secret dataspoke-langfuse-secret -n "${LANGFUSE_NS}" >/dev/null 2>&1; then
  info "Deleting secret 'dataspoke-langfuse-secret' from ${LANGFUSE_NS}..."
  kubectl delete secret dataspoke-langfuse-secret -n "${LANGFUSE_NS}"
else
  info "Secret 'dataspoke-langfuse-secret' not found in ${LANGFUSE_NS} — skipping."
fi

# ---------------------------------------------------------------------------
# Delete the consumer-side secret mirror in dataspoke-01
# This secret holds only LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY for the
# umbrella chart's existingSecretKeyRef.
# ---------------------------------------------------------------------------
if kubectl get secret dataspoke-langfuse-secret -n "${DATASPOKE_NS}" >/dev/null 2>&1; then
  info "Deleting consumer-side secret 'dataspoke-langfuse-secret' from ${DATASPOKE_NS}..."
  kubectl delete secret dataspoke-langfuse-secret -n "${DATASPOKE_NS}"
else
  info "Consumer-side secret 'dataspoke-langfuse-secret' not found in ${DATASPOKE_NS} — skipping."
fi

echo ""
info "Langfuse subsystem removed."
echo ""
