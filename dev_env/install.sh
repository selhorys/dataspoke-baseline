#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env — copy and edit it before running this script."
fi
source "$SCRIPT_DIR/.env"

echo ""
echo "=== Installing DataSpoke dev environment ==="
echo ""

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
command -v kubectl >/dev/null 2>&1 || error "kubectl is not installed or not in PATH."
command -v helm    >/dev/null 2>&1 || error "helm is not installed or not in PATH."
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Switch Kubernetes context
# ---------------------------------------------------------------------------
info "Switching to Kubernetes context: ${DATASPOKE_DEV_KUBE_CLUSTER}"
kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Create namespaces (idempotent)
# ---------------------------------------------------------------------------
NAMESPACES=(
  "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
  "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
  "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"
)

for NS in "${NAMESPACES[@]}"; do
  if kubectl get namespace "${NS}" >/dev/null 2>&1; then
    info "Namespace '${NS}' already exists — skipping."
  else
    info "Creating namespace '${NS}'..."
    kubectl create namespace "${NS}"
  fi
done

# ---------------------------------------------------------------------------
# Install DataHub
# ---------------------------------------------------------------------------
info "Running datahub/install.sh..."
bash "$SCRIPT_DIR/datahub/install.sh"

# ---------------------------------------------------------------------------
# Install DataSpoke infrastructure
# ---------------------------------------------------------------------------
info "Running dataspoke-infra/install.sh..."
bash "$SCRIPT_DIR/dataspoke-infra/install.sh"

# ---------------------------------------------------------------------------
# Install dataspoke-example sources
# ---------------------------------------------------------------------------
info "Running dataspoke-example/install.sh..."
bash "$SCRIPT_DIR/dataspoke-example/install.sh"

# ---------------------------------------------------------------------------
# Install lock service
# ---------------------------------------------------------------------------
info "Running dataspoke-lock/install.sh..."
bash "$SCRIPT_DIR/dataspoke-lock/install.sh"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Installation complete ==="
echo ""
echo "Namespaces:"
kubectl get namespaces "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}" \
  "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}" \
  "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}" 2>/dev/null || true
echo ""
echo "Port-forward scripts:"
echo ""
echo "  DataHub (UI + GMS):         ./datahub-port-forward.sh"
echo "  DataSpoke infra (PG, etc.): ./dataspoke-port-forward.sh"
echo "  Example sources:            ./dummy-data-port-forward.sh"
echo "  Lock service:               ./lock-port-forward.sh"
echo ""
echo "DataHub UI:  http://localhost:${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_UI_PORT:-9002}"
echo "DataHub GMS: http://localhost:${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_GMS_PORT:-9004}"
echo "Credentials: datahub / datahub"
echo ""
echo "DataSpoke infrastructure (after port-forward):"
echo "  PostgreSQL: localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT:-9201}"
echo "  Redis:      localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT:-9202}"
echo "  Qdrant:     localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT:-9203} (HTTP), :${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_GRPC_PORT:-9204} (gRPC)"
echo "  Temporal:   localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_TEMPORAL_PORT:-9205}"
echo "  Lock API:   localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_DEV_ENV_LOCK_PORT:-9221}"
echo ""
echo "Run app services locally:"
echo "  source .env"
echo "  cd .. && uv sync"
echo "  uv run uvicorn src.api.main:app --reload --port 8000"
echo ""
