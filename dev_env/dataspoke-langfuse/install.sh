#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHART_DIR="$REPO_ROOT/helm-charts/dataspoke-langfuse"
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
echo "=== Installing DataSpoke Langfuse subsystem ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Verify required env vars
# ---------------------------------------------------------------------------
info "Checking required environment variables..."
: "${DATASPOKE_DEV_KUBE_CLUSTER:?DATASPOKE_DEV_KUBE_CLUSTER must be set in .env}"
: "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE:?DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE must be set in .env}"
: "${DATASPOKE_POSTGRES_PASSWORD:?DATASPOKE_POSTGRES_PASSWORD must be set in .env}"
: "${DATASPOKE_REDIS_PASSWORD:?DATASPOKE_REDIS_PASSWORD must be set in .env}"
: "${DATASPOKE_DEV_INGRESS_IP:?DATASPOKE_DEV_INGRESS_IP must be set in .env (run nginx-ingress/install.sh first)}"
info "Required environment variables present."

# ---------------------------------------------------------------------------
# Generate and persist random secrets if not already set
# ---------------------------------------------------------------------------
# Re-read .env into this function to get current values after possible prior run
_env_set_or_generate() {
  local var_name="$1"
  local current_value="${!var_name:-}"
  if [[ -z "$current_value" ]]; then
    info "${var_name} not set — generating with openssl rand -hex 32..."
    local generated
    generated="$(openssl rand -hex 32)"
    printf '\n%s=%s\n' "${var_name}" "${generated}" >> "$ENV_FILE"
    export "${var_name}=${generated}"
    info "  ${var_name} generated and appended to .env."
  else
    info "  ${var_name} already set."
  fi
}

_env_set_or_generate DATASPOKE_LANGFUSE_NEXTAUTH_SECRET
_env_set_or_generate DATASPOKE_LANGFUSE_SALT
_env_set_or_generate DATASPOKE_LANGFUSE_ENCRYPTION_KEY
_env_set_or_generate DATASPOKE_LANGFUSE_CLICKHOUSE_PASSWORD
_env_set_or_generate DATASPOKE_LANGFUSE_MINIO_ROOT_PASSWORD

# MinIO root user is fixed (not random)
DATASPOKE_LANGFUSE_MINIO_ROOT_USER="${DATASPOKE_LANGFUSE_MINIO_ROOT_USER:-minio}"

# Headless-init seeded credentials (dev). On first web start, Langfuse reads
# LANGFUSE_INIT_* env vars and provisions an org + project + user + API key
# pair so no manual UI signup is needed. Deterministic public key + persisted
# secret key make the pair stable across re-installs.
#
# Login at the UI: dataspoke@dataspoke.local / dataspoke
DATASPOKE_LANGFUSE_INIT_USER_EMAIL="${DATASPOKE_LANGFUSE_INIT_USER_EMAIL:-dataspoke@dataspoke.local}"
DATASPOKE_LANGFUSE_INIT_USER_NAME="${DATASPOKE_LANGFUSE_INIT_USER_NAME:-dataspoke}"
DATASPOKE_LANGFUSE_INIT_USER_PASSWORD="${DATASPOKE_LANGFUSE_INIT_USER_PASSWORD:-dataspoke}"
DATASPOKE_LANGFUSE_INIT_ORG_ID="${DATASPOKE_LANGFUSE_INIT_ORG_ID:-dataspoke-org}"
DATASPOKE_LANGFUSE_INIT_ORG_NAME="${DATASPOKE_LANGFUSE_INIT_ORG_NAME:-DataSpoke}"
DATASPOKE_LANGFUSE_INIT_PROJECT_ID="${DATASPOKE_LANGFUSE_INIT_PROJECT_ID:-dataspoke-project}"
DATASPOKE_LANGFUSE_INIT_PROJECT_NAME="${DATASPOKE_LANGFUSE_INIT_PROJECT_NAME:-dataspoke}"

# Public key is human-readable, hardcoded; secret key is persisted random hex
# so it stays stable across re-runs and matches what Langfuse provisioned.
# Persist both into .env so downstream tools (helm --set-string, host-mode
# clients) read them via the normal source dev_env/.env path.
if [[ -z "${DATASPOKE_LANGFUSE_PUBLIC_KEY:-}" ]]; then
  info "DATASPOKE_LANGFUSE_PUBLIC_KEY not set — defaulting to pk-lf-dataspoke-dev..."
  DATASPOKE_LANGFUSE_PUBLIC_KEY="pk-lf-dataspoke-dev"
  if grep -q "^DATASPOKE_LANGFUSE_PUBLIC_KEY=" "$ENV_FILE" 2>/dev/null; then
    sed -i.bak "s|^DATASPOKE_LANGFUSE_PUBLIC_KEY=.*|DATASPOKE_LANGFUSE_PUBLIC_KEY=${DATASPOKE_LANGFUSE_PUBLIC_KEY}|" "$ENV_FILE" \
      && rm -f "${ENV_FILE}.bak"
  else
    printf '\n%s=%s\n' "DATASPOKE_LANGFUSE_PUBLIC_KEY" "${DATASPOKE_LANGFUSE_PUBLIC_KEY}" >> "$ENV_FILE"
  fi
  export DATASPOKE_LANGFUSE_PUBLIC_KEY
  info "  DATASPOKE_LANGFUSE_PUBLIC_KEY=${DATASPOKE_LANGFUSE_PUBLIC_KEY} persisted to .env."
fi
if [[ -z "${DATASPOKE_LANGFUSE_SECRET_KEY:-}" ]]; then
  info "DATASPOKE_LANGFUSE_SECRET_KEY not set — generating sk-lf-dataspoke-dev-<hex>..."
  _generated_sk="sk-lf-dataspoke-dev-$(openssl rand -hex 24)"
  if grep -q "^DATASPOKE_LANGFUSE_SECRET_KEY=" "$ENV_FILE" 2>/dev/null; then
    sed -i.bak "s|^DATASPOKE_LANGFUSE_SECRET_KEY=.*|DATASPOKE_LANGFUSE_SECRET_KEY=${_generated_sk}|" "$ENV_FILE" \
      && rm -f "${ENV_FILE}.bak"
  else
    printf '\n%s=%s\n' "DATASPOKE_LANGFUSE_SECRET_KEY" "${_generated_sk}" >> "$ENV_FILE"
  fi
  export DATASPOKE_LANGFUSE_SECRET_KEY="${_generated_sk}"
  info "  DATASPOKE_LANGFUSE_SECRET_KEY generated and persisted to .env."
fi

chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
command -v kubectl >/dev/null 2>&1 || error "kubectl is not installed or not in PATH."
command -v helm    >/dev/null 2>&1 || error "helm is not installed or not in PATH."
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Add Langfuse Helm repo
# ---------------------------------------------------------------------------
if helm repo list 2>/dev/null | grep -q "^langfuse"; then
  info "Helm repo 'langfuse' already added."
else
  info "Adding Helm repo 'langfuse'..."
  helm repo add langfuse https://langfuse.github.io/langfuse-k8s
fi
helm repo update langfuse
info "Helm repo 'langfuse' up to date."

# ---------------------------------------------------------------------------
# Build chart dependencies
# ---------------------------------------------------------------------------
if [[ -d "$CHART_DIR/charts" ]] && compgen -G "$CHART_DIR/charts/langfuse-*.tgz" > /dev/null 2>&1; then
  info "Langfuse chart dependency already present — skipping helm dependency update."
else
  info "Running helm dependency update for dataspoke-langfuse..."
  helm dependency update "$CHART_DIR"
fi

# ---------------------------------------------------------------------------
# Create dataspoke-langfuse-secret (idempotent)
# ---------------------------------------------------------------------------
info "Creating dataspoke-langfuse-secret..."
kubectl create secret generic dataspoke-langfuse-secret \
  --namespace "${NS}" \
  --from-literal=LANGFUSE_PUBLIC_KEY="${DATASPOKE_LANGFUSE_PUBLIC_KEY}" \
  --from-literal=LANGFUSE_SECRET_KEY="${DATASPOKE_LANGFUSE_SECRET_KEY}" \
  --from-literal=LANGFUSE_NEXTAUTH_SECRET="${DATASPOKE_LANGFUSE_NEXTAUTH_SECRET}" \
  --from-literal=LANGFUSE_SALT="${DATASPOKE_LANGFUSE_SALT}" \
  --from-literal=LANGFUSE_ENCRYPTION_KEY="${DATASPOKE_LANGFUSE_ENCRYPTION_KEY}" \
  --from-literal=LANGFUSE_CLICKHOUSE_PASSWORD="${DATASPOKE_LANGFUSE_CLICKHOUSE_PASSWORD}" \
  --from-literal=LANGFUSE_MINIO_ROOT_USER="${DATASPOKE_LANGFUSE_MINIO_ROOT_USER}" \
  --from-literal=LANGFUSE_MINIO_ROOT_PASSWORD="${DATASPOKE_LANGFUSE_MINIO_ROOT_PASSWORD}" \
  --from-literal=LANGFUSE_S3_ACCESS_KEY_ID="${DATASPOKE_LANGFUSE_MINIO_ROOT_USER}" \
  --from-literal=LANGFUSE_S3_SECRET_ACCESS_KEY="${DATASPOKE_LANGFUSE_MINIO_ROOT_PASSWORD}" \
  --from-literal=LANGFUSE_INIT_USER_EMAIL="${DATASPOKE_LANGFUSE_INIT_USER_EMAIL}" \
  --from-literal=LANGFUSE_INIT_USER_NAME="${DATASPOKE_LANGFUSE_INIT_USER_NAME}" \
  --from-literal=LANGFUSE_INIT_USER_PASSWORD="${DATASPOKE_LANGFUSE_INIT_USER_PASSWORD}" \
  --from-literal=LANGFUSE_INIT_ORG_ID="${DATASPOKE_LANGFUSE_INIT_ORG_ID}" \
  --from-literal=LANGFUSE_INIT_ORG_NAME="${DATASPOKE_LANGFUSE_INIT_ORG_NAME}" \
  --from-literal=LANGFUSE_INIT_PROJECT_ID="${DATASPOKE_LANGFUSE_INIT_PROJECT_ID}" \
  --from-literal=LANGFUSE_INIT_PROJECT_NAME="${DATASPOKE_LANGFUSE_INIT_PROJECT_NAME}" \
  --from-literal=LANGFUSE_INIT_PROJECT_PUBLIC_KEY="${DATASPOKE_LANGFUSE_PUBLIC_KEY}" \
  --from-literal=LANGFUSE_INIT_PROJECT_SECRET_KEY="${DATASPOKE_LANGFUSE_SECRET_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
info "dataspoke-langfuse-secret applied."

# ---------------------------------------------------------------------------
# Helm install/upgrade
# ---------------------------------------------------------------------------
INGRESS_DOMAIN="${DATASPOKE_DEV_INGRESS_IP}.nip.io"
LANGFUSE_HOST="http://langfuse.${INGRESS_DOMAIN}"

info "Installing/upgrading dataspoke-langfuse Helm release..."
helm upgrade --install dataspoke-langfuse "$CHART_DIR" \
  -f "$CHART_DIR/values.yaml" \
  -f "$CHART_DIR/values-dev.yaml" \
  -n "${NS}" \
  --set-string "langfuse.langfuse.nextauth.url=${LANGFUSE_HOST}" \
  --set-string "langfuse.langfuse.ingress.hosts[0].host=langfuse.${INGRESS_DOMAIN}" \
  --set-string "langfuse.langfuse.ingress.hosts[0].paths[0].path=/" \
  --set-string "langfuse.langfuse.ingress.hosts[0].paths[0].pathType=Prefix" \
  --timeout 15m

# ---------------------------------------------------------------------------
# Wait for rollout
# ---------------------------------------------------------------------------
info "Waiting for Langfuse web deployment to become ready..."
kubectl rollout status deployment/dataspoke-langfuse-web \
  -n "${NS}" --timeout=300s \
  && info "dataspoke-langfuse-web is ready." \
  || warn "dataspoke-langfuse-web did not become ready in time — check pod logs."

info "Waiting for Langfuse worker deployment to become ready..."
kubectl rollout status deployment/dataspoke-langfuse-worker \
  -n "${NS}" --timeout=300s \
  && info "dataspoke-langfuse-worker is ready." \
  || warn "dataspoke-langfuse-worker did not become ready in time — check pod logs."

# ---------------------------------------------------------------------------
# Write DATASPOKE_LANGFUSE_HOST back into .env (idempotent upsert)
# ---------------------------------------------------------------------------
if grep -q "^DATASPOKE_LANGFUSE_HOST=" "$ENV_FILE" 2>/dev/null; then
  sed -i.bak "s|^DATASPOKE_LANGFUSE_HOST=.*|DATASPOKE_LANGFUSE_HOST=${LANGFUSE_HOST}|" "$ENV_FILE" \
    && rm -f "${ENV_FILE}.bak"
else
  printf '\nDATASPOKE_LANGFUSE_HOST=%s\n' "${LANGFUSE_HOST}" >> "$ENV_FILE"
fi
info "DATASPOKE_LANGFUSE_HOST=${LANGFUSE_HOST} written to .env."

# ---------------------------------------------------------------------------
# Propagate Langfuse host into the DataSpoke umbrella chart so the API and
# Airflow containers receive the correct DATASPOKE_LANGFUSE_HOST at startup.
# --reuse-values preserves all other umbrella settings set by dataspoke-infra.
# ---------------------------------------------------------------------------
UMBRELLA_CHART="$REPO_ROOT/helm-charts/dataspoke"
if helm status dataspoke -n "${NS}" >/dev/null 2>&1; then
  info "Propagating Langfuse host + public key into the DataSpoke umbrella chart..."
  # All three of host/public/secret must be set for LLMClient to install the
  # Langfuse callback (src/shared/llm/client.py). publicKey lands in the
  # ConfigMap (non-secret); secretKey is read from dataspoke-langfuse-secret
  # via existingSecret reference (see helm-charts/dataspoke/values.yaml).
  helm upgrade --install dataspoke "$UMBRELLA_CHART" \
    --reuse-values \
    --set-string "config.langfuse.host=${LANGFUSE_HOST}" \
    --set-string "config.langfuse.publicKey=${DATASPOKE_LANGFUSE_PUBLIC_KEY}" \
    -n "${NS}" --wait --timeout 5m
  kubectl rollout restart deployment/dataspoke-api -n "${NS}" || true
  info "dataspoke-api restarted with updated Langfuse host + public key."
else
  info "DataSpoke umbrella release not found — host will be picked up on next dataspoke-infra install."
fi

# ---------------------------------------------------------------------------
# Access summary
# ---------------------------------------------------------------------------
echo ""
info "DataSpoke Langfuse installation complete."
echo ""
echo "  Langfuse UI:    ${LANGFUSE_HOST}/"
echo "  Namespace:      ${NS}"
echo "  Secret:         dataspoke-langfuse-secret"
echo ""
echo "  First-time setup:"
echo "    1. Open ${LANGFUSE_HOST}/ and sign up for an account."
echo "    2. Create an API key pair in Settings → API Keys."
echo "    3. Set DATASPOKE_LANGFUSE_PUBLIC_KEY and DATASPOKE_LANGFUSE_SECRET_KEY in .env."
echo "    4. Re-run this script to update the secret in the cluster."
echo ""
