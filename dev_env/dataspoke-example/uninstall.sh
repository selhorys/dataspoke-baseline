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
echo "=== Uninstalling dataspoke-example ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

# ---------------------------------------------------------------------------
# Delete manifests
# ---------------------------------------------------------------------------
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Deleting manifests from namespace '${NS}'..."
  kubectl delete -f "$SCRIPT_DIR/manifests/" --namespace "${NS}" --ignore-not-found=true

  # Clean up secret created outside manifests/ by install.sh
  if kubectl get secret/example-postgres-secret -n "${NS}" >/dev/null 2>&1; then
    info "Deleting secret/example-postgres-secret..."
    kubectl delete secret/example-postgres-secret -n "${NS}"
  fi
else
  warn "Namespace '${NS}' does not exist — nothing to delete."
fi

echo ""
info "dataspoke-example resources removed."
echo ""
