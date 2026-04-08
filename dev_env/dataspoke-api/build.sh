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

REGISTRY="${DATASPOKE_DEV_IMAGE_REGISTRY:?Set DATASPOKE_DEV_IMAGE_REGISTRY in .env}"
VENDOR="${DATASPOKE_DEV_CLOUD_VENDOR:-}"
TAG="${1:-dev}"
IMAGE="${REGISTRY}/api:${TAG}"
PROJECT_ROOT="$SCRIPT_DIR/../.."

# ---------------------------------------------------------------------------
# Build + push based on cloud vendor
# ---------------------------------------------------------------------------
case "${VENDOR}" in
  GCP|gcp)
    # Extract GCP project ID from the registry URL:
    #   <region>-docker.pkg.dev/<project>/<repo>  →  <project>
    GCP_PROJECT=$(echo "${REGISTRY}" | sed -n 's|.*-docker\.pkg\.dev/\([^/]*\)/.*|\1|p')
    if [[ -z "${GCP_PROJECT}" ]]; then
      error "Could not extract GCP project from DATASPOKE_DEV_IMAGE_REGISTRY (${REGISTRY}). Expected format: <region>-docker.pkg.dev/<project>/<repo>"
    fi

    info "Building ${IMAGE} via Google Cloud Build (project: ${GCP_PROJECT})..."
    gcloud builds submit "${PROJECT_ROOT}" \
      --config /dev/stdin \
      --project "${GCP_PROJECT}" \
      --substitutions="_IMAGE=${IMAGE}" \
      --quiet <<'CLOUDBUILD'
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', '${_IMAGE}', '-f', 'docker-images/api/Dockerfile', '.']
images: ['${_IMAGE}']
CLOUDBUILD
    ;;

  AWS|aws)
    # TODO: Implement AWS CodeBuild or local docker build + ECR push
    error "AWS remote build is not yet implemented. Set DATASPOKE_DEV_CLOUD_VENDOR=GCP or use local Docker."
    ;;

  *)
    # Fallback: local Docker build + push (requires Docker daemon)
    info "Building ${IMAGE} via local Docker..."
    docker build -t "${IMAGE}" -f "${PROJECT_ROOT}/docker-images/api/Dockerfile" "${PROJECT_ROOT}"
    info "Pushing ${IMAGE}..."
    docker push "${IMAGE}"
    ;;
esac

info "Done. Image: ${IMAGE}"
