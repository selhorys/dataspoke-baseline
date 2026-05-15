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
  error ".env not found at $ENV_FILE — run from dev_env/ and ensure .env exists."
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
use_context "${DATASPOKE_DEV_KUBE_CLUSTER}"

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
info "Installing ingress-nginx controller..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace "${NS}" \
  --values "$SCRIPT_DIR/values-dev.yaml" \
  --timeout 5m

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

# Write ingress IP/domain first; the comment block is written only on first append.
if grep -q "^DATASPOKE_DEV_INGRESS_IP=" "${ENV_FILE}"; then
  upsert_env_var DATASPOKE_DEV_INGRESS_IP "${EXTERNAL_IP}" "${ENV_FILE}"
else
  echo "" >> "${ENV_FILE}"
  echo "# --- Dev: nginx-ingress (written by nginx-ingress/install.sh) -------" >> "${ENV_FILE}"
  echo "DATASPOKE_DEV_INGRESS_IP=${EXTERNAL_IP}" >> "${ENV_FILE}"
fi
upsert_env_var DATASPOKE_DEV_INGRESS_DOMAIN "${INGRESS_DOMAIN}" "${ENV_FILE}"

# ---------------------------------------------------------------------------
# Derive and write runtime variables that depend on the ingress IP/domain.
# These are read by Python app code and integration tests via os.environ.
# ---------------------------------------------------------------------------

# Tier A: HTTP endpoints (use domain-based URLs)
upsert_env_var DATASPOKE_DATAHUB_GMS_URL      "http://datahub.${INGRESS_DOMAIN}/gms" "${ENV_FILE}"
upsert_env_var DATASPOKE_DATAHUB_KAFKA_BROKERS "${EXTERNAL_IP}:9005"                  "${ENV_FILE}"
upsert_env_var DATASPOKE_AIRFLOW_URL           "http://airflow.${INGRESS_DOMAIN}"      "${ENV_FILE}"

# Tier B: TCP endpoints (use IP directly)
upsert_env_var DATASPOKE_POSTGRES_HOST         "${EXTERNAL_IP}"                        "${ENV_FILE}"
upsert_env_var DATASPOKE_REDIS_HOST            "${EXTERNAL_IP}"                        "${ENV_FILE}"

# Example data sources
upsert_env_var DATASPOKE_EXAMPLE_PG_HOST       "${EXTERNAL_IP}"                        "${ENV_FILE}"
upsert_env_var DATASPOKE_EXAMPLE_KAFKA_BROKERS "${EXTERNAL_IP}:9104"                   "${ENV_FILE}"

info "Written to .env:"
info "  DATASPOKE_DEV_INGRESS_IP=${EXTERNAL_IP}"
info "  DATASPOKE_DEV_INGRESS_DOMAIN=${INGRESS_DOMAIN}"
info "  + 7 derived runtime variables (POSTGRES_HOST, REDIS_HOST, etc.)"

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
echo "  DataSpoke API: http://app.${INGRESS_DOMAIN}/api/v1/..."
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
