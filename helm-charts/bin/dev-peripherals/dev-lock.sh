#!/usr/bin/env bash
# Install the dev-lock advisory mutex service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$(cd "$BIN_DIR/.." && pwd)/.env.dev}"
PERIPHERALS_DIR="$(cd "$BIN_DIR/../dev-peripherals" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example and edit it."
fi
source "$ENV_FILE"

echo ""
echo "=== Installing dev-lock service ==="
echo ""

NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "${NS}"

# ---------------------------------------------------------------------------
# Apply manifests
# ---------------------------------------------------------------------------
info "Applying lock service manifests to namespace '${NS}'..."
kubectl apply -f "$PERIPHERALS_DIR/dev-lock/manifests/" --namespace "${NS}"

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
# In shared mode the lock API is reached on 127.0.0.1 via port-forward
# (bin/port-forward.sh); in managed mode it rides the ingress LoadBalancer IP.
INGRESS_IP="$(tcp_access_host)"
[[ -z "${INGRESS_IP}" ]] && INGRESS_IP="<ingress-ip>"
echo ""
info "dev-lock installation complete."
echo ""
echo "  Lock API: ${INGRESS_IP}:9221  (-> dev-lock:8080)"
echo ""
echo "  GET    http://${INGRESS_IP}:9221/lock              # status"
echo "  POST   http://${INGRESS_IP}:9221/lock/acquire      # acquire"
echo "  POST   http://${INGRESS_IP}:9221/lock/release      # release"
echo "  DELETE http://${INGRESS_IP}:9221/lock              # force-release"
echo ""
