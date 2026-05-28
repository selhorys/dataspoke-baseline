#!/usr/bin/env bash
# Build and push a DataSpoke container image.
#
# Usage: build-image.sh <name> [<tag>]
#   <name>  One of: api, airflow, postgres, frontend
#   <tag>   Image tag (default: dev)
#   --help, -h   Print this usage message.
#
# Dispatches on DATASPOKE_KUBE_CLOUD_VENDOR:
#   GCP|gcp   → gcloud builds submit (no local Docker required)
#   AWS|aws   → error: not yet implemented
#   empty     → local docker build + docker push
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Argument parsing — handle --help before loading .env so it works without one
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  print_usage
  exit 0
fi

NAME="${1:-}"
TAG="${2:-dev}"

if [[ -z "$NAME" ]]; then
  print_usage >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/../.env"
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE — copy helm-charts/.env.example and edit it."
fi
source "$ENV_FILE"

case "$NAME" in
  api|airflow|postgres) DOCKERFILE_PATH="docker-images/${NAME}/Dockerfile" ;;
  frontend)             DOCKERFILE_PATH="src/frontend/Dockerfile" ;;
  *) error "Unknown image name '${NAME}'. Must be one of: api, airflow, postgres, frontend." ;;
esac
REGISTRY="${DATASPOKE_KUBE_IMAGE_REGISTRY:?DATASPOKE_KUBE_IMAGE_REGISTRY must be set in .env}"
VENDOR="${DATASPOKE_KUBE_CLOUD_VENDOR:-}"
IMAGE="${REGISTRY}/${NAME}:${TAG}"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

info "Building image: ${IMAGE}"
info "  Dockerfile: ${DOCKERFILE_PATH}"
info "  Vendor:     ${VENDOR:-local}"

# ---------------------------------------------------------------------------
# Build + push based on cloud vendor
# ---------------------------------------------------------------------------
case "${VENDOR}" in
  GCP|gcp)
    # Extract GCP project ID from the registry URL:
    #   <region>-docker.pkg.dev/<project>/<repo>  →  <project>
    GCP_PROJECT=$(echo "${REGISTRY}" | sed -n 's|.*-docker\.pkg\.dev/\([^/]*\)/.*|\1|p')
    if [[ -z "${GCP_PROJECT}" ]]; then
      error "Could not extract GCP project from DATASPOKE_KUBE_IMAGE_REGISTRY (${REGISTRY}). Expected format: <region>-docker.pkg.dev/<project>/<repo>"
    fi

    info "Building ${IMAGE} via Google Cloud Build (project: ${GCP_PROJECT})..."
    gcloud builds submit "${PROJECT_ROOT}" \
      --config /dev/stdin \
      --project "${GCP_PROJECT}" \
      --substitutions="_IMAGE=${IMAGE}" \
      --quiet <<CLOUDBUILD
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', '\${_IMAGE}', '-f', '${DOCKERFILE_PATH}', '.']
images: ['\${_IMAGE}']
CLOUDBUILD
    ;;

  AWS|aws)
    error "AWS remote build is not yet implemented. Set DATASPOKE_KUBE_CLOUD_VENDOR=GCP or leave empty to use local Docker."
    ;;

  *)
    # Fallback: local Docker build + push (requires Docker daemon)
    info "Building ${IMAGE} via local Docker..."
    docker build -t "${IMAGE}" -f "${PROJECT_ROOT}/${DOCKERFILE_PATH}" "${PROJECT_ROOT}"
    info "Pushing ${IMAGE}..."
    docker push "${IMAGE}"
    ;;
esac

info "Done. Image: ${IMAGE}"
