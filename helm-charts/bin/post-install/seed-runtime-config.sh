#!/usr/bin/env bash
# Seed LLM provider and model into the runtime config table via the internal
# admin API. Source values: DATASPOKE_DEV_LLM_{PROVIDER,MODEL} from .env.
#
# Auth: retrieves DATASPOKE_INTERNAL_TOKEN from the running API pod.
# Endpoint: http://app.<DOMAIN>/internal/admin/conf
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

# ---------------------------------------------------------------------------
# Seed LLM provider/model into runtime config
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_DEV_LLM_PROVIDER:-}" && -n "${DATASPOKE_DEV_LLM_MODEL:-}" ]]; then
  info "Seeding dev LLM provider/model into runtime config via /internal/admin/conf..."
  HTTP_CODE=$(curl -fsS -o /tmp/seed-resp.json -w "%{http_code}" -X PATCH \
    "http://app.${DOMAIN}/internal/admin/conf" \
    -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"llm_provider\": \"${DATASPOKE_DEV_LLM_PROVIDER}\", \"llm_model\": \"${DATASPOKE_DEV_LLM_MODEL}\"}" \
    2>&1 || echo "000")
  case "$HTTP_CODE" in
    200|204)
      info "OK (HTTP ${HTTP_CODE}): Runtime config seeded (provider=${DATASPOKE_DEV_LLM_PROVIDER} model=${DATASPOKE_DEV_LLM_MODEL})."
      ;;
    *)
      error "PATCH failed (HTTP ${HTTP_CODE}): http://app.${DOMAIN}/internal/admin/conf — see /tmp/seed-resp.json"
      ;;
  esac
else
  info "DATASPOKE_DEV_LLM_PROVIDER or DATASPOKE_DEV_LLM_MODEL not set — skipping runtime config PATCH."
fi
