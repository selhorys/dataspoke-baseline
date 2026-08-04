#!/usr/bin/env bash
# Seed DataHub and Langfuse peripheral connection config via the internal
# admin API. The profile comes from the env file's own variable names through
# the shared seed_profile (bin/lib/helpers.sh) — the same decision this
# directory's other seeds make from the same file, so `ENV_FILE=` alone picks
# it and there is no profile flag:
#   dev  — derives the connection from the dev peripheral topology (in-cluster
#          DataHub GMS and Kafka DNS, the DataHub UI host on the ingress
#          domain) and sends no secret field: the dev DataHub PAT and Langfuse
#          secret key are placed into K8s Secrets by install.sh before the API
#          pod starts.
#   prod — takes the operator's connection verbatim from
#          DATASPOKE_PROD_PERIPHERAL_*, secret fields included, and lets the
#          API route the DataHub PAT, the Kafka SASL password and the Langfuse
#          secret key into dataspoke-{datahub,langfuse}-secret itself. This
#          script creates no Secret.
#
# Each env var suffix is the API contract field it carries, upper-cased —
# src/api/schemas/admin.py (DatahubPeripheralPatchRequest,
# LangfusePeripheralPatchRequest) is the single authority for the field names.
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

# install.sh checks these for the whole install, but this script is also run
# standalone (`ENV_FILE=… bash bin/post-install/seed-peripheral-config.sh`),
# where a missing interpreter would otherwise surface as `command not found`
# inside a command substitution followed by a bare exit.
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
# a backslash in a DataHub PAT stays a value instead of producing a 422 naming
# a field the operator never typed. An absent variable and an empty one are
# treated identically — the field is omitted, which the API reads as "leave
# unchanged"; an empty string is a *clearing* write, which for token,
# kafka_sasl_password or secret_key would silently unset a working credential.
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

# patch_peripheral <peripheral> <payload> <description> [stdin]
# PATCH /internal/admin/peripherals/<peripheral> and translate the helper's
# status line. `stdin` set to 1 routes the payload through
# API_INTERNAL_REQUEST_BODY_STDIN so a credential in it never reaches argv;
# the assignment lives inside the command substitution's subshell, so it
# cannot leak into a later call.
patch_peripheral() {
  local peripheral="$1" payload="$2" description="$3" use_stdin="${4:-0}"
  local path="/internal/admin/peripherals/${peripheral}"
  local response http_code body
  if [[ "$use_stdin" == "1" ]]; then
    response="$(API_INTERNAL_REQUEST_BODY_STDIN=1 api_internal_request "${NS}" PATCH "${path}" "${payload}")"
  else
    response="$(api_internal_request "${NS}" PATCH "${path}" "${payload}")"
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
      # token, kafka_sasl_password or secret_key would otherwise print the
      # credential itself.
      error "PATCH failed (HTTP ${http_code}) from ${path}. Response detail: $(printf '%s' "$body" | api_error_detail)"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------
case "$(seed_profile "$ENV_FILE")" in
  ambiguous)
    error "$ENV_FILE declares both DATASPOKE_PROD_* and DATASPOKE_DEV_* variables — the profile is ambiguous, and guessing it decides whether a production deployment is pointed at the dev peripheral topology. Keep one block per env file."
    ;;

  prod)
    # -------------------------------------------------------------------------
    # Prod: the operator's connection, verbatim, secret fields included
    # -------------------------------------------------------------------------
    if [[ -z "${DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL:-}" ]]; then
      error "$ENV_FILE names the prod profile but DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL is empty — DataSpoke reads every dataset, tag and lineage through GMS, so there is nothing to seed. Fill the peripheral block in, or configure it through PATCH /api/v1/admin/peripherals/datahub."
    fi
    if [[ -z "${DATASPOKE_PROD_PERIPHERAL_DATAHUB_TOKEN:-}" ]]; then
      warn "DATASPOKE_PROD_PERIPHERAL_DATAHUB_TOKEN is empty — leaving the stored PAT unchanged. Without one, every DataHub read and write from DataSpoke is rejected by GMS."
    fi
    if [[ -z "${DATASPOKE_PROD_PERIPHERAL_DATAHUB_FRONTEND_URL:-}" ]]; then
      warn "DATASPOKE_PROD_PERIPHERAL_DATAHUB_FRONTEND_URL is empty — leaving the stored URL unchanged. It is the only source the frontend reads for its DataHub deep links, which stay unavailable until it is set."
    fi

    info "Seeding DataHub connection into peripheral config via /internal/admin/peripherals/datahub..."
    DATAHUB_PAYLOAD="$(build_payload \
      gms_url=DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL \
      frontend_url=DATASPOKE_PROD_PERIPHERAL_DATAHUB_FRONTEND_URL \
      token=DATASPOKE_PROD_PERIPHERAL_DATAHUB_TOKEN \
      kafka_brokers=DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_BROKERS \
      kafka_security_protocol=DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SECURITY_PROTOCOL \
      kafka_sasl_mechanism=DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_MECHANISM \
      kafka_sasl_username=DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_USERNAME \
      kafka_sasl_password=DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_PASSWORD \
      kafka_aws_region=DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_AWS_REGION \
      service_corpuser_urn=DATASPOKE_PROD_PERIPHERAL_DATAHUB_SERVICE_CORPUSER_URN \
      default_env=DATASPOKE_PROD_PERIPHERAL_DATAHUB_DEFAULT_ENV)"
    # Carries the PAT and the Kafka SASL password, so it goes over stdin.
    patch_peripheral datahub "$DATAHUB_PAYLOAD" "DataHub peripheral config seeded." 1

    LANGFUSE_PAYLOAD="$(build_payload \
      host=DATASPOKE_PROD_PERIPHERAL_LANGFUSE_HOST \
      public_key=DATASPOKE_PROD_PERIPHERAL_LANGFUSE_PUBLIC_KEY \
      secret_key=DATASPOKE_PROD_PERIPHERAL_LANGFUSE_SECRET_KEY \
      project_id=DATASPOKE_PROD_PERIPHERAL_LANGFUSE_PROJECT_ID \
      environment_tag=DATASPOKE_PROD_PERIPHERAL_LANGFUSE_ENVIRONMENT_TAG)"
    if [[ "$LANGFUSE_PAYLOAD" == "{}" ]]; then
      info "No DATASPOKE_PROD_PERIPHERAL_LANGFUSE_* value set — skipping Langfuse peripheral PATCH (tracing stays off)."
    else
      info "Seeding Langfuse connection into peripheral config via /internal/admin/peripherals/langfuse..."
      # Carries the Langfuse secret key, so it goes over stdin.
      patch_peripheral langfuse "$LANGFUSE_PAYLOAD" "Langfuse peripheral config seeded." 1
    fi
    ;;

  dev)
    # -------------------------------------------------------------------------
    # Dev: derived from the dev peripheral topology
    # -------------------------------------------------------------------------
    if [[ -z "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE:-}" ]]; then
      error "DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE is empty in $ENV_FILE — the in-cluster GMS and Kafka addresses this path seeds are built from it."
    fi
    DATAHUB_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
    DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"

    if [[ -z "$DOMAIN" ]]; then
      error "DATASPOKE_KUBE_INGRESS_DOMAIN not set in $ENV_FILE — needed to build the DataHub frontend_url payload field."
    fi
    SCHEME="$(ingress_scheme)"

    # The API runs in-cluster, so its peripheral_config must hold the
    # in-cluster service DNS (not the laptop-facing ingress URL .env also stores).
    export SEED_DATAHUB_GMS_URL="http://datahub-datahub-gms.${DATAHUB_NS}.svc.cluster.local:8080"
    export SEED_DATAHUB_KAFKA_BROKERS="datahub-prerequisites-kafka.${DATAHUB_NS}.svc.cluster.local:9092"

    # This is the sole place the browser-facing DataHub UI URL is set: the DB
    # `peripheral_config` row's frontend_url is the only source the frontend reads
    # for the DataHub deep link, and it cannot be derived from the in-cluster GMS
    # address above (different host/port/scheme in real deployments) — it is
    # computed here from the same ingress scheme+domain as every other ingress host.
    export SEED_DATAHUB_FRONTEND_URL="${SCHEME}://datahub.${DOMAIN}"

    info "Seeding DataHub connection into peripheral config via /internal/admin/peripherals/datahub..."
    DATAHUB_PAYLOAD="$(build_payload \
      gms_url=SEED_DATAHUB_GMS_URL \
      kafka_brokers=SEED_DATAHUB_KAFKA_BROKERS \
      frontend_url=SEED_DATAHUB_FRONTEND_URL \
      service_corpuser_urn=DATASPOKE_DEV_DATAHUB_SERVICE_CORPUSER_URN \
      default_env=DATASPOKE_DEV_DATAHUB_DEFAULT_ENV)"
    patch_peripheral datahub "$DATAHUB_PAYLOAD" "DataHub peripheral config seeded."

    if [[ -n "${DATASPOKE_DEV_LANGFUSE_HOST:-}" && -n "${DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY:-}" ]]; then
      info "Seeding Langfuse connection into peripheral config via /internal/admin/peripherals/langfuse..."
      # Same `:-dataspoke-project` default langfuse.sh creates the project under, so
      # the seeded project_id cannot diverge from the project that actually exists.
      export SEED_LANGFUSE_PROJECT_ID="${DATASPOKE_DEV_LANGFUSE_INIT_PROJECT_ID:-dataspoke-project}"
      LANGFUSE_PAYLOAD="$(build_payload \
        host=DATASPOKE_DEV_LANGFUSE_HOST \
        public_key=DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY \
        project_id=SEED_LANGFUSE_PROJECT_ID \
        environment_tag=DATASPOKE_DEV_LANGFUSE_ENVIRONMENT_TAG)"
      patch_peripheral langfuse "$LANGFUSE_PAYLOAD" "Langfuse peripheral config seeded."
    else
      info "DATASPOKE_DEV_LANGFUSE_HOST or DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY not set — skipping Langfuse peripheral PATCH."
    fi
    ;;

  *)
    error "$ENV_FILE declares no DATASPOKE_PROD_* or DATASPOKE_DEV_* variable, so it names no peripheral source. Fill in the peripheral block before seeding."
    ;;
esac
