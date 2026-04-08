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

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
CHART_DIR="$SCRIPT_DIR/../../helm-charts/dataspoke"

echo ""
echo "=== Installing DataSpoke infrastructure ==="
echo ""

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
command -v kubectl >/dev/null 2>&1 || error "kubectl is not installed or not in PATH."
command -v helm    >/dev/null 2>&1 || error "helm is not installed or not in PATH."
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Namespace '${NS}' already exists."
else
  info "Creating namespace '${NS}'..."
  kubectl create namespace "${NS}"
fi

# ---------------------------------------------------------------------------
# Create secrets from .env variables (idempotent)
# ---------------------------------------------------------------------------
info "Creating dataspoke-postgres-secret..."
kubectl create secret generic dataspoke-postgres-secret \
  --namespace "${NS}" \
  --from-literal=POSTGRES_USER="${DATASPOKE_POSTGRES_USER}" \
  --from-literal=POSTGRES_PASSWORD="${DATASPOKE_POSTGRES_PASSWORD}" \
  --from-literal=POSTGRES_DB="${DATASPOKE_POSTGRES_DB}" \
  --dry-run=client -o yaml | kubectl apply -f -

info "Creating dataspoke-redis-secret..."
kubectl create secret generic dataspoke-redis-secret \
  --namespace "${NS}" \
  --from-literal=REDIS_PASSWORD="${DATASPOKE_REDIS_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -

if [[ -n "${DATASPOKE_QDRANT_API_KEY:-}" ]]; then
  info "Creating dataspoke-qdrant-secret..."
  kubectl create secret generic dataspoke-qdrant-secret \
    --namespace "${NS}" \
    --from-literal=QDRANT_API_KEY="${DATASPOKE_QDRANT_API_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  info "DATASPOKE_QDRANT_API_KEY not set — skipping qdrant secret."
fi

# ---------------------------------------------------------------------------
# Register required Helm repositories (idempotent)
# ---------------------------------------------------------------------------
add_repo_if_missing() {
  local name="$1" url="$2"
  if helm repo list 2>/dev/null | grep -q "^${name}"; then
    info "Helm repo '${name}' already added."
  else
    info "Adding Helm repo '${name}' (${url})..."
    helm repo add "${name}" "${url}"
  fi
}

info "Adding/updating Helm repositories..."
add_repo_if_missing bitnami  "https://charts.bitnami.com/bitnami"
add_repo_if_missing qdrant   "https://qdrant.github.io/qdrant-helm"
add_repo_if_missing kestra   "https://helm.kestra.io/"
helm repo update

# ---------------------------------------------------------------------------
# Build chart dependencies
# ---------------------------------------------------------------------------
if [[ -d "$CHART_DIR" ]]; then
  info "Building Helm chart dependencies..."
  helm dependency build "$CHART_DIR"
fi

# ---------------------------------------------------------------------------
# Install via umbrella Helm chart with dev profile
# ---------------------------------------------------------------------------
if [[ -d "$CHART_DIR" ]]; then
  info "Installing DataSpoke infra via Helm chart at $CHART_DIR..."
  helm upgrade --install dataspoke "$CHART_DIR" \
    -f "$CHART_DIR/values-dev.yaml" \
    -n "${NS}" \
    --set postgresql.auth.existingSecret=dataspoke-postgres-secret \
    --set postgresql.auth.username="${DATASPOKE_POSTGRES_USER}" \
    --set postgresql.auth.database="${DATASPOKE_POSTGRES_DB}" \
    --set redis.auth.existingSecret=dataspoke-redis-secret \
    --set kestra.configurations.application.datasources.postgres.username="${DATASPOKE_POSTGRES_USER}" \
    --set kestra.configurations.application.datasources.postgres.password="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set global.postgresql.auth.password="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set api.image.repository="${DATASPOKE_DEV_IMAGE_REGISTRY}/api" \
    --set api.image.tag=dev \
    --set-string secrets.postgres.user="${DATASPOKE_POSTGRES_USER}" \
    --set-string secrets.postgres.password="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set-string secrets.redis.password="${DATASPOKE_REDIS_PASSWORD}" \
    --set-string secrets.datahub.token="${DATASPOKE_DATAHUB_TOKEN:-}" \
    --set-string secrets.kestra.user="${DATASPOKE_KESTRA_USER:-}" \
    --set-string secrets.kestra.password="${DATASPOKE_KESTRA_PASSWORD:-}" \
    --set-string secrets.llm.apiKey="${DATASPOKE_LLM_API_KEY:-}" \
    --set-string config.kestra.callbackBaseUrl="http://dataspoke-api:8002" \
    --set-string config.datahub.gmsUrl="http://datahub-datahub-gms.${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}.svc.cluster.local:8080" \
    --set-string config.datahub.kafkaBrokers="datahub-prerequisites-kafka.${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}.svc.cluster.local:9092" \
    --timeout 5m --wait
else
  warn "Helm chart not found at $CHART_DIR — skipping Helm install."
  warn "DataSpoke infrastructure must be installed manually or the chart must be created first."
fi

# ---------------------------------------------------------------------------
# Wait for Kestra to become ready
# ---------------------------------------------------------------------------
info "Waiting for Kestra to become ready..."
kubectl rollout status deployment/dataspoke-kestra-standalone -n "${NS}" --timeout=120s \
  && info "Kestra is ready." \
  || warn "Kestra did not become ready in time — check pod logs."

# ---------------------------------------------------------------------------
# Print access instructions
# ---------------------------------------------------------------------------
echo ""
info "DataSpoke infrastructure installation complete."
kubectl get pods -n "${NS}" 2>/dev/null || true
echo ""
echo "Port-forward with:  ../dataspoke-port-forward.sh"
echo ""
echo "  PostgreSQL: localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_POSTGRES_PORT:-9201}"
echo "  Redis:      localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_REDIS_PORT:-9202}"
echo "  Qdrant:     localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_HTTP_PORT:-9203} (HTTP), :${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_QDRANT_GRPC_PORT:-9204} (gRPC)"
echo "  Kestra:     localhost:${DATASPOKE_DEV_KUBE_DATASPOKE_PORT_FORWARD_KESTRA_PORT:-9205} (API + UI)"
echo ""
