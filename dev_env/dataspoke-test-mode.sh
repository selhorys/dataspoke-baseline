#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Start the DataSpoke in-cluster API in test mode for api-wired integration
# testing.
#
# Builds and pushes the Docker image, deploys via Helm (dataspoke-infra
# install.sh), and waits for the rollout. The API is accessible via nginx
# ingress — no port-forward needed.
#
# DATASPOKE_TEST_MODE=true is baked into values-dev.yaml (api.testMode: true)
# so Kestra callbacks reach the API via http://dataspoke-api:8000 within the
# cluster.
#
# Usage:
#   ./dev_env/dataspoke-test-mode.sh                  # Build + deploy
#   ./dev_env/dataspoke-test-mode.sh --skip-build     # Skip docker build, just deploy
#   ./dev_env/dataspoke-test-mode.sh --health-check   # Run health check first
#   ./dev_env/dataspoke-test-mode.sh --stop           # Scale down API deployment
#
# After the server is running, in a second terminal:
#   DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/ -v
#
# Note: DATASPOKE_TEST_MODE must be set in the pytest process as well —
# the API pod has it baked in via values-dev.yaml.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SKIP_BUILD=false
HEALTH_CHECK=false
STOP_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)       SKIP_BUILD=true ;;
    --health-check)     HEALTH_CHECK=true ;;
    --stop)             STOP_ONLY=true ;;
    # Legacy flags accepted as no-ops (server is now in-cluster)
    --skip-migrate|--no-reload|--backend-only) true ;;
    --port) shift ;;  # consume the value too
    *) warn "Unknown option: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env"
fi
source "$SCRIPT_DIR/.env"

NS="${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
DOMAIN="${DATASPOKE_DEV_INGRESS_DOMAIN:-}"

# ---------------------------------------------------------------------------
# --stop: scale down the API deployment
# ---------------------------------------------------------------------------
if [[ "$STOP_ONLY" == "true" ]]; then
  info "Scaling down dataspoke-api deployment..."
  kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}" >/dev/null 2>&1
  kubectl scale deployment/dataspoke-api --replicas=0 -n "${NS}" 2>/dev/null \
    && info "dataspoke-api scaled to 0." \
    || warn "Could not scale dataspoke-api — it may not exist yet."
  exit 0
fi

# ---------------------------------------------------------------------------
# Health check (optional)
# ---------------------------------------------------------------------------
if [[ "$HEALTH_CHECK" == "true" ]]; then
  info "Running dev-env health check..."
  if ! bash "$SCRIPT_DIR/health-check.sh"; then
    error "Health check failed — fix the failing services before starting test mode."
  fi
fi

# ---------------------------------------------------------------------------
# Step 1: Build and push image (unless --skip-build)
# ---------------------------------------------------------------------------
if [[ "$SKIP_BUILD" == "false" ]]; then
  info "Building and pushing DataSpoke API image..."
  if ! bash "$SCRIPT_DIR/dataspoke-api/build.sh" dev; then
    echo ""
    warn "Image build failed. Common fixes by cloud vendor:"
    echo ""
    VENDOR="${DATASPOKE_DEV_CLOUD_VENDOR:-}"
    case "${VENDOR}" in
      GCP|gcp)
        echo "  GCP Cloud Build prerequisites:"
        echo "    1. Enable Cloud Build API:"
        echo "       gcloud services enable cloudbuild.googleapis.com --project <PROJECT_ID>"
        echo ""
        echo "    2. Grant Cloud Build permissions to your account:"
        echo "       gcloud projects add-iam-policy-binding <PROJECT_ID> \\"
        echo "         --member='user:<YOUR_EMAIL>' \\"
        echo "         --role='roles/cloudbuild.builds.editor'"
        echo ""
        echo "    3. Ensure Artifact Registry exists:"
        echo "       gcloud artifacts repositories create dataspoke \\"
        echo "         --repository-format=docker --location=<REGION> \\"
        echo "         --project <PROJECT_ID>"
        echo ""
        echo "    4. Wait 1-2 minutes after enabling APIs, then retry."
        ;;
      AWS|aws)
        echo "  AWS ECR prerequisites:"
        echo "    1. Create ECR repository:"
        echo "       aws ecr create-repository --repository-name dataspoke/api --region <REGION>"
        echo ""
        echo "    2. Authenticate Docker to ECR:"
        echo "       aws ecr get-login-password --region <REGION> | \\"
        echo "         docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com"
        echo ""
        echo "    (Note: AWS remote build via CodeBuild is not yet implemented.)"
        ;;
      *)
        echo "  Local Docker build prerequisites:"
        echo "    1. Ensure Docker daemon is running (Docker Desktop, colima, etc.)"
        echo "    2. Authenticate to your container registry: docker login <REGISTRY>"
        echo ""
        echo "  Or set DATASPOKE_DEV_CLOUD_VENDOR=GCP in .env to use Google Cloud Build."
        ;;
    esac
    echo ""
    exit 1
  fi
else
  info "--skip-build: skipping docker build."
fi

# ---------------------------------------------------------------------------
# Step 2: Deploy via Helm (dataspoke-infra install.sh)
# ---------------------------------------------------------------------------
info "Deploying DataSpoke infra + API via Helm..."
bash "$SCRIPT_DIR/dataspoke-infra/install.sh"

# ---------------------------------------------------------------------------
# Step 3: Wait for API rollout
# ---------------------------------------------------------------------------
info "Waiting for dataspoke-api rollout..."
kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}" >/dev/null 2>&1
kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=120s \
  && info "dataspoke-api is ready." \
  || { warn "dataspoke-api did not become ready in time — check pod logs."; }

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo ""
info "DataSpoke test-mode is running."
echo ""
if [[ -n "$DOMAIN" ]]; then
  echo "  API:             http://app.${DOMAIN}/api"
  echo "  ReDoc:           http://app.${DOMAIN}/redoc"
else
  echo "  API:             http://app.<INGRESS_DOMAIN>/api"
  echo "  Note: set DATASPOKE_DEV_INGRESS_DOMAIN in .env for the correct URL"
fi
echo "  DATASPOKE_TEST_MODE: true (baked into deployment via values-dev.yaml)"
echo ""
echo "  In a second terminal:"
echo "    DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/ -v"
echo ""
echo "  Stop with: $0 --stop"
echo ""
