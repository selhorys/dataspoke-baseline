#!/usr/bin/env bash
# Build + push the custom DataSpoke PostgreSQL image (pgvector + Apache AGE).
# Modeled after dev_env/dataspoke-airflow/build.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=../lib/helpers.sh
source "$SCRIPT_DIR/../lib/helpers.sh"

if [[ ! -f "$SCRIPT_DIR/../.env" ]]; then
  error ".env not found at $SCRIPT_DIR/../.env — run from dev_env/ and ensure .env exists."
fi
source "$SCRIPT_DIR/../.env"

REGISTRY="${DATASPOKE_DEV_IMAGE_REGISTRY:?Set DATASPOKE_DEV_IMAGE_REGISTRY in .env}"
VENDOR="${DATASPOKE_DEV_CLOUD_VENDOR:-}"
TAG="${1:-dev}"
IMAGE="${REGISTRY}/postgres:${TAG}"
PROJECT_ROOT="$SCRIPT_DIR/../.."

case "${VENDOR}" in
  GCP|gcp)
    GCP_PROJECT=$(echo "${REGISTRY}" | sed -n 's|.*-docker\.pkg\.dev/\([^/]*\)/.*|\1|p')
    if [[ -z "${GCP_PROJECT}" ]]; then
      error "Could not extract GCP project from DATASPOKE_DEV_IMAGE_REGISTRY (${REGISTRY})."
    fi

    info "Building ${IMAGE} via Google Cloud Build (project: ${GCP_PROJECT})..."
    gcloud builds submit "${PROJECT_ROOT}" \
      --config /dev/stdin \
      --project "${GCP_PROJECT}" \
      --substitutions="_IMAGE=${IMAGE}" \
      --quiet <<'CLOUDBUILD'
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', '${_IMAGE}', '-f', 'docker-images/postgres/Dockerfile', '.']
images: ['${_IMAGE}']
CLOUDBUILD
    ;;

  AWS|aws)
    error "AWS remote build is not yet implemented. Set DATASPOKE_DEV_CLOUD_VENDOR=GCP or use local Docker."
    ;;

  *)
    info "Building ${IMAGE} via local Docker..."
    docker build -t "${IMAGE}" -f "${PROJECT_ROOT}/docker-images/postgres/Dockerfile" "${PROJECT_ROOT}"
    info "Pushing ${IMAGE}..."
    docker push "${IMAGE}"
    ;;
esac

info "Done. Image: ${IMAGE}"
