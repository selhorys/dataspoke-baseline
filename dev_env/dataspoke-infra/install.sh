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

# Generate internal-auth token for Airflow→API calls (idempotent: skip if secret exists)
if kubectl -n "${NS}" get secret dataspoke-internal-auth >/dev/null 2>&1; then
  info "dataspoke-internal-auth already exists — skipping."
else
  info "Generating dataspoke-internal-auth token..."
  token="$(openssl rand -hex 32)"
  kubectl -n "${NS}" create secret generic dataspoke-internal-auth \
    --from-literal=token="${token}"
  info "dataspoke-internal-auth created."
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
add_repo_if_missing bitnami         "https://charts.bitnami.com/bitnami"
add_repo_if_missing qdrant          "https://qdrant.github.io/qdrant-helm"
add_repo_if_missing apache-airflow  "https://airflow.apache.org"
helm repo update

# ---------------------------------------------------------------------------
# Build chart dependencies
# ---------------------------------------------------------------------------
if [[ -d "$CHART_DIR" ]]; then
  info "Building Helm chart dependencies..."
  helm dependency build "$CHART_DIR"
fi

# ---------------------------------------------------------------------------
# Build + push the custom Airflow image (DAGs baked in). Idempotent — the
# existing :dev tag is overwritten. Skip with SKIP_AIRFLOW_BUILD=1 when
# iterating on non-DAG changes.
# ---------------------------------------------------------------------------
if [[ "${SKIP_AIRFLOW_BUILD:-0}" != "1" ]]; then
  info "Building DataSpoke Airflow image (DAGs baked in)..."
  bash "$SCRIPT_DIR/../dataspoke-airflow/build.sh" dev
else
  info "SKIP_AIRFLOW_BUILD=1 set — skipping Airflow image build."
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
    --set airflow.data.metadataConnection.user="${DATASPOKE_POSTGRES_USER}" \
    --set airflow.data.metadataConnection.pass="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set global.postgresql.auth.password="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set api.image.repository="${DATASPOKE_DEV_IMAGE_REGISTRY}/api" \
    --set api.image.tag=dev \
    --set-string airflow.images.airflow.repository="${DATASPOKE_DEV_IMAGE_REGISTRY}/airflow" \
    --set-string airflow.images.airflow.tag=dev \
    --set airflow.images.airflow.pullPolicy=Always \
    --set-string secrets.postgres.user="${DATASPOKE_POSTGRES_USER}" \
    --set-string secrets.postgres.password="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set-string secrets.redis.password="${DATASPOKE_REDIS_PASSWORD}" \
    --set-string secrets.datahub.token="${DATASPOKE_DATAHUB_TOKEN:-}" \
    --set-string secrets.airflow.user="${DATASPOKE_AIRFLOW_USER:-admin}" \
    --set-string secrets.airflow.password="${DATASPOKE_AIRFLOW_PASSWORD:-admin}" \
    --set-string secrets.llm.apiKey="${DATASPOKE_LLM_API_KEY:-}" \
    --set-string config.airflow.callbackBaseUrl="http://dataspoke-api:8002" \
    --set-string config.datahub.gmsUrl="http://datahub-datahub-gms.${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}.svc.cluster.local:8080" \
    --set-string config.datahub.kafkaBrokers="datahub-prerequisites-kafka.${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}.svc.cluster.local:9092" \
    --set "api.ingress.hosts[0].host=app.${DATASPOKE_DEV_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
    --set "api.ingress.hosts[0].paths[0].path=/" \
    --set "api.ingress.hosts[0].paths[0].pathType=Prefix" \
    --set "airflow.ingress.apiServer.hosts[0].name=airflow.${DATASPOKE_DEV_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
    --timeout 10m
else
  warn "Helm chart not found at $CHART_DIR — skipping Helm install."
  warn "DataSpoke infrastructure must be installed manually or the chart must be created first."
fi

# ---------------------------------------------------------------------------
# Wait for Airflow api-server to become ready (Airflow 3.x renamed webserver → api-server)
# ---------------------------------------------------------------------------
info "Waiting for Airflow api-server to become ready..."
kubectl rollout status deployment/dataspoke-airflow-api-server -n "${NS}" --timeout=120s \
  && info "Airflow api-server is ready." \
  || warn "Airflow api-server did not become ready in time — check pod logs."

# ---------------------------------------------------------------------------
# Print access instructions
# ---------------------------------------------------------------------------
echo ""
info "DataSpoke infrastructure installation complete."
kubectl get pods -n "${NS}" 2>/dev/null || true
echo ""
if [[ -n "${DATASPOKE_DEV_INGRESS_DOMAIN:-}" ]]; then
  echo "  DataSpoke API: http://app.${DATASPOKE_DEV_INGRESS_DOMAIN}/api/v1/"
  echo "  Airflow UI:    http://airflow.${DATASPOKE_DEV_INGRESS_DOMAIN}/"
  echo "  Airflow creds: ${DATASPOKE_AIRFLOW_USER:-admin} / ${DATASPOKE_AIRFLOW_PASSWORD:-admin}"
fi
echo "  PostgreSQL:    ${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9201"
echo "  Redis:         ${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9202"
echo "  Qdrant:        ${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9203 (HTTP), :9204 (gRPC)"
echo ""
