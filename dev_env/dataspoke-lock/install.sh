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
echo "=== Installing dev-env lock service ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "${NS}"

# ---------------------------------------------------------------------------
# Apply manifests
# ---------------------------------------------------------------------------
info "Applying lock service manifests to namespace '${NS}'..."
kubectl apply -f "$SCRIPT_DIR/manifests/" --namespace "${NS}"

# ---------------------------------------------------------------------------
# Wait for rollout
# ---------------------------------------------------------------------------
info "Waiting for dev-lock deployment to be ready (timeout: 2m)..."
kubectl rollout status deployment/dev-lock \
  --namespace "${NS}" \
  --timeout=2m

# ---------------------------------------------------------------------------
# Print access info
# ---------------------------------------------------------------------------
echo ""
info "Lock service installation complete."
echo ""
echo "  Lock API: ${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9221  (-> dev-lock:8080)"
echo ""
echo "  GET    http://${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9221/lock              # status"
echo "  POST   http://${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9221/lock/acquire      # acquire"
echo "  POST   http://${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9221/lock/release      # release"
echo "  DELETE http://${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9221/lock              # force-release"
echo ""
