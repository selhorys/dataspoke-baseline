#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHART_DIR="$REPO_ROOT/helm-charts/langfuse"
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
echo "=== Installing Langfuse subsystem ==="
echo ""

LANGFUSE_NS="${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Verify required env vars
# ---------------------------------------------------------------------------
info "Checking required environment variables..."
: "${DATASPOKE_DEV_KUBE_CLUSTER:?DATASPOKE_DEV_KUBE_CLUSTER must be set in .env}"
: "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE:?DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE must be set in .env}"
: "${DATASPOKE_DEV_INGRESS_IP:?DATASPOKE_DEV_INGRESS_IP must be set in .env (run nginx-ingress/install.sh first)}"
info "Required environment variables present."

# ---------------------------------------------------------------------------
# Generate and persist random secrets if not already set
# ---------------------------------------------------------------------------
_env_set_or_generate() {
  local var_name="$1"
  local current_value="${!var_name:-}"
  if [[ -n "$current_value" ]]; then
    info "  ${var_name} already set."
    return
  fi
  info "${var_name} not set — generating with openssl rand -hex 32..."
  local generated
  generated="$(openssl rand -hex 32)"
  upsert_env_var "${var_name}" "${generated}" "${ENV_FILE}"
  info "  ${var_name} generated and written to .env."
  export "${var_name}=${generated}"
}

_env_set_or_generate DATASPOKE_DEV_LANGFUSE_NEXTAUTH_SECRET
_env_set_or_generate DATASPOKE_DEV_LANGFUSE_SALT
_env_set_or_generate DATASPOKE_DEV_LANGFUSE_ENCRYPTION_KEY
_env_set_or_generate DATASPOKE_DEV_LANGFUSE_CLICKHOUSE_PASSWORD
_env_set_or_generate DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_PASSWORD
_env_set_or_generate DATASPOKE_DEV_LANGFUSE_POSTGRES_PASSWORD
_env_set_or_generate DATASPOKE_DEV_LANGFUSE_REDIS_PASSWORD

# MinIO root user is fixed (not random)
DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_USER="${DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_USER:-minio}"

# Headless-init seeded credentials (dev). On first web start, Langfuse reads
# LANGFUSE_INIT_* env vars and provisions an org + project + user + API key
# pair so no manual UI signup is needed. Deterministic public key + persisted
# secret key make the pair stable across re-installs.
#
# Login at the UI: dataspoke@dataspoke.local / dataspoke
DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL="${DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL:-dataspoke@dataspoke.local}"
DATASPOKE_DEV_LANGFUSE_INIT_USER_NAME="${DATASPOKE_DEV_LANGFUSE_INIT_USER_NAME:-dataspoke}"
DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD="${DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD:-dataspoke}"
DATASPOKE_DEV_LANGFUSE_INIT_ORG_ID="${DATASPOKE_DEV_LANGFUSE_INIT_ORG_ID:-dataspoke-org}"
DATASPOKE_DEV_LANGFUSE_INIT_ORG_NAME="${DATASPOKE_DEV_LANGFUSE_INIT_ORG_NAME:-DataSpoke}"
DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID="${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID:-dataspoke-project}"
DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_NAME="${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_NAME:-dataspoke}"

# Public key is human-readable, hardcoded; secret key is persisted random hex
# so it stays stable across re-runs and matches what Langfuse provisioned.
if [[ -z "${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY:-}" ]]; then
  info "DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY not set — defaulting to pk-lf-dataspoke-dev..."
  DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY="pk-lf-dataspoke-dev"
  upsert_env_var DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY "${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY}" "${ENV_FILE}"
  export DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY
  info "  DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY=${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY} persisted to .env."
fi
if [[ -z "${DATASPOKE_DEV_LANGFUSE_SECRET_KEY:-}" ]]; then
  info "DATASPOKE_DEV_LANGFUSE_SECRET_KEY not set — generating sk-lf-dataspoke-dev-<hex>..."
  _generated_sk="sk-lf-dataspoke-dev-$(openssl rand -hex 24)"
  upsert_env_var DATASPOKE_DEV_LANGFUSE_SECRET_KEY "${_generated_sk}" "${ENV_FILE}"
  export DATASPOKE_DEV_LANGFUSE_SECRET_KEY="${_generated_sk}"
  info "  DATASPOKE_DEV_LANGFUSE_SECRET_KEY generated and persisted to .env."
fi

chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Ensure langfuse namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "${LANGFUSE_NS}"

# ---------------------------------------------------------------------------
# Add Langfuse Helm repo
# ---------------------------------------------------------------------------
helm_repo_add_if_missing langfuse https://langfuse.github.io/langfuse-k8s
helm repo update langfuse
info "Helm repo 'langfuse' up to date."

# ---------------------------------------------------------------------------
# Build chart dependencies
# ---------------------------------------------------------------------------
if [[ -d "$CHART_DIR/charts" ]] && compgen -G "$CHART_DIR/charts/langfuse-*.tgz" > /dev/null 2>&1; then
  info "Langfuse chart dependency already present — skipping helm dependency update."
else
  info "Running helm dependency update for langfuse..."
  helm dependency update "$CHART_DIR"
fi

# ---------------------------------------------------------------------------
# Create dataspoke-langfuse-secret in langfuse-01 (full key set — idempotent)
# Langfuse pods (web, worker, migration jobs) resolve this secret locally
# within their own namespace.
# ---------------------------------------------------------------------------
info "Creating dataspoke-langfuse-secret in ${LANGFUSE_NS} (full key set)..."
kubectl create secret generic dataspoke-langfuse-secret \
  --namespace "${LANGFUSE_NS}" \
  --from-literal=LANGFUSE_PUBLIC_KEY="${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY}" \
  --from-literal=LANGFUSE_SECRET_KEY="${DATASPOKE_DEV_LANGFUSE_SECRET_KEY}" \
  --from-literal=LANGFUSE_NEXTAUTH_SECRET="${DATASPOKE_DEV_LANGFUSE_NEXTAUTH_SECRET}" \
  --from-literal=LANGFUSE_SALT="${DATASPOKE_DEV_LANGFUSE_SALT}" \
  --from-literal=LANGFUSE_ENCRYPTION_KEY="${DATASPOKE_DEV_LANGFUSE_ENCRYPTION_KEY}" \
  --from-literal=LANGFUSE_CLICKHOUSE_PASSWORD="${DATASPOKE_DEV_LANGFUSE_CLICKHOUSE_PASSWORD}" \
  --from-literal=LANGFUSE_POSTGRES_PASSWORD="${DATASPOKE_DEV_LANGFUSE_POSTGRES_PASSWORD}" \
  --from-literal=LANGFUSE_REDIS_PASSWORD="${DATASPOKE_DEV_LANGFUSE_REDIS_PASSWORD}" \
  --from-literal=LANGFUSE_MINIO_ROOT_USER="${DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_USER}" \
  --from-literal=LANGFUSE_MINIO_ROOT_PASSWORD="${DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_PASSWORD}" \
  --from-literal=LANGFUSE_S3_ACCESS_KEY_ID="${DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_USER}" \
  --from-literal=LANGFUSE_S3_SECRET_ACCESS_KEY="${DATASPOKE_DEV_LANGFUSE_MINIO_ROOT_PASSWORD}" \
  --from-literal=LANGFUSE_INIT_USER_EMAIL="${DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL}" \
  --from-literal=LANGFUSE_INIT_USER_NAME="${DATASPOKE_DEV_LANGFUSE_INIT_USER_NAME}" \
  --from-literal=LANGFUSE_INIT_USER_PASSWORD="${DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD}" \
  --from-literal=LANGFUSE_INIT_ORG_ID="${DATASPOKE_DEV_LANGFUSE_INIT_ORG_ID}" \
  --from-literal=LANGFUSE_INIT_ORG_NAME="${DATASPOKE_DEV_LANGFUSE_INIT_ORG_NAME}" \
  --from-literal=LANGFUSE_INIT_PROJECT_ID="${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID}" \
  --from-literal=LANGFUSE_INIT_PROJECT_NAME="${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_NAME}" \
  --from-literal=LANGFUSE_INIT_PROJECT_PUBLIC_KEY="${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY}" \
  --from-literal=LANGFUSE_INIT_PROJECT_SECRET_KEY="${DATASPOKE_DEV_LANGFUSE_SECRET_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
info "dataspoke-langfuse-secret applied in ${LANGFUSE_NS}."

# ---------------------------------------------------------------------------
# Helm install/upgrade
# ---------------------------------------------------------------------------
INGRESS_DOMAIN="${DATASPOKE_DEV_INGRESS_IP}.nip.io"
LANGFUSE_HOST="http://langfuse.${INGRESS_DOMAIN}"

info "Installing/upgrading langfuse Helm release in namespace ${LANGFUSE_NS}..."
helm upgrade --install langfuse "$CHART_DIR" \
  -f "$CHART_DIR/values.yaml" \
  -f "$CHART_DIR/values-dev.yaml" \
  -n "${LANGFUSE_NS}" \
  --set-string "langfuse.langfuse.nextauth.url=${LANGFUSE_HOST}" \
  --set-string "langfuse.langfuse.ingress.hosts[0].host=langfuse.${INGRESS_DOMAIN}" \
  --set-string "langfuse.langfuse.ingress.hosts[0].paths[0].path=/" \
  --set-string "langfuse.langfuse.ingress.hosts[0].paths[0].pathType=Prefix" \
  --timeout 15m

# ---------------------------------------------------------------------------
# Wait for rollout
# The upstream langfuse subchart names its Deployments using .Release.Name as
# the prefix. With release name `langfuse` the deployments are named
# `langfuse-web` and `langfuse-worker`.
# ---------------------------------------------------------------------------
info "Waiting for Langfuse web deployment to become ready..."
kubectl rollout status deployment/langfuse-web \
  -n "${LANGFUSE_NS}" --timeout=300s \
  && info "langfuse-web is ready." \
  || warn "langfuse-web did not become ready in time — check pod logs."

info "Waiting for Langfuse worker deployment to become ready..."
kubectl rollout status deployment/langfuse-worker \
  -n "${LANGFUSE_NS}" --timeout=300s \
  && info "langfuse-worker is ready." \
  || warn "langfuse-worker did not become ready in time — check pod logs."

# ---------------------------------------------------------------------------
# Write DATASPOKE_DEV_LANGFUSE_HOST back into .env (idempotent upsert)
# dataspoke-infra/install.sh reads this on its initial helm upgrade --install,
# so no post-hoc reupgrade of the umbrella chart is needed.
# ---------------------------------------------------------------------------
upsert_env_var DATASPOKE_DEV_LANGFUSE_HOST "${LANGFUSE_HOST}" "${ENV_FILE}"
info "DATASPOKE_DEV_LANGFUSE_HOST=${LANGFUSE_HOST} written to .env."

# ---------------------------------------------------------------------------
# Access summary
# ---------------------------------------------------------------------------
echo ""
info "Langfuse installation complete."
echo ""
echo "  Langfuse UI:    ${LANGFUSE_HOST}/"
echo "  Namespace:      ${LANGFUSE_NS}"
echo "  Secret:         dataspoke-langfuse-secret in ${LANGFUSE_NS}"
echo ""
echo "  Login: dataspoke@dataspoke.local / dataspoke"
echo ""
