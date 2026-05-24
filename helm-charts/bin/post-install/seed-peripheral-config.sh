#!/usr/bin/env bash
# Seed DataHub and Langfuse peripheral connection config via the internal
# admin API. Reads non-secret fields from .env; secret fields (DataHub PAT,
# Langfuse secret key) are already in K8s Secrets placed by install.sh.
#
# Auth: retrieves DATASPOKE_INTERNAL_TOKEN from the running API pod.
# Endpoint: http://app.<DOMAIN>/internal/admin/peripherals/{datahub,langfuse}
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$(cd "$BIN_DIR/.." && pwd)/.env"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE — copy helm-charts/.env.example and edit it."
fi
source "$ENV_FILE"

NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"

if [[ -z "$DOMAIN" ]]; then
  error "DATASPOKE_KUBE_INGRESS_DOMAIN not set in .env — cannot reach the admin API."
fi

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

BASE_URL="http://app.${DOMAIN}/internal/admin/peripherals"

# ---------------------------------------------------------------------------
# Seed DataHub peripheral config (non-secret fields only)
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_DEV_DATAHUB_GMS_URL:-}" && -n "${DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS:-}" ]]; then
  info "Seeding DataHub connection into peripheral config via ${BASE_URL}/datahub..."
  HTTP_CODE=$(curl -fsS -o /tmp/seed-resp.json -w "%{http_code}" -X PATCH \
    "${BASE_URL}/datahub" \
    -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"gms_url\": \"${DATASPOKE_DEV_DATAHUB_GMS_URL}\", \"kafka_brokers\": \"${DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS}\"}" \
    2>&1 || echo "000")
  case "$HTTP_CODE" in
    200|204)
      info "OK (HTTP ${HTTP_CODE}): DataHub peripheral config seeded."
      ;;
    *)
      error "PATCH failed (HTTP ${HTTP_CODE}): ${BASE_URL}/datahub — see /tmp/seed-resp.json"
      ;;
  esac
else
  info "DATASPOKE_DEV_DATAHUB_GMS_URL or DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS not set — skipping DataHub peripheral PATCH."
fi

# ---------------------------------------------------------------------------
# Seed Langfuse peripheral config (non-secret fields only)
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_DEV_LANGFUSE_HOST:-}" && -n "${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY:-}" ]]; then
  info "Seeding Langfuse connection into peripheral config via ${BASE_URL}/langfuse..."
  HTTP_CODE=$(curl -fsS -o /tmp/seed-resp.json -w "%{http_code}" -X PATCH \
    "${BASE_URL}/langfuse" \
    -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"host\": \"${DATASPOKE_DEV_LANGFUSE_HOST}\", \"public_key\": \"${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY}\"}" \
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
  info "DATASPOKE_DEV_LANGFUSE_HOST or DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY not set — skipping Langfuse peripheral PATCH."
fi
