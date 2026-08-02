#!/usr/bin/env bash
# Seed DataHub and Langfuse peripheral connection config via the internal
# admin API. Reads required connection fields and optional operator metadata
# fields from .env; secret fields (DataHub PAT, Langfuse secret key) are
# already in K8s Secrets placed by install.sh.
#
# Auth: the API pod's own DATASPOKE_INTERNAL_TOKEN, read from inside the pod
# by api_internal_request (bin/lib/helpers.sh) and sent as X-Internal-Token —
# never extracted to this machine.
# Transport: PATCH /internal/admin/peripherals/{datahub,langfuse}, reached
# over the API's own loopback port from inside its pod (no ingress host in
# the request URL). The ingress scheme+domain still appear below, but only
# as payload data — the browser-facing DataHub UI URL this script PATCHes
# into peripheral_config, not as anything this script connects to.
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
  error "DATASPOKE_KUBE_INGRESS_DOMAIN not set in .env — needed to build the DataHub frontend_url payload field."
fi
SCHEME="$(ingress_scheme)"

# The API runs in-cluster, so its peripheral_config must hold the
# in-cluster service DNS (not the laptop-facing ingress URL .env also stores).
DATAHUB_GMS_INCLUSTER="http://datahub-datahub-gms.${DATAHUB_NS}.svc.cluster.local:8080"
DATAHUB_KAFKA_INCLUSTER="datahub-prerequisites-kafka.${DATAHUB_NS}.svc.cluster.local:9092"

# This is the sole place the browser-facing DataHub UI URL is set: the DB
# `peripheral_config` row's frontend_url is the only source the frontend reads
# for the DataHub deep link, and it cannot be derived from the in-cluster GMS
# address above (different host/port/scheme in real deployments) — it is
# computed here from the same ingress scheme+domain as every other ingress host.
DATAHUB_FRONTEND_URL="${SCHEME}://datahub.${DOMAIN}"

# ---------------------------------------------------------------------------
# Seed DataHub peripheral config (required fields + optional operator metadata)
# ---------------------------------------------------------------------------
info "Seeding DataHub connection into peripheral config via /internal/admin/peripherals/datahub..."
datahub_payload="{\"gms_url\": \"${DATAHUB_GMS_INCLUSTER}\", \"kafka_brokers\": \"${DATAHUB_KAFKA_INCLUSTER}\", \"frontend_url\": \"${DATAHUB_FRONTEND_URL}\""
[[ -n "${DATASPOKE_DEV_DATAHUB_SERVICE_CORPUSER_URN:-}" ]] && datahub_payload+=", \"service_corpuser_urn\": \"${DATASPOKE_DEV_DATAHUB_SERVICE_CORPUSER_URN}\""
[[ -n "${DATASPOKE_DEV_DATAHUB_DEFAULT_ENV:-}" ]]          && datahub_payload+=", \"default_env\": \"${DATASPOKE_DEV_DATAHUB_DEFAULT_ENV}\""
datahub_payload+="}"
RESPONSE="$(api_internal_request "${NS}" PATCH "/internal/admin/peripherals/datahub" "${datahub_payload}")"
HTTP_CODE="$(printf '%s\n' "$RESPONSE" | head -n1)"
BODY="$(printf '%s\n' "$RESPONSE" | tail -n +2)"
case "$HTTP_CODE" in
  200|204)
    info "OK (HTTP ${HTTP_CODE}): DataHub peripheral config seeded."
    ;;
  000)
    error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
    ;;
  *)
    error "PATCH failed (HTTP ${HTTP_CODE}) from /internal/admin/peripherals/datahub. Response body: ${BODY}"
    ;;
esac

# ---------------------------------------------------------------------------
# Seed Langfuse peripheral config (required fields + optional operator metadata)
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_TEST_LANGFUSE_HOST:-}" && -n "${DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY:-}" ]]; then
  info "Seeding Langfuse connection into peripheral config via /internal/admin/peripherals/langfuse..."
  langfuse_payload="{\"host\": \"${DATASPOKE_TEST_LANGFUSE_HOST}\", \"public_key\": \"${DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY}\""
  # Same `:-dataspoke-project` default langfuse.sh creates the project under, so
  # the seeded project_id cannot diverge from the project that actually exists.
  langfuse_payload+=", \"project_id\": \"${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID:-dataspoke-project}\""
  [[ -n "${DATASPOKE_DEV_LANGFUSE_ENVIRONMENT_TAG:-}" ]]  && langfuse_payload+=", \"environment_tag\": \"${DATASPOKE_DEV_LANGFUSE_ENVIRONMENT_TAG}\""
  langfuse_payload+="}"
  RESPONSE="$(api_internal_request "${NS}" PATCH "/internal/admin/peripherals/langfuse" "${langfuse_payload}")"
  HTTP_CODE="$(printf '%s\n' "$RESPONSE" | head -n1)"
  BODY="$(printf '%s\n' "$RESPONSE" | tail -n +2)"
  case "$HTTP_CODE" in
    200|204)
      info "OK (HTTP ${HTTP_CODE}): Langfuse peripheral config seeded."
      ;;
    000)
      error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
      ;;
    *)
      error "PATCH failed (HTTP ${HTTP_CODE}) from /internal/admin/peripherals/langfuse. Response body: ${BODY}"
      ;;
  esac
else
  info "DATASPOKE_TEST_LANGFUSE_HOST or DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY not set — skipping Langfuse peripheral PATCH."
fi
