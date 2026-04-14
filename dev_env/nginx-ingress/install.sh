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
echo "=== Installing nginx-ingress controller ==="
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
# Add / update Helm repo
# ---------------------------------------------------------------------------
info "Adding/updating ingress-nginx Helm repository..."
if helm repo list 2>/dev/null | grep -q "^ingress-nginx"; then
  info "Helm repo 'ingress-nginx' already added — updating."
  helm repo update ingress-nginx
else
  helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
  helm repo update ingress-nginx
fi

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
NS="ingress-nginx"
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Namespace '${NS}' already exists."
else
  info "Creating namespace '${NS}'..."
  kubectl create namespace "${NS}"
fi

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
ENV_FILE="$SCRIPT_DIR/../.env"

# Update or append DATASPOKE_DEV_INGRESS_IP
if grep -q "^DATASPOKE_DEV_INGRESS_IP=" "${ENV_FILE}"; then
  sed -i.bak "s|^DATASPOKE_DEV_INGRESS_IP=.*|DATASPOKE_DEV_INGRESS_IP=${EXTERNAL_IP}|" "${ENV_FILE}"
else
  echo "" >> "${ENV_FILE}"
  echo "# --- Dev: nginx-ingress (written by nginx-ingress/install.sh) -------" >> "${ENV_FILE}"
  echo "DATASPOKE_DEV_INGRESS_IP=${EXTERNAL_IP}" >> "${ENV_FILE}"
fi

# Update or append DATASPOKE_DEV_INGRESS_DOMAIN
if grep -q "^DATASPOKE_DEV_INGRESS_DOMAIN=" "${ENV_FILE}"; then
  sed -i.bak "s|^DATASPOKE_DEV_INGRESS_DOMAIN=.*|DATASPOKE_DEV_INGRESS_DOMAIN=${INGRESS_DOMAIN}|" "${ENV_FILE}"
else
  echo "DATASPOKE_DEV_INGRESS_DOMAIN=${INGRESS_DOMAIN}" >> "${ENV_FILE}"
fi

# ---------------------------------------------------------------------------
# Derive and write runtime variables that depend on the ingress IP/domain.
# These are read by Python app code and integration tests via os.environ.
# ---------------------------------------------------------------------------
update_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    echo "${key}=${value}" >> "${ENV_FILE}"
  fi
}

# Tier A: HTTP endpoints (use domain-based URLs)
update_env "DATASPOKE_DATAHUB_GMS_URL"      "http://datahub.${INGRESS_DOMAIN}/gms"
update_env "DATASPOKE_DATAHUB_KAFKA_BROKERS" "${EXTERNAL_IP}:9005"
update_env "DATASPOKE_AIRFLOW_URL"           "http://airflow.${INGRESS_DOMAIN}"

# Tier B: TCP endpoints (use IP directly)
update_env "DATASPOKE_POSTGRES_HOST"         "${EXTERNAL_IP}"
update_env "DATASPOKE_REDIS_HOST"            "${EXTERNAL_IP}"
update_env "DATASPOKE_QDRANT_HOST"           "${EXTERNAL_IP}"

# Example data sources
update_env "DATASPOKE_EXAMPLE_PG_HOST"       "${EXTERNAL_IP}"
update_env "DATASPOKE_EXAMPLE_KAFKA_BROKERS" "${EXTERNAL_IP}:9104"

# Clean up sed backup files
rm -f "${ENV_FILE}.bak"

info "Written to .env:"
info "  DATASPOKE_DEV_INGRESS_IP=${EXTERNAL_IP}"
info "  DATASPOKE_DEV_INGRESS_DOMAIN=${INGRESS_DOMAIN}"
info "  + 8 derived runtime variables (POSTGRES_HOST, REDIS_HOST, etc.)"

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
echo "  Qdrant HTTP:     ${EXTERNAL_IP}:9203"
echo "  Qdrant gRPC:     ${EXTERNAL_IP}:9204"
echo "  DataHub Kafka:   ${EXTERNAL_IP}:9005"
echo "  Example PG:      ${EXTERNAL_IP}:9102"
echo "  Example Kafka:   ${EXTERNAL_IP}:9104"
echo "  Lock API:        ${EXTERNAL_IP}:9221"
echo ""
