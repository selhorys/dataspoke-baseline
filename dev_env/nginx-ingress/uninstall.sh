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
  error ".env not found at $SCRIPT_DIR/../.env — run from dev_env/ and ensure .env exists."
fi
source "$SCRIPT_DIR/../.env"

echo ""
echo "=== Uninstalling nginx-ingress controller ==="
echo ""

# ---------------------------------------------------------------------------
# Switch Kubernetes context
# ---------------------------------------------------------------------------
info "Switching to Kubernetes context: ${DATASPOKE_DEV_KUBE_CLUSTER}"
kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Uninstall Helm release
# ---------------------------------------------------------------------------
NS="ingress-nginx"

if helm status ingress-nginx -n "${NS}" >/dev/null 2>&1; then
  info "Uninstalling ingress-nginx Helm release..."
  helm uninstall ingress-nginx -n "${NS}"
  info "Helm release uninstalled."
else
  warn "Helm release 'ingress-nginx' not found in namespace '${NS}' — skipping."
fi

# ---------------------------------------------------------------------------
# Delete namespace
# ---------------------------------------------------------------------------
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Deleting namespace '${NS}'..."
  kubectl delete namespace "${NS}"
  info "Namespace '${NS}' deleted."
else
  warn "Namespace '${NS}' does not exist — skipping."
fi

echo ""
info "nginx-ingress controller uninstalled."
echo ""
