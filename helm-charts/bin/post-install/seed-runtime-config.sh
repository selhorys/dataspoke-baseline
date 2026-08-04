#!/usr/bin/env bash
# Seed LLM inference settings into the runtime config table via the internal
# admin API. The profile comes from the env file's own variable names through
# the shared seed_profile (bin/lib/helpers.sh), so `ENV_FILE=` alone picks it
# and there is no profile flag:
#   dev  — DATASPOKE_DEV_LLM_{PROVIDER,MODEL}, followed by a second PATCH that
#          turns the four stub_* dependency flags on. That second PATCH is
#          unconditional on this path: a dev deployment runs on the stub
#          Redis, LLM, pgvector manager and notification service whether or
#          not its LLM block is filled in.
#   prod — DATASPOKE_PROD_LLM_{PROVIDER,MODEL,API_KEY}, and no stub_* flag at
#          all: the toggles are a dev mechanism, and a production deployment
#          answering 200 off a stub Redis, LLM, pgvector manager or
#          notification service fails invisibly.
#
# Auth: the API pod's own DATASPOKE_INTERNAL_TOKEN, read from inside the pod
# by api_internal_request (bin/lib/helpers.sh) and sent as X-Internal-Token —
# never extracted to this machine.
# Endpoint: PATCH /internal/admin/conf, reached over the API's own loopback
# port from inside its pod (no ingress, no DNS). The LLM API key rides in that
# payload on stdin, and the API routes it into dataspoke-llm-secret itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$(cd "$BIN_DIR/.." && pwd)/.env.dev}"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# install.sh checks these for the whole install, but this script is also run
# standalone (`ENV_FILE=… bash bin/post-install/seed-runtime-config.sh`), where
# a missing interpreter would otherwise surface as `command not found` inside a
# command substitution followed by a bare exit.
require_tools kubectl python3

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example (dev) or helm-charts/.env.prod.example (prod) and edit it."
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Payload construction and transport
# ---------------------------------------------------------------------------
# build_payload <field>=<ENV_VAR> [...]
# Serialise the named fields into a JSON object with json.dumps, so a quote or
# a backslash in an API key stays a value instead of producing a 422 naming a
# field the operator never typed. An absent variable and an empty one are
# treated identically — the field is omitted, which the API reads as "leave
# unchanged"; an empty string would be a *clearing* write for llm_api_key.
#
# Only the variables a payload names are exported, and only into the command
# substitution this function always runs inside, so the credentials the env
# file carries for other purposes never enter any child process's environment.
build_payload() {
  local spec
  for spec in "$@"; do
    # Indirect export of the variable *named* by the argument's right-hand
    # side, which is what this needs.
    # shellcheck disable=SC2163
    export "${spec#*=}"
  done
  python3 - "$@" <<'PYEOF'
import json, os, sys

payload = {}
for arg in sys.argv[1:]:
    field, _, var = arg.partition("=")
    value = os.environ.get(var, "")
    if value != "":
        payload[field] = value
sys.stdout.write(json.dumps(payload))
PYEOF
}

# patch_conf <payload> <description> [stdin]
# PATCH /internal/admin/conf and translate the helper's status line. `stdin`
# set to 1 routes the payload through API_INTERNAL_REQUEST_BODY_STDIN so a
# credential in it never reaches argv; the assignment lives inside the command
# substitution's subshell, so it cannot leak into a later call.
patch_conf() {
  local payload="$1" description="$2" use_stdin="${3:-0}"
  local response http_code body
  if [[ "$use_stdin" == "1" ]]; then
    response="$(API_INTERNAL_REQUEST_BODY_STDIN=1 api_internal_request "${NS}" PATCH "/internal/admin/conf" "${payload}")"
  else
    response="$(api_internal_request "${NS}" PATCH "/internal/admin/conf" "${payload}")"
  fi
  http_code="$(printf '%s\n' "$response" | head -n1)"
  body="$(printf '%s\n' "$response" | tail -n +2)"
  case "$http_code" in
    200|204)
      info "OK (HTTP ${http_code}): ${description}"
      ;;
    000)
      error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
      ;;
    *)
      # The rejected value is stripped from the reported detail: a 422 on
      # llm_api_key would otherwise print the key itself.
      error "PATCH failed (HTTP ${http_code}) from /internal/admin/conf. Response detail: $(printf '%s' "$body" | api_error_detail)"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------
case "$(seed_profile "$ENV_FILE")" in
  ambiguous)
    error "$ENV_FILE declares both DATASPOKE_PROD_* and DATASPOKE_DEV_* variables — the profile is ambiguous, and guessing it decides whether this deployment runs on stub services. Keep one block per env file."
    ;;

  prod)
    # -------------------------------------------------------------------------
    # Prod: LLM provider/model/key, no stub_* flag
    # -------------------------------------------------------------------------
    PROVIDER="${DATASPOKE_PROD_LLM_PROVIDER:-}"
    MODEL="${DATASPOKE_PROD_LLM_MODEL:-}"
    API_KEY="${DATASPOKE_PROD_LLM_API_KEY:-}"

    if [[ -z "${PROVIDER}${MODEL}${API_KEY}" ]]; then
      info "No DATASPOKE_PROD_LLM_* value set in $ENV_FILE — nothing to seed, skipping. Every LLM-backed feature stays unavailable until a provider, model and key are set (this script, or PATCH /api/v1/admin/conf)."
    else
      if [[ -n "$PROVIDER" && -z "$MODEL" ]]; then
        error "DATASPOKE_PROD_LLM_PROVIDER is set but DATASPOKE_PROD_LLM_MODEL is empty — the inference loop needs both, and a provider alone would leave every LLM call unroutable."
      fi
      if [[ -z "$PROVIDER" && -n "$MODEL" ]]; then
        error "DATASPOKE_PROD_LLM_MODEL is set but DATASPOKE_PROD_LLM_PROVIDER is empty — the inference loop needs both."
      fi
      if [[ -z "$API_KEY" ]]; then
        warn "DATASPOKE_PROD_LLM_API_KEY is empty — leaving the stored key unchanged. Every LLM-backed feature fails until a key is set (this script, or PATCH /api/v1/admin/conf)."
      fi

      info "Seeding prod LLM settings into runtime config via /internal/admin/conf..."
      PAYLOAD="$(build_payload \
        llm_provider=DATASPOKE_PROD_LLM_PROVIDER \
        llm_model=DATASPOKE_PROD_LLM_MODEL \
        llm_api_key=DATASPOKE_PROD_LLM_API_KEY)"
      # The key rides in this payload, so it goes over stdin rather than argv.
      patch_conf "$PAYLOAD" "Runtime config seeded (provider=${PROVIDER:-<unchanged>} model=${MODEL:-<unchanged>})." 1
    fi
    ;;

  dev)
    # -------------------------------------------------------------------------
    # Dev: LLM provider/model, then the stub dependency flags
    # -------------------------------------------------------------------------
    if [[ -n "${DATASPOKE_DEV_LLM_PROVIDER:-}" && -n "${DATASPOKE_DEV_LLM_MODEL:-}" ]]; then
      info "Seeding dev LLM provider/model into runtime config via /internal/admin/conf..."
      PAYLOAD="$(build_payload \
        llm_provider=DATASPOKE_DEV_LLM_PROVIDER \
        llm_model=DATASPOKE_DEV_LLM_MODEL)"
      patch_conf "$PAYLOAD" "Runtime config seeded (provider=${DATASPOKE_DEV_LLM_PROVIDER} model=${DATASPOKE_DEV_LLM_MODEL})."
    else
      info "DATASPOKE_DEV_LLM_PROVIDER or DATASPOKE_DEV_LLM_MODEL not set — skipping LLM provider/model PATCH."
    fi

    # The dev API key reaches the deployment as dataspoke-llm-secret, created by
    # install.sh before the API pod starts, so it is not sent through this route.
    info "Seeding stub service flags into runtime config via /internal/admin/conf..."
    patch_conf '{"stub_redis_client": true, "stub_llm_client": true, "stub_pgvector_manager": true, "stub_notification_service": true}' \
      "Stub service flags seeded."
    ;;

  *)
    info "$ENV_FILE declares no DATASPOKE_PROD_* or DATASPOKE_DEV_* variable, so it names no runtime-config source — skipping."
    ;;
esac
