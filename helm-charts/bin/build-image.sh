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
#   AWS|aws   → ECR login + ensure-repository + local docker build + push
#   empty     → local docker build + docker push
#
# AWS notes:
#   - Requires a local Docker daemon and the `aws` CLI.
#   - Auth uses DATASPOKE_AWS_PROFILE (required — errors if unset) and the
#     region parsed from the registry host (<acct>.dkr.ecr.<region>.amazonaws.com/...).
#   - The ECR repository is created on first use if it does not exist.
#   - Set DATASPOKE_DOCKER_SUDO=true to prefix docker with sudo (some hosts
#     require root for the Docker socket).
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
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env.dev}"
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example and edit it."
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
    require_tools aws docker

    # Registry host = everything before the first '/'.
    # Repository name = the remainder joined with the image name
    # (e.g. "dataspoke/api" from registry "123.dkr.ecr.us-east-1.amazonaws.com/dataspoke").
    ECR_HOST="${REGISTRY%%/*}"
    ECR_REPO="${REGISTRY#*/}/${NAME}"

    # Parse the AWS region from the ECR host:
    # expected format: <account-id>.dkr.ecr.<region>.amazonaws.com
    # Region charset is constrained (lowercase, digits, hyphens) so a malformed
    # registry host fails the parse below instead of yielding a junk region.
    ECR_REGION="$(echo "${ECR_HOST}" | sed -n 's|^[0-9]*\.dkr\.ecr\.\([a-z0-9-]\{1,\}\)\.amazonaws\.com$|\1|p')"
    if [[ -z "${ECR_REGION}" ]]; then
      error "Could not parse the AWS region from DATASPOKE_KUBE_IMAGE_REGISTRY (${REGISTRY}). Expected: <acct>.dkr.ecr.<region>.amazonaws.com/<repo-prefix>"
    fi

    # AWS CLI profile is required — no default, to avoid silently authenticating
    # against the wrong account/profile.
    if [[ -z "${DATASPOKE_AWS_PROFILE:-}" ]]; then
      error "DATASPOKE_AWS_PROFILE must be set in .env for AWS/ECR builds (the aws CLI profile used for ECR auth + repository creation)."
    fi
    AWS_PROFILE_ARG=(--profile "${DATASPOKE_AWS_PROFILE}")

    # docker may need sudo for the daemon socket on some hosts.
    DOCKER=(docker)
    [[ "${DATASPOKE_DOCKER_SUDO:-false}" == "true" ]] && DOCKER=(sudo docker)

    info "Logging in to ECR (${ECR_HOST}, region ${ECR_REGION})..."
    aws ecr get-login-password --region "${ECR_REGION}" "${AWS_PROFILE_ARG[@]}" \
        | "${DOCKER[@]}" login --username AWS --password-stdin "${ECR_HOST}"

    info "Ensuring ECR repository '${ECR_REPO}' exists..."
    if ! aws ecr describe-repositories --region "${ECR_REGION}" "${AWS_PROFILE_ARG[@]}" \
        --repository-names "${ECR_REPO}" >/dev/null 2>&1; then
      info "  Repository not found — creating '${ECR_REPO}'."
      aws ecr create-repository --region "${ECR_REGION}" "${AWS_PROFILE_ARG[@]}" \
          --repository-name "${ECR_REPO}" >/dev/null
    fi

    # Target platform must match the cluster nodes. Default linux/amd64 (EKS
    # nodes are x86_64); on an arm64 build host this cross-builds via QEMU
    # emulation. Override with DATASPOKE_IMAGE_PLATFORM (e.g. linux/arm64).
    PLATFORM="${DATASPOKE_IMAGE_PLATFORM:-linux/amd64}"
    info "Building ${IMAGE} via local Docker (platform ${PLATFORM})..."
    # Use the legacy builder (DOCKER_BUILDKIT=0) for cross-arch builds: it honors
    # --platform via QEMU binfmt emulation without requiring the buildx plugin,
    # which colima/plain-docker-engine hosts may not have installed.
    DOCKER_BUILDKIT=0 "${DOCKER[@]}" build --platform "${PLATFORM}" \
        -t "${IMAGE}" -f "${PROJECT_ROOT}/${DOCKERFILE_PATH}" "${PROJECT_ROOT}"
    info "Pushing ${IMAGE}..."
    "${DOCKER[@]}" push "${IMAGE}"
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
