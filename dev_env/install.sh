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
# Install nginx-ingress controller
# ---------------------------------------------------------------------------
info "Running nginx-ingress/install.sh..."
bash "$SCRIPT_DIR/nginx-ingress/install.sh"

# Re-source .env to pick up DATASPOKE_DEV_INGRESS_IP and DATASPOKE_DEV_INGRESS_DOMAIN
source "$SCRIPT_DIR/.env"

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
# Install DataSpoke Langfuse (LLM observability)
# Must run after dataspoke-infra (Postgres + Redis must be up) and before
# any API/Airflow containers start so they pick up Langfuse env at launch.
# ---------------------------------------------------------------------------
info "Running dataspoke-langfuse/install.sh..."
bash "$SCRIPT_DIR/dataspoke-langfuse/install.sh"

# Re-source .env to pick up DATASPOKE_LANGFUSE_HOST written by install.sh
source "$SCRIPT_DIR/.env"

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
echo "Ingress endpoints (via nginx-ingress at ${DATASPOKE_DEV_INGRESS_IP:-<not set>}):"
echo ""
echo "  DataHub UI:    http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/"
echo "  DataHub GMS:   http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/gms/"
echo "  DataSpoke API: http://app.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/api/v1/"
echo "  Airflow UI:    http://airflow.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/"
echo "  Langfuse UI:   ${DATASPOKE_LANGFUSE_HOST:-http://langfuse.<not set>}/"
echo ""
echo "  PostgreSQL:    ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9201"
echo "  Redis:         ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9202"
echo "  DataHub Kafka: ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9005"
echo "  Example PG:    ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9102"
echo "  Example Kafka: ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9104"
echo "  Lock API:      ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9221"
echo ""
echo "  Credentials:"
echo "    DataHub:  datahub / datahub"
echo "    Airflow:  ${DATASPOKE_AIRFLOW_USER:-admin} / ${DATASPOKE_AIRFLOW_PASSWORD:-admin}"
echo ""
echo "Environment:"
echo "  .env has been populated with ingress-derived variables."
echo "  Run 'source .env' to load them into your shell."
echo ""
echo "API is deployed in-cluster. To rebuild after code changes:"
echo "  ./dataspoke-test-mode.sh"
echo ""
echo "Seed dummy data:"
echo "  cd .. && uv sync"
echo "  uv run python -m tests.integration.util --reset-seed"
echo ""
