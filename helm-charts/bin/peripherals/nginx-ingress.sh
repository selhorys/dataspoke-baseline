#!/usr/bin/env bash
# Install the nginx-ingress controller and write the assigned external IP/domain
# back to helm-charts/.env for downstream scripts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$(cd "$BIN_DIR/.." && pwd)/.env"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE — copy helm-charts/.env.example and edit it."
fi
source "$ENV_FILE"

echo ""
echo "=== Installing nginx-ingress controller ==="
echo ""

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Switch Kubernetes context
# ---------------------------------------------------------------------------
use_context "${DATASPOKE_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Add / update Helm repo
# ---------------------------------------------------------------------------
info "Adding/updating ingress-nginx Helm repository..."
helm_repo_add_if_missing ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update ingress-nginx

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "ingress-nginx"
NS="ingress-nginx"

# ---------------------------------------------------------------------------
# Install / upgrade ingress-nginx
# ---------------------------------------------------------------------------
PERIPHERALS_DIR="$(cd "$BIN_DIR/../peripherals" && pwd)"
info "Installing ingress-nginx controller..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace "${NS}" \
  --values "$PERIPHERALS_DIR/nginx-ingress/values-dev.yaml" \
  --timeout 5m

# ---------------------------------------------------------------------------
# Wait for the controller Deployment to roll out.
#
# On GKE Autopilot the cluster scales from 0 nodes, so the controller pod and
# its backing node can take a few minutes to come up. Block on the rollout
# condition (not a fixed sleep) before polling for the LoadBalancer IP, which
# GCP only assigns once the controller has a schedulable node.
# ---------------------------------------------------------------------------
info "Waiting for ingress-nginx-controller rollout (up to 5m)..."
kubectl rollout status deployment/ingress-nginx-controller \
  -n "${NS}" --timeout=5m

# ---------------------------------------------------------------------------
# Wait for LoadBalancer external IP (poll up to 120s)
# ---------------------------------------------------------------------------
info "Waiting for LoadBalancer external IP (up to 120s)..."
EXTERNAL_IP=""
ELAPSED=0
TIMEOUT=120

while [[ -z "${EXTERNAL_IP}" && ${ELAPSED} -lt ${TIMEOUT} ]]; do
  EXTERNAL_IP=$(kubectl get svc ingress-nginx-controller \
    -n "${NS}" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)

  if [[ -z "${EXTERNAL_IP}" ]]; then
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    if (( ELAPSED % 30 == 0 )); then
      info "  Still waiting... (${ELAPSED}s elapsed)"
    fi
  fi
done

if [[ -z "${EXTERNAL_IP}" ]]; then
  error "LoadBalancer did not receive an external IP within ${TIMEOUT}s. Check GKE firewall rules and Autopilot quotas."
fi

info "External IP assigned: ${EXTERNAL_IP}"

# ---------------------------------------------------------------------------
# Compute domain and write to .env
# ---------------------------------------------------------------------------
INGRESS_DOMAIN="${EXTERNAL_IP}.nip.io"

# Write ingress IP/domain
upsert_env_var DATASPOKE_KUBE_INGRESS_IP     "${EXTERNAL_IP}"     "${ENV_FILE}"
upsert_env_var DATASPOKE_KUBE_INGRESS_DOMAIN "${INGRESS_DOMAIN}"  "${ENV_FILE}"

# ---------------------------------------------------------------------------
# Derive and write runtime variables that depend on the ingress IP/domain.
# ---------------------------------------------------------------------------

# Tier A: HTTP endpoints (use domain-based URLs)
upsert_env_var DATASPOKE_TEST_AIRFLOW_URL      "http://airflow.${INGRESS_DOMAIN}"      "${ENV_FILE}"

# Tier B: TCP endpoints (use IP directly)
upsert_env_var DATASPOKE_TEST_POSTGRES_HOST    "${EXTERNAL_IP}"                        "${ENV_FILE}"
upsert_env_var DATASPOKE_TEST_REDIS_HOST       "${EXTERNAL_IP}"                        "${ENV_FILE}"

# Example data sources
upsert_env_var DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST "${EXTERNAL_IP}"                "${ENV_FILE}"
upsert_env_var DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS "${EXTERNAL_IP}:9104"           "${ENV_FILE}"

info "Written to .env:"
info "  DATASPOKE_KUBE_INGRESS_IP=${EXTERNAL_IP}"
info "  DATASPOKE_KUBE_INGRESS_DOMAIN=${INGRESS_DOMAIN}"
info "  + 5 derived test variables (DATASPOKE_TEST_POSTGRES_HOST, DATASPOKE_TEST_REDIS_HOST, etc.)"

# ---------------------------------------------------------------------------
# Print access summary
# ---------------------------------------------------------------------------
echo ""
info "nginx-ingress controller is ready."
kubectl get pods -n "${NS}"
echo ""
echo "Ingress external IP: ${EXTERNAL_IP}"
echo ""
echo "HTTP endpoints (Tier A):"
echo "  DataSpoke UI:  http://app.${INGRESS_DOMAIN}/"
echo "  DataSpoke API: http://api.${INGRESS_DOMAIN}/api/v1/..."
echo "  DataHub UI:    http://datahub.${INGRESS_DOMAIN}/"
echo "  DataHub GMS:   http://datahub.${INGRESS_DOMAIN}/gms/..."
echo "  Airflow UI:    http://airflow.${INGRESS_DOMAIN}/"
echo ""
echo "TCP endpoints (Tier B):"
echo "  PostgreSQL:      ${EXTERNAL_IP}:9201"
echo "  Redis:           ${EXTERNAL_IP}:9202"
echo "  DataHub Kafka:   ${EXTERNAL_IP}:9005"
echo "  Example PG:      ${EXTERNAL_IP}:9102"
echo "  Example Kafka:   ${EXTERNAL_IP}:9104"
echo "  Lock API:        ${EXTERNAL_IP}:9221"
echo ""
