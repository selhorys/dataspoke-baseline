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
require_tools kubectl helm
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "${NS}"

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

# Internal-auth token (Airflow→API): .env is the source of truth. Generate
# and persist back to .env on first run, then upsert the cluster secret.
if [[ -z "${DATASPOKE_INTERNAL_TOKEN:-}" ]]; then
  info "DATASPOKE_INTERNAL_TOKEN unset — generating and appending to .env..."
  DATASPOKE_INTERNAL_TOKEN="$(openssl rand -hex 32)"
  printf '\nDATASPOKE_INTERNAL_TOKEN=%s\n' "${DATASPOKE_INTERNAL_TOKEN}" \
    >> "$SCRIPT_DIR/../.env"
fi

info "Applying dataspoke-internal-auth..."
kubectl create secret generic dataspoke-internal-auth \
  --namespace "${NS}" \
  --from-literal=token="${DATASPOKE_INTERNAL_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -

# LLM API key — provisioned out-of-band so helm upgrade cannot clobber online
# rotations done via PATCH /admin/conf. The app reads the key at runtime from
# the dataspoke-llm-secret Secret via the Kubernetes API (api-secret-reader RBAC).
if [[ -n "${DATASPOKE_DEV_LLM_API_KEY:-}" ]]; then
  info "Applying dataspoke-llm-secret (LLM API key)..."
  kubectl create secret generic dataspoke-llm-secret \
    --namespace "${NS}" \
    --from-literal=api_key="${DATASPOKE_DEV_LLM_API_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  info "DATASPOKE_DEV_LLM_API_KEY is unset — dataspoke-llm-secret not created; app will read key as unset until set via /admin/conf."
fi

# DataHub token — provisioned out-of-band (same rationale as dataspoke-llm-secret).
# DATASPOKE_DEV_DATAHUB_TOKEN is written by dev_env/datahub/install.sh.
if [[ -n "${DATASPOKE_DEV_DATAHUB_TOKEN:-}" ]]; then
  info "Applying dataspoke-datahub-secret (DataHub PAT)..."
  kubectl create secret generic dataspoke-datahub-secret \
    --namespace "${NS}" \
    --from-literal=token="${DATASPOKE_DEV_DATAHUB_TOKEN}" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  info "DATASPOKE_DEV_DATAHUB_TOKEN is unset — dataspoke-datahub-secret not created; app will treat DataHub as unconfigured until set via /api/v1/admin/peripherals/datahub."
fi

# Langfuse secret key — provisioned out-of-band (same rationale as dataspoke-llm-secret).
# DATASPOKE_DEV_LANGFUSE_SECRET_KEY is written by dev_env/langfuse/install.sh.
if [[ -n "${DATASPOKE_DEV_LANGFUSE_SECRET_KEY:-}" ]]; then
  info "Applying dataspoke-langfuse-secret (Langfuse secret key)..."
  kubectl create secret generic dataspoke-langfuse-secret \
    --namespace "${NS}" \
    --from-literal=secret_key="${DATASPOKE_DEV_LANGFUSE_SECRET_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  info "DATASPOKE_DEV_LANGFUSE_SECRET_KEY is unset — dataspoke-langfuse-secret not created; Langfuse tracing disabled until set via /api/v1/admin/peripherals/langfuse."
fi

# ---------------------------------------------------------------------------
# Register required Helm repositories (idempotent)
# ---------------------------------------------------------------------------
info "Adding/updating Helm repositories..."
helm_repo_add_if_missing bitnami        "https://charts.bitnami.com/bitnami"
helm_repo_add_if_missing apache-airflow "https://airflow.apache.org"
helm repo update

# ---------------------------------------------------------------------------
# Build chart dependencies
# ---------------------------------------------------------------------------
if [[ -d "$CHART_DIR" ]]; then
  info "Building Helm chart dependencies..."
  helm dependency build "$CHART_DIR"
fi

# ---------------------------------------------------------------------------
# Build + push the custom PostgreSQL image (pgvector + Apache AGE). Idempotent
# — the existing :dev tag is overwritten. Skip with SKIP_POSTGRES_BUILD=1 when
# the image is already up-to-date in the registry.
# ---------------------------------------------------------------------------
if [[ "${SKIP_POSTGRES_BUILD:-0}" != "1" ]]; then
  info "Building DataSpoke PostgreSQL image (pgvector + AGE)..."
  bash "$SCRIPT_DIR/../dataspoke-postgres/build.sh" dev
else
  info "SKIP_POSTGRES_BUILD=1 set — skipping PostgreSQL image build."
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
    --set-string global.imageRegistry="" \
    --set-string postgresql.image.registry="" \
    --set-string postgresql.image.repository="${DATASPOKE_DEV_IMAGE_REGISTRY}/postgres" \
    --set-string postgresql.image.tag=dev \
    --set api.image.repository="${DATASPOKE_DEV_IMAGE_REGISTRY}/api" \
    --set api.image.tag=dev \
    --set-string airflow.images.airflow.repository="${DATASPOKE_DEV_IMAGE_REGISTRY}/airflow" \
    --set-string airflow.images.airflow.tag=dev \
    --set airflow.images.airflow.pullPolicy=Always \
    --set-string secrets.postgres.user="${DATASPOKE_POSTGRES_USER}" \
    --set-string secrets.postgres.password="${DATASPOKE_POSTGRES_PASSWORD}" \
    --set-string secrets.redis.password="${DATASPOKE_REDIS_PASSWORD}" \
    --set-string secrets.airflow.user="${DATASPOKE_AIRFLOW_USER:-admin}" \
    --set-string secrets.airflow.password="${DATASPOKE_AIRFLOW_PASSWORD:-admin}" \
    --set-string config.airflow.callbackBaseUrl="http://dataspoke-api:8002" \
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
# Create pgvector + AGE extensions as the postgres superuser.
#
# The Bitnami chart's initdb scripts run as the application user `dataspoke`,
# which does not have CREATE EXTENSION privilege. This step is idempotent
# (CREATE EXTENSION IF NOT EXISTS) and ensures the extensions are available
# on every reinstall without granting the app user superuser rights.
# ---------------------------------------------------------------------------
info "Ensuring pgvector + age extensions in the dataspoke database..."
# Fix #3a: raise timeout to 5m — Autopilot cold-start can take > 2 min.
# Keep || true: the subsequent psql block has its own success/warn handling.
kubectl rollout status statefulset/dataspoke-postgresql -n "${NS}" --timeout=5m >/dev/null 2>&1 || true
kubectl exec -n "${NS}" dataspoke-postgresql-0 -- \
  env PGPASSWORD="${DATASPOKE_POSTGRES_PASSWORD}" \
  psql -U postgres -d "${DATASPOKE_POSTGRES_DB}" -c "
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS age;
    GRANT USAGE ON SCHEMA ag_catalog TO ${DATASPOKE_POSTGRES_USER};
    GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO ${DATASPOKE_POSTGRES_USER};
  " >/dev/null 2>&1 \
  && info "  Extensions ready (vector + age)." \
  || warn "  Could not create extensions — run manually via kubectl exec."

# ---------------------------------------------------------------------------
# Wait for Airflow api-server to become ready (Airflow 3.x renamed webserver → api-server)
# Fix #3b: raise timeout to 5m (observed 4m31s on cold Autopilot start).
# Change failure from warn to error — a hung api-server should fail loudly.
# ---------------------------------------------------------------------------
info "Waiting for Airflow api-server to become ready..."
kubectl rollout status deployment/dataspoke-airflow-api-server -n "${NS}" --timeout=5m \
  && info "Airflow api-server is ready." \
  || error "Airflow api-server did not become ready in time — check pod logs."

# ---------------------------------------------------------------------------
# Wait for DataSpoke API deployment, then seed dev LLM provider/model into
# the runtime config table. The app factory defaults apply on first start;
# this PATCH overrides them to match the dev-tier vars without embedding
# provider/model in the ConfigMap.
# ---------------------------------------------------------------------------
info "Waiting for DataSpoke API to become ready..."
kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
  && info "DataSpoke API is ready." \
  || warn "DataSpoke API did not become ready in time — skipping runtime config PATCH."

if [[ -n "${DATASPOKE_DEV_LLM_PROVIDER:-}" && -n "${DATASPOKE_DEV_LLM_MODEL:-}" ]]; then
  info "Seeding dev LLM provider/model into runtime config via /internal/admin/conf..."
  curl -fsS -X PATCH \
    "http://app.${DATASPOKE_DEV_INGRESS_DOMAIN:-dev.dataspoke.example.com}/internal/admin/conf" \
    -H "X-Internal-Token: ${DATASPOKE_INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"llm_provider\": \"${DATASPOKE_DEV_LLM_PROVIDER}\", \"llm_model\": \"${DATASPOKE_DEV_LLM_MODEL}\"}" \
    && info "Runtime config patched: provider=${DATASPOKE_DEV_LLM_PROVIDER} model=${DATASPOKE_DEV_LLM_MODEL}." \
    || warn "Runtime config PATCH failed — app will use factory defaults. Run manually after API is reachable."
else
  info "DATASPOKE_DEV_LLM_PROVIDER or DATASPOKE_DEV_LLM_MODEL not set — skipping runtime config PATCH."
fi

# Seed DataHub peripheral connection (non-secret fields) via internal API.
# DATASPOKE_DEV_DATAHUB_GMS_URL and DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS are
# written by dev_env/datahub/install.sh. The token was already placed in the
# dataspoke-datahub-secret above.
if [[ -n "${DATASPOKE_DEV_DATAHUB_GMS_URL:-}" && -n "${DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS:-}" ]]; then
  info "Seeding DataHub connection into peripheral config via /internal/admin/peripherals/datahub..."
  curl -fsS -X PATCH \
    "http://app.${DATASPOKE_DEV_INGRESS_DOMAIN:-dev.dataspoke.example.com}/internal/admin/peripherals/datahub" \
    -H "X-Internal-Token: ${DATASPOKE_INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"gms_url\": \"${DATASPOKE_DEV_DATAHUB_GMS_URL}\", \"kafka_brokers\": \"${DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS}\"}" \
    && info "DataHub peripheral config patched: gms_url=${DATASPOKE_DEV_DATAHUB_GMS_URL}." \
    || warn "DataHub peripheral config PATCH failed — DataHub-touching routes will return 503 until set via /api/v1/admin/peripherals/datahub."
else
  info "DATASPOKE_DEV_DATAHUB_GMS_URL or DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS not set — skipping DataHub peripheral PATCH."
fi

# Seed Langfuse peripheral connection (non-secret fields) via internal API.
# DATASPOKE_DEV_LANGFUSE_HOST and DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY are
# written by dev_env/langfuse/install.sh. The secret key was already placed
# in the dataspoke-langfuse-secret above.
if [[ -n "${DATASPOKE_DEV_LANGFUSE_HOST:-}" && -n "${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY:-}" ]]; then
  info "Seeding Langfuse connection into peripheral config via /internal/admin/peripherals/langfuse..."
  curl -fsS -X PATCH \
    "http://app.${DATASPOKE_DEV_INGRESS_DOMAIN:-dev.dataspoke.example.com}/internal/admin/peripherals/langfuse" \
    -H "X-Internal-Token: ${DATASPOKE_INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"host\": \"${DATASPOKE_DEV_LANGFUSE_HOST}\", \"public_key\": \"${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY}\"}" \
    && info "Langfuse peripheral config patched: host=${DATASPOKE_DEV_LANGFUSE_HOST}." \
    || warn "Langfuse peripheral config PATCH failed — Langfuse tracing will remain disabled until set via /api/v1/admin/peripherals/langfuse."
else
  info "DATASPOKE_DEV_LANGFUSE_HOST or DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY not set — skipping Langfuse peripheral PATCH."
fi

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
echo ""
