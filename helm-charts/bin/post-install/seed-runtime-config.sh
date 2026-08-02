#!/usr/bin/env bash
# Seed LLM provider and model into the runtime config table via the internal
# admin API. Source values: DATASPOKE_DEV_LLM_{PROVIDER,MODEL} from .env.
#
# Auth: the API pod's own DATASPOKE_INTERNAL_TOKEN, read from inside the pod
# by api_internal_request (bin/lib/helpers.sh) and sent as X-Internal-Token —
# never extracted to this machine.
# Endpoint: PATCH /internal/admin/conf, reached over the API's own loopback
# port from inside its pod (no ingress, no DNS).
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

# ---------------------------------------------------------------------------
# Seed LLM provider/model into runtime config
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_DEV_LLM_PROVIDER:-}" && -n "${DATASPOKE_DEV_LLM_MODEL:-}" ]]; then
  info "Seeding dev LLM provider/model into runtime config via /internal/admin/conf..."
  RESPONSE="$(api_internal_request "${NS}" PATCH "/internal/admin/conf" "{\"llm_provider\": \"${DATASPOKE_DEV_LLM_PROVIDER}\", \"llm_model\": \"${DATASPOKE_DEV_LLM_MODEL}\"}")"
  HTTP_CODE="$(printf '%s\n' "$RESPONSE" | head -n1)"
  BODY="$(printf '%s\n' "$RESPONSE" | tail -n +2)"
  case "$HTTP_CODE" in
    200|204)
      info "OK (HTTP ${HTTP_CODE}): Runtime config seeded (provider=${DATASPOKE_DEV_LLM_PROVIDER} model=${DATASPOKE_DEV_LLM_MODEL})."
      ;;
    000)
      error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
      ;;
    *)
      error "PATCH failed (HTTP ${HTTP_CODE}) from /internal/admin/conf. Response body: ${BODY}"
      ;;
  esac
else
  info "DATASPOKE_DEV_LLM_PROVIDER or DATASPOKE_DEV_LLM_MODEL not set — skipping LLM provider/model PATCH."
fi

# ---------------------------------------------------------------------------
# Seed stub service flags for dev
# ---------------------------------------------------------------------------
info "Seeding stub service flags into runtime config via /internal/admin/conf..."
RESPONSE="$(api_internal_request "${NS}" PATCH "/internal/admin/conf" '{"stub_redis_client": true, "stub_llm_client": true, "stub_pgvector_manager": true, "stub_notification_service": true}')"
HTTP_CODE="$(printf '%s\n' "$RESPONSE" | head -n1)"
BODY="$(printf '%s\n' "$RESPONSE" | tail -n +2)"
case "$HTTP_CODE" in
  200|204)
    info "OK (HTTP ${HTTP_CODE}): Stub service flags seeded."
    ;;
  000)
    error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
    ;;
  *)
    error "PATCH failed (HTTP ${HTTP_CODE}) from /internal/admin/conf. Response body: ${BODY}"
    ;;
esac
