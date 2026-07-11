#!/usr/bin/env bash
# Seed DataHub and Langfuse peripheral connection config via the internal
# admin API. Reads required connection fields and optional operator metadata
# fields from .env; secret fields (DataHub PAT, Langfuse secret key) are
# already in K8s Secrets placed by install.sh.
#
# Auth: retrieves DATASPOKE_INTERNAL_TOKEN from the running API pod.
# Endpoint: <scheme>://api.<DOMAIN>/internal/admin/peripherals/{datahub,langfuse}
# (scheme per DATASPOKE_KUBE_INGRESS_SCHEME, default http)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$(cd "$BIN_DIR/.." && pwd)/.env.dev}"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example and edit it."
fi
source "$ENV_FILE"

NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
DATAHUB_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"

if [[ -z "$DOMAIN" ]]; then
  error "DATASPOKE_KUBE_INGRESS_DOMAIN not set in .env — cannot reach the admin API."
fi
SCHEME="$(ingress_scheme)"

# The API runs in-cluster, so its peripheral_config must hold the
# in-cluster service DNS (not the ingress URL that .env now stores for
# laptop consumers).
DATAHUB_GMS_INCLUSTER="http://datahub-datahub-gms.${DATAHUB_NS}.svc.cluster.local:8080"
DATAHUB_KAFKA_INCLUSTER="datahub-prerequisites-kafka.${DATAHUB_NS}.svc.cluster.local:9092"

# ---------------------------------------------------------------------------
# Retrieve internal token from the running API pod
# ---------------------------------------------------------------------------
info "Retrieving DATASPOKE_INTERNAL_TOKEN from dataspoke-api pod..."
INTERNAL_TOKEN="$(kubectl exec -n "${NS}" deploy/dataspoke-api -c api -- \
  printenv DATASPOKE_INTERNAL_TOKEN 2>/dev/null || true)"

if [[ -z "$INTERNAL_TOKEN" ]]; then
  error "Could not read DATASPOKE_INTERNAL_TOKEN from dataspoke-api pod — is the API running?"
fi
info "Internal token retrieved."

BASE_URL="${SCHEME}://api.${DOMAIN}/internal/admin/peripherals"

# ---------------------------------------------------------------------------
# Seed DataHub peripheral config (required fields + optional operator metadata)
# ---------------------------------------------------------------------------
info "Seeding DataHub connection into peripheral config via ${BASE_URL}/datahub..."
datahub_payload="{\"gms_url\": \"${DATAHUB_GMS_INCLUSTER}\", \"kafka_brokers\": \"${DATAHUB_KAFKA_INCLUSTER}\""
[[ -n "${DATASPOKE_DEV_DATAHUB_SERVICE_CORPUSER_URN:-}" ]] && datahub_payload+=", \"service_corpuser_urn\": \"${DATASPOKE_DEV_DATAHUB_SERVICE_CORPUSER_URN}\""
[[ -n "${DATASPOKE_DEV_DATAHUB_DEFAULT_ENV:-}" ]]          && datahub_payload+=", \"default_env\": \"${DATASPOKE_DEV_DATAHUB_DEFAULT_ENV}\""
datahub_payload+="}"
HTTP_CODE=$(curl -fsS -o /tmp/seed-resp.json -w "%{http_code}" -X PATCH \
  "${BASE_URL}/datahub" \
  -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${datahub_payload}" \
  2>&1 || echo "000")
case "$HTTP_CODE" in
  200|204)
    info "OK (HTTP ${HTTP_CODE}): DataHub peripheral config seeded."
    ;;
  *)
    error "PATCH failed (HTTP ${HTTP_CODE}): ${BASE_URL}/datahub — see /tmp/seed-resp.json"
    ;;
esac

# ---------------------------------------------------------------------------
# Seed Langfuse peripheral config (required fields + optional operator metadata)
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_TEST_LANGFUSE_HOST:-}" && -n "${DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY:-}" ]]; then
  info "Seeding Langfuse connection into peripheral config via ${BASE_URL}/langfuse..."
  langfuse_payload="{\"host\": \"${DATASPOKE_TEST_LANGFUSE_HOST}\", \"public_key\": \"${DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY}\""
  [[ -n "${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID:-}" ]]  && langfuse_payload+=", \"project_id\": \"${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID}\""
  [[ -n "${DATASPOKE_DEV_LANGFUSE_ENVIRONMENT_TAG:-}" ]]  && langfuse_payload+=", \"environment_tag\": \"${DATASPOKE_DEV_LANGFUSE_ENVIRONMENT_TAG}\""
  langfuse_payload+="}"
  HTTP_CODE=$(curl -fsS -o /tmp/seed-resp.json -w "%{http_code}" -X PATCH \
    "${BASE_URL}/langfuse" \
    -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${langfuse_payload}" \
    2>&1 || echo "000")
  case "$HTTP_CODE" in
    200|204)
      info "OK (HTTP ${HTTP_CODE}): Langfuse peripheral config seeded."
      ;;
    *)
      error "PATCH failed (HTTP ${HTTP_CODE}): ${BASE_URL}/langfuse — see /tmp/seed-resp.json"
      ;;
  esac
else
  info "DATASPOKE_TEST_LANGFUSE_HOST or DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY not set — skipping Langfuse peripheral PATCH."
fi
