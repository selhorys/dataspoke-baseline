#!/usr/bin/env bash
# DataSpoke installation entry point.
#
# Usage: install.sh --profile {dev|prod} [OPTIONS]
#
# OPTIONS
#   --env-file <path>           Path to the env file (default: helm-charts/.env.<PROFILE>).
#   --components <csv>          Subset of components to install (default: all-for-profile).
#                               Names: nginx-ingress, datahub, langfuse, dataspoke-infra,
#                                      api, frontend, dummy-data, dev-lock, seed
#   --from-component <n>        Resume an interrupted full install at <n>.
#   --frontend none|local|cluster
#                               Frontend deployment mode. Controls whether the Next.js
#                               frontend is deployed and how developers access it.
#                               none    — do not deploy; no image build. (dev default)
#                               local   — do not deploy; write src/frontend/.env.local
#                                         so `pnpm dev` points at the in-cluster API.
#                                         (dev only)
#                               cluster — build image and deploy in-cluster via Helm.
#                                         (prod default; also available in dev)
#   --skip-build                Skip Docker image rebuilds (api/airflow/postgres/frontend).
#   --skip-seed                 Skip post-install admin-API seeding (both profiles).
#   --values <path>             Extra values file for the umbrella chart (prod).
#   --image-tag <tag>           Override image tag (default: dev).
#   --help, -h                  Print this usage message.
#
# The --components api path rebuilds the API image, runs helm upgrade, and
# waits for rollout.
# The --components frontend path builds the frontend image, runs helm upgrade
# with frontend.enabled=true, and waits for rollout. In dev the default install
# keeps frontend.enabled=false (host pnpm dev); --components frontend explicitly
# deploys the containerised frontend in-cluster for verification.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HELM_CHARTS_DIR/.." && pwd)"
CHART_DIR="$HELM_CHARTS_DIR/dataspoke"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
PROFILE=""
ENV_FILE_ARG=""
COMPONENTS_CSV=""
FROM_COMPONENT=""
FRONTEND_MODE=""
SKIP_BUILD=false
SKIP_SEED=false
EXTRA_VALUES=""
IMAGE_TAG="dev"
IMAGE_TAG_EXPLICIT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)         PROFILE="${2:-}"; shift 2 ;;
    --env-file)        ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --components)      COMPONENTS_CSV="${2:-}"; shift 2 ;;
    --from-component)  FROM_COMPONENT="${2:-}"; shift 2 ;;
    --frontend)        FRONTEND_MODE="${2:-}"; shift 2 ;;
    --skip-build)      SKIP_BUILD=true; shift ;;
    --skip-seed)       SKIP_SEED=true; shift ;;
    --values)          EXTRA_VALUES="${2:-}"; shift 2 ;;
    --image-tag)       IMAGE_TAG="${2:-dev}"; IMAGE_TAG_EXPLICIT=true; shift 2 ;;
    --help|-h) print_usage; exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  error "--profile {dev|prod} is required. Use --help for usage."
fi
if [[ "$PROFILE" != "dev" && "$PROFILE" != "prod" ]]; then
  error "Invalid profile '${PROFILE}'. Must be 'dev' or 'prod'."
fi

# Apply per-profile defaults for FRONTEND_MODE
if [[ -z "$FRONTEND_MODE" ]]; then
  if [[ "$PROFILE" == "dev" ]]; then
    FRONTEND_MODE="none"
  else
    FRONTEND_MODE="cluster"
  fi
fi
# Validate FRONTEND_MODE
if [[ "$FRONTEND_MODE" != "none" && "$FRONTEND_MODE" != "local" && "$FRONTEND_MODE" != "cluster" ]]; then
  error "Invalid --frontend '${FRONTEND_MODE}'. Must be none|local|cluster."
fi
if [[ "$FRONTEND_MODE" == "local" && "$PROFILE" == "prod" ]]; then
  error "--frontend local is dev-only (no localhost story for prod)."
fi

# Resolve env file: explicit --env-file wins; otherwise profile-aware default.
ENV_FILE="${ENV_FILE_ARG:-$HELM_CHARTS_DIR/.env.$PROFILE}"
export ENV_FILE

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.${PROFILE}.example (or .env.dev.example for dev) and edit it."
fi
source "$ENV_FILE"
# Harden permissions immediately — the file may have been created via cp or
# an editor that inherits a permissive umask.
chmod 600 "$ENV_FILE" 2>/dev/null || true

# Every *_NAMESPACE var below is interpolated into `kubectl apply -f -` YAML
# documents throughout this script (metadata.name / metadata.namespace), so an
# unvalidated value could inject an arbitrary extra manifest. Kubernetes
# namespaces are DNS-1123 labels: lowercase alphanumeric or '-', starting and
# ending alphanumeric, max 63 chars. Checked once here rather than per call
# site, mirroring ingress_class()/ingress_tls_secret() in lib/helpers.sh.
_validate_namespace_var() {
  local var_name="$1"
  local val="${!var_name:-}"
  [[ -z "$val" ]] && return 0
  if [[ ! "$val" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || "${#val}" -gt 63 ]]; then
    error "Invalid ${var_name} '${val}'. Must be a valid Kubernetes namespace (DNS-1123 label: lowercase alphanumeric and '-', max 63 chars)."
  fi
}
_validate_namespace_var DATASPOKE_KUBE_DATASPOKE_NAMESPACE
_validate_namespace_var DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
_validate_namespace_var DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE
_validate_namespace_var DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE

START_TIME=$SECONDS
export START_TIME

echo ""
echo "=== DataSpoke installation (profile: ${PROFILE}) ==="
echo ""

# ---------------------------------------------------------------------------
# Pre-flight: required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm python3
info "kubectl, helm, and python3 are available."

# ---------------------------------------------------------------------------
# Per-install tempdir for background task logs (0700, cleaned on exit)
# ---------------------------------------------------------------------------
INSTALL_TMPDIR="$(mktemp -d -t dataspoke-install.XXXX)"
chmod 700 "$INSTALL_TMPDIR"
trap 'rm -rf "${INSTALL_TMPDIR}"' EXIT

# ---------------------------------------------------------------------------
# Shared helpers (used by both profile branches)
# ---------------------------------------------------------------------------
PIDS=()
LABELS=()

_run_bg() {
  local label="$1"; shift
  ( "$@" > "${INSTALL_TMPDIR}/${label//\//-}.log" 2>&1 ) &
  PIDS+=($!)
  LABELS+=("$label")
  info "  Started background task: $label (pid $!)"
}

_wait_all() {
  local failed=0
  for i in "${!PIDS[@]}"; do
    local pid="${PIDS[$i]}"
    local label="${LABELS[$i]}"
    if wait "$pid"; then
      info "  [OK] $label"
    else
      warn "  [FAIL] $label (exit $?)"
      cat "${INSTALL_TMPDIR}/${label//\//-}.log" >&2 || true
      (( failed++ ))
    fi
  done
  PIDS=()
  LABELS=()
  if (( failed > 0 )); then
    error "${failed} background task(s) failed — see output above."
  fi
}

# _build_chart_deps <chart_dir>
# `helm dependency build` does not re-package an unchanged-version file://
# dependency, so edits to a local subchart's templates would otherwise ship a
# stale package. Drop the local subchart archives first so the build re-packages
# them from the current source. This forces a full re-resolve, so the remote OCI
# deps (bitnami postgresql/redis) get re-pulled from Docker's CDN — a fetch that
# intermittently resets the connection. Retry the build to ride out those
# transient resets rather than failing the whole install on one bad pull.
_build_chart_deps() {
  local chart_dir="$1"
  rm -f "${chart_dir}"/charts/frontend-*.tgz "${chart_dir}"/charts/event-consumer-*.tgz
  local attempt
  for attempt in 1 2 3 4 5; do
    if helm dependency build "${chart_dir}"; then
      return 0
    fi
    warn "  helm dependency build failed (attempt ${attempt}/5) — retrying in 5s..."
    sleep 5
  done
  error "helm dependency build for '${chart_dir}' failed after 5 attempts."
}

# ---------------------------------------------------------------------------
# Secret management helpers
# ---------------------------------------------------------------------------

# _ensure_fernet_key_joins_credentials_secret <namespace> <secret_name>
# Dev-only self-heal for a credentials Secret that predates
# DATASPOKE_AIRFLOW_FERNET_KEY joining the 13-key contract: patches the key in
# rather than leaving the Secret on its old 12-key shape, which would
# otherwise hard-error later in _ensure_airflow_fernet_secret with no
# remediation. Adopts, in order: the key already projected into
# dataspoke-airflow-metadata-encryption-key (the new-name projection), then
# dataspoke-airflow-fernet-key (the Airflow subchart's own pre-install-hook
# Secret, from a release installed before fernetKeySecretName was pinned) —
# so a cluster that already ran Airflow keeps its stored connections and
# Variables decryptable. Generation is the last resort, only when neither
# exists. No-op once the key is present. Prod never calls this — the operator
# owns the pre-created Secret's shape; see _check_airflow_credentials_prod.
_ensure_fernet_key_joins_credentials_secret() {
  local ns="$1"
  local secret_name="$2"

  local existing
  existing="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
  [[ -n "${existing}" ]] && return 0

  info "'${secret_name}' predates DATASPOKE_AIRFLOW_FERNET_KEY joining the credentials contract — adding it."

  local fernet_key="" adopted_from=""
  if kubectl get secret dataspoke-airflow-metadata-encryption-key -n "${ns}" >/dev/null 2>&1; then
    fernet_key="$(kubectl get secret dataspoke-airflow-metadata-encryption-key -n "${ns}" \
      -o jsonpath='{.data.fernet-key}' | base64 --decode)"
    adopted_from="dataspoke-airflow-metadata-encryption-key"
  fi
  if [[ -z "${fernet_key}" ]] && kubectl get secret dataspoke-airflow-fernet-key -n "${ns}" >/dev/null 2>&1; then
    fernet_key="$(kubectl get secret dataspoke-airflow-fernet-key -n "${ns}" \
      -o jsonpath='{.data.fernet-key}' | base64 --decode)"
    adopted_from="dataspoke-airflow-fernet-key"
  fi

  if [[ -n "${fernet_key}" ]]; then
    info "  Adopting the Fernet key already live in this cluster (${adopted_from})."
  else
    info "  No live Fernet key found on this cluster — generating a new one."
    fernet_key="$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
  fi

  # --patch-file /dev/stdin (not -p with the value inlined) so the key
  # material never lands in this process's argv — matching every other
  # Secret write in this file and the ps-visibility guidance in README.md.
  kubectl patch secret "${secret_name}" -n "${ns}" --type=merge --patch-file /dev/stdin <<EOF
{"data":{"DATASPOKE_AIRFLOW_FERNET_KEY":"$(printf '%s' "${fernet_key}" | base64 | tr -d '\n')"}}
EOF

  # dataspoke-airflow-fernet-key is left in place even when adopted from —
  # it is not deleted here so a mid-upgrade pod restart never finds Airflow's
  # secretKeyRef pointing at a Secret that no longer exists (the umbrella
  # helm upgrade that re-points every Airflow component onto
  # dataspoke-airflow-metadata-encryption-key has not run yet at this point
  # in the install). helm-charts/bin/uninstall.sh deletes it alongside the
  # other chart-derived Airflow Secrets on both profiles.
}

# _ensure_dataspoke_secrets <namespace> <profile> [<secret_name>]
# Idempotent: creates the consolidated credential Secret in dev with
# auto-generated values (including Airflow webserver/jwt secrets).
# In prod: fails fast unless the Secret already exists.
# <secret_name> defaults to "dataspoke-secrets".
_ensure_dataspoke_secrets() {
  local ns="$1"
  local profile="$2"
  local secret_name="${3:-dataspoke-secrets}"

  if kubectl get secret "${secret_name}" -n "${ns}" >/dev/null 2>&1; then
    if [[ "$profile" == "dev" ]]; then
      # Dev may still self-heal a pre-existing Secret that predates the
      # 13-key contract (see _ensure_fernet_key_joins_credentials_secret), so
      # "leaving untouched" would be inaccurate here — that function logs its
      # own message on the patch path.
      info "'${secret_name}' already exists in '${ns}'."
      _ensure_fernet_key_joins_credentials_secret "${ns}" "${secret_name}"
    else
      info "'${secret_name}' already exists in '${ns}' — leaving untouched."
    fi
    return 0
  fi

  if [[ "$profile" == "prod" ]]; then
    error "prod install requires a pre-created K8s Secret named '${secret_name}'. Either:
  kubectl create secret generic ${secret_name} \\
    --from-literal=DATASPOKE_POSTGRES_USER=<u> \\
    --from-literal=DATASPOKE_POSTGRES_PASSWORD=<p> \\
    --from-literal=DATASPOKE_POSTGRES_DB=<db> \\
    --from-literal=DATASPOKE_REDIS_PASSWORD=<p> \\
    --from-literal=DATASPOKE_AIRFLOW_USER=<u> \\
    --from-literal=DATASPOKE_AIRFLOW_PASSWORD=<p> \\
    --from-literal=DATASPOKE_INTERNAL_TOKEN=<t> \\
    --from-literal=DATASPOKE_JWT_SECRET_KEY=<k> \\
    --from-literal=DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY=<k> \\
    --from-literal=DATASPOKE_AIRFLOW_JWT_SECRET=<k> \\
    --from-literal=DATASPOKE_AIRFLOW_FERNET_KEY=<f> \\
    --from-literal=DATASPOKE_OAUTH_STATE_SECRET=<k> \\
    --from-literal=DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET=<s> \\
    -n ${ns}
or pass --values <overlay.yaml> with secrets.existingSecret: <name>"
  fi

  local pg_user pg_password pg_db redis_password
  pg_user="dataspoke"
  pg_db="dataspoke"
  pg_password="$(openssl rand -hex 32)"
  redis_password="$(openssl rand -hex 32)"

  local airflow_password internal_token jwt_secret airflow_webserver_secret airflow_jwt_secret
  airflow_password="$(openssl rand -hex 32)"
  internal_token="$(openssl rand -hex 32)"
  jwt_secret="$(openssl rand -hex 32)"
  airflow_webserver_secret="$(openssl rand -hex 16)"
  airflow_jwt_secret="$(openssl rand -hex 16)"

  # OAuth state secret: auto-generated per install (random HMAC key).
  local oauth_state_secret
  oauth_state_secret="$(openssl rand -hex 32)"

  # Airflow Fernet key: encrypts connection secrets and Variables in Airflow's
  # metadata DB (the Postgres PVC), so it must not silently change while that
  # PVC survives — a fresh value would leave every stored connection and
  # Variable permanently undecryptable. `openssl rand -hex 32` (used for every
  # other key above) is the wrong shape: it decodes to 48 raw bytes, and
  # Fernet requires exactly 32. Generation is the last resort — first adopt
  # whatever key is already projected into
  # dataspoke-airflow-metadata-encryption-key on this cluster, so a
  # credentials Secret re-created by hand while the release is live keeps the
  # Postgres PVC's Airflow connections decryptable. This path — a fresh
  # `_ensure_dataspoke_secrets` create — does not cover a `bin/uninstall.sh`
  # dev teardown, which deletes the credentials Secret and this projection
  # together; see spec/feature/HELM_CHART.md §Dev — install-time provisioning.
  local airflow_fernet_key=""
  if kubectl get secret dataspoke-airflow-metadata-encryption-key -n "${ns}" >/dev/null 2>&1; then
    airflow_fernet_key="$(kubectl get secret dataspoke-airflow-metadata-encryption-key -n "${ns}" \
      -o jsonpath='{.data.fernet-key}' | base64 --decode)"
    [[ -n "${airflow_fernet_key}" ]] && info "  Adopting the Fernet key already projected into dataspoke-airflow-metadata-encryption-key."
  fi
  if [[ -z "${airflow_fernet_key}" ]] && kubectl get secret dataspoke-airflow-fernet-key -n "${ns}" >/dev/null 2>&1; then
    # The Airflow subchart's own pre-install-hook Secret, from a release
    # installed before fernetKeySecretName was pinned.
    airflow_fernet_key="$(kubectl get secret dataspoke-airflow-fernet-key -n "${ns}" \
      -o jsonpath='{.data.fernet-key}' | base64 --decode)"
    [[ -n "${airflow_fernet_key}" ]] && info "  Adopting the Fernet key already projected into dataspoke-airflow-fernet-key (legacy subchart hook Secret)."
  fi
  if [[ -z "${airflow_fernet_key}" ]]; then
    airflow_fernet_key="$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
  fi

  # Google OAuth client secret: sourced from DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET
  # in .env. Falls back to a placeholder if absent — the OAuth callback will fail
  # gracefully until the operator supplies a real value.
  local google_oauth_client_secret
  google_oauth_client_secret="${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET:-placeholder-set-google-oauth-secret-via-env}"

  info "Creating '${secret_name}' in namespace '${ns}'..."
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: ${secret_name}
  namespace: ${ns}
type: Opaque
data:
  DATASPOKE_POSTGRES_USER: $(printf '%s' "${pg_user}" | base64 | tr -d '\n')
  DATASPOKE_POSTGRES_PASSWORD: $(printf '%s' "${pg_password}" | base64 | tr -d '\n')
  DATASPOKE_POSTGRES_DB: $(printf '%s' "${pg_db}" | base64 | tr -d '\n')
  DATASPOKE_REDIS_PASSWORD: $(printf '%s' "${redis_password}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_USER: $(printf '%s' "dataspoke-admin" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_PASSWORD: $(printf '%s' "${airflow_password}" | base64 | tr -d '\n')
  DATASPOKE_INTERNAL_TOKEN: $(printf '%s' "${internal_token}" | base64 | tr -d '\n')
  DATASPOKE_JWT_SECRET_KEY: $(printf '%s' "${jwt_secret}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY: $(printf '%s' "${airflow_webserver_secret}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_JWT_SECRET: $(printf '%s' "${airflow_jwt_secret}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_FERNET_KEY: $(printf '%s' "${airflow_fernet_key}" | base64 | tr -d '\n')
  DATASPOKE_OAUTH_STATE_SECRET: $(printf '%s' "${oauth_state_secret}" | base64 | tr -d '\n')
  DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET: $(printf '%s' "${google_oauth_client_secret}" | base64 | tr -d '\n')
EOF
}

# _derive_airflow_metadata_secret <namespace> [<secret_name>]
# Reads DATASPOKE_POSTGRES_{USER,PASSWORD} from the consolidated Secret,
# builds the Airflow metadata connection URI, and applies
# dataspoke-airflow-metadata-db (key: connection). Idempotent.
# <secret_name> defaults to "dataspoke-secrets".
_derive_airflow_metadata_secret() {
  local ns="$1"
  local secret_name="${2:-dataspoke-secrets}"

  local pg_user pg_password
  pg_user="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_POSTGRES_USER}' | base64 --decode)"
  pg_password="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_POSTGRES_PASSWORD}' | base64 --decode)"

  local url_enc_pwd
  url_enc_pwd="$(printf '%s' "${pg_password}" | python3 -c \
    'import urllib.parse,sys; print(urllib.parse.quote(sys.stdin.read(),safe=""))')"

  local conn_uri="postgresql://${pg_user}:${url_enc_pwd}@dataspoke-postgresql:5432/airflow?sslmode=disable"

  info "Applying dataspoke-airflow-metadata-db..."
  if kubectl get secret dataspoke-airflow-metadata-db -n "${ns}" >/dev/null 2>&1; then
    info "  dataspoke-airflow-metadata-db already exists — skipping."
    return 0
  fi
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-metadata-db
  namespace: ${ns}
type: Opaque
data:
  connection: $(printf '%s' "${conn_uri}" | base64 | tr -d '\n')
EOF
}

# Set by _ensure_airflow_key_secrets when a derived Airflow key Secret was
# *updated* (source key rotated), as opposed to created on a fresh install.
# Read after the umbrella helm upgrade to decide whether to roll the pods that
# hold the old key in memory.
AIRFLOW_KEYS_ROTATED=false

# _ensure_airflow_key_secrets <namespace> <secret_name>
# Derives Airflow webserver/jwt secrets from the consolidated Secret and
# creates the two Airflow-chart-compatible Secrets. Idempotent.
_ensure_airflow_key_secrets() {
  local ns="$1"
  local secret_name="$2"

  AIRFLOW_KEYS_ROTATED=false

  local webserver_key jwt_key
  webserver_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY}' | base64 --decode)"
  jwt_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_JWT_SECRET}' | base64 --decode)"

  if [[ -z "${webserver_key}" || -z "${jwt_key}" ]]; then
    error "Secret '${secret_name}' is missing DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY or DATASPOKE_AIRFLOW_JWT_SECRET."
  fi

  local existing_api_secret_key=""
  if kubectl get secret dataspoke-airflow-api-secret-key -n "${ns}" >/dev/null 2>&1; then
    existing_api_secret_key="$(kubectl get secret dataspoke-airflow-api-secret-key -n "${ns}" \
      -o jsonpath='{.data.api-secret-key}' | base64 --decode)"
  fi
  if [[ "${existing_api_secret_key}" != "${webserver_key}" ]]; then
    if [[ -n "${existing_api_secret_key}" ]]; then
      AIRFLOW_KEYS_ROTATED=true
    fi
    info "Creating/updating dataspoke-airflow-api-secret-key..."
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-api-secret-key
  namespace: ${ns}
type: Opaque
data:
  api-secret-key: $(printf '%s' "${webserver_key}" | base64 | tr -d '\n')
EOF
  else
    info "  dataspoke-airflow-api-secret-key already up to date — skipping."
  fi

  local existing_jwt_secret=""
  if kubectl get secret dataspoke-airflow-jwt-secret -n "${ns}" >/dev/null 2>&1; then
    existing_jwt_secret="$(kubectl get secret dataspoke-airflow-jwt-secret -n "${ns}" \
      -o jsonpath='{.data.jwt-secret}' | base64 --decode)"
  fi
  if [[ "${existing_jwt_secret}" != "${jwt_key}" ]]; then
    if [[ -n "${existing_jwt_secret}" ]]; then
      AIRFLOW_KEYS_ROTATED=true
    fi
    info "Creating/updating dataspoke-airflow-jwt-secret..."
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-jwt-secret
  namespace: ${ns}
type: Opaque
data:
  jwt-secret: $(printf '%s' "${jwt_key}" | base64 | tr -d '\n')
EOF
  else
    info "  dataspoke-airflow-jwt-secret already up to date — skipping."
  fi

  if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" ]]; then
    warn "Airflow signing key rotated — the consuming pods will be restarted after the upgrade."
  fi
}

# _ensure_airflow_fernet_secret <namespace> <secret_name>
# Projects DATASPOKE_AIRFLOW_FERNET_KEY from the consolidated Secret into
# dataspoke-airflow-metadata-encryption-key (key: fernet-key), the single-key
# shape the Airflow chart's fernetKeySecretName expects. Idempotent, but —
# unlike _ensure_airflow_key_secrets — deliberately has no rotation branch:
# the two signing keys tolerate rotation (a mismatch is re-projected and the
# affected pods restarted, costing only live Airflow sessions), but the
# Fernet key encrypts Airflow's stored connections and Variables in the
# metadata DB, so re-projecting a changed value would leave that data
# permanently undecryptable with no recovery path. A live projection that
# disagrees with the source key therefore aborts the install with the
# recovery command instead of silently overwriting it. The comparison source
# falls back to dataspoke-airflow-fernet-key (the Airflow subchart's own
# pre-install-hook Secret) whenever dataspoke-airflow-metadata-encryption-key
# does not exist yet OR exists with an empty/absent fernet-key — gated on the
# read VALUE, not on Secret existence, so a cluster installed before
# fernetKeySecretName was pinned to the new name — where the new-name
# projection has never been created, or exists but was never populated —
# still gets a real comparison instead of an unchecked create. Mirrors
# _ensure_fernet_key_joins_credentials_secret's emptiness-gated fallback above.
_ensure_airflow_fernet_secret() {
  local ns="$1"
  local secret_name="$2"

  local fernet_key
  fernet_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' | base64 --decode)"

  if [[ -z "${fernet_key}" ]]; then
    error "Secret '${secret_name}' is missing DATASPOKE_AIRFLOW_FERNET_KEY."
  fi

  local existing_fernet_key="" compared_against="dataspoke-airflow-metadata-encryption-key"
  if kubectl get secret dataspoke-airflow-metadata-encryption-key -n "${ns}" >/dev/null 2>&1; then
    existing_fernet_key="$(kubectl get secret dataspoke-airflow-metadata-encryption-key -n "${ns}" \
      -o jsonpath='{.data.fernet-key}' | base64 --decode)"
  fi
  if [[ -z "${existing_fernet_key}" ]] && kubectl get secret dataspoke-airflow-fernet-key -n "${ns}" >/dev/null 2>&1; then
    existing_fernet_key="$(kubectl get secret dataspoke-airflow-fernet-key -n "${ns}" \
      -o jsonpath='{.data.fernet-key}' | base64 --decode)"
    compared_against="dataspoke-airflow-fernet-key"
  fi

  if [[ -n "${existing_fernet_key}" && "${existing_fernet_key}" != "${fernet_key}" ]]; then
    error "DATASPOKE_AIRFLOW_FERNET_KEY in Secret '${secret_name}' disagrees with the key already
projected into '${compared_against}' on this cluster. Re-projecting it
would leave Airflow's stored connections and Variables in the metadata DB permanently
undecryptable, so this aborts instead. To recover, restore the source key to match the live
projection:
  kubectl get secret ${compared_against} -n ${ns} \\
    -o jsonpath='{.data.fernet-key}' | base64 --decode
then set DATASPOKE_AIRFLOW_FERNET_KEY in '${secret_name}' to that value. Alternatively, drop the
Postgres PVC together with the credentials Secret for a clean reset (dev: --delete-pvcs; prod:
--delete-namespaces) — a freshly generated Fernet key is correct only once the PVC it would have
disagreed with is also gone."
  fi

  # Gate the skip branch on the *value* actually live in the new-name
  # projection matching the contract key — not on the Secret merely
  # existing. existing_fernet_key is read from dataspoke-airflow-metadata-
  # encryption-key only when compared_against still holds its default (the
  # emptiness-gated fallback above overwrites it to the legacy Secret's name
  # whenever the new one is absent OR present-but-empty); a missing Secret, a
  # present Secret with an empty/absent fernet-key key, and a legacy-only
  # projection all fail this check and fall through to the idempotent kubectl
  # apply below.
  if [[ -n "${existing_fernet_key}" \
        && "${existing_fernet_key}" == "${fernet_key}" \
        && "${compared_against}" == "dataspoke-airflow-metadata-encryption-key" ]]; then
    info "  dataspoke-airflow-metadata-encryption-key already up to date — skipping."
  else
    info "Creating dataspoke-airflow-metadata-encryption-key..."
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-metadata-encryption-key
  namespace: ${ns}
type: Opaque
data:
  fernet-key: $(printf '%s' "${fernet_key}" | base64 | tr -d '\n')
EOF
  fi
}

# _rollout_restart_workload <namespace> <name>
# Restarts a workload without hardcoding its kind: the Airflow chart renders
# scheduler and triggerer as either a Deployment or a StatefulSet depending on
# log/triggerer persistence.
_rollout_restart_workload() {
  local ns="$1"
  local name="$2"

  local kind
  for kind in deployment statefulset; do
    if kubectl get "${kind}/${name}" -n "${ns}" >/dev/null 2>&1; then
      kubectl rollout restart "${kind}/${name}" -n "${ns}"
      return 0
    fi
  done

  info "  ${name} not found in ${ns} — skipping restart."
}

# _restart_airflow_key_consumers <namespace>
# Rolls every Airflow component that holds a signing key in memory. A Secret
# content update does not roll pods that reference it via secretKeyRef, and the
# chart's own checksum/jwt-secret annotation is suppressed when jwtSecretName is
# set (which install.sh always sets), so the restart must be explicit.
# AIRFLOW__API__SECRET_KEY reaches all four components; the JWT secret reaches
# api-server and scheduler only.
_restart_airflow_key_consumers() {
  local ns="$1"

  info "Restarting Airflow components to pick up the rotated signing keys..."
  local component
  for component in api-server scheduler dag-processor triggerer; do
    _rollout_restart_workload "${ns}" "dataspoke-airflow-${component}"
  done
}

# _sync_env_from_secret <namespace> <secret_key> <env_var_name> [<secret_name>]
# Extracts <secret_key> from the consolidated Secret and writes/updates
# <env_var_name>=<value> in helm-charts/.env.<profile>. Idempotent.
# <secret_name> defaults to "dataspoke-secrets".
_sync_env_from_secret() {
  local ns="$1"
  local secret_key="$2"
  local env_var_name="$3"
  local secret_name="${4:-dataspoke-secrets}"

  local value
  value="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath="{.data.${secret_key}}" | base64 --decode)"

  local prefix="${env_var_name}="
  local tmp_file
  tmp_file="$(mktemp)"

  if grep -q "^${env_var_name}=" "$ENV_FILE" 2>/dev/null; then
    awk -v prefix="${prefix}" -v val="${value}" \
      'index($0, prefix)==1 {print prefix val; next} {print}' \
      "$ENV_FILE" > "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  else
    cp "$ENV_FILE" "$tmp_file"
    printf '%s=%s\n' "${env_var_name}" "${value}" >> "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

# _write_env_var <env_var_name> <value>
# Writes/updates a plain (non-Secret) value in helm-charts/.env.<profile>. Idempotent.
_write_env_var() {
  local env_var_name="$1"
  local value="$2"

  local prefix="${env_var_name}="
  local tmp_file
  tmp_file="$(mktemp)"

  if grep -q "^${env_var_name}=" "$ENV_FILE" 2>/dev/null; then
    awk -v prefix="${prefix}" -v val="${value}" \
      'index($0, prefix)==1 {print prefix val; next} {print}' \
      "$ENV_FILE" > "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  else
    cp "$ENV_FILE" "$tmp_file"
    printf '%s=%s\n' "${env_var_name}" "${value}" >> "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

# _check_airflow_credentials_prod <namespace> <secret_name>
# Validates ALL 13 required keys are present, non-empty, and not equal to known
# insecure defaults. Prod profile only.
_check_airflow_credentials_prod() {
  local ns="$1"
  local secret_name="$2"

  local required_keys=(
    DATASPOKE_POSTGRES_USER DATASPOKE_POSTGRES_PASSWORD DATASPOKE_POSTGRES_DB
    DATASPOKE_REDIS_PASSWORD
    DATASPOKE_AIRFLOW_USER DATASPOKE_AIRFLOW_PASSWORD
    DATASPOKE_INTERNAL_TOKEN DATASPOKE_JWT_SECRET_KEY
    DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY DATASPOKE_AIRFLOW_JWT_SECRET
    DATASPOKE_AIRFLOW_FERNET_KEY
    DATASPOKE_OAUTH_STATE_SECRET DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET
  )

  for key in "${required_keys[@]}"; do
    local val
    val="$(kubectl get secret "${secret_name}" -n "${ns}" \
      -o jsonpath="{.data.${key}}" 2>/dev/null | base64 --decode 2>/dev/null || true)"
    if [[ -z "${val}" ]]; then
      if [[ "${key}" == "DATASPOKE_AIRFLOW_FERNET_KEY" ]]; then
        error "prod Secret '${secret_name}' is missing required key: DATASPOKE_AIRFLOW_FERNET_KEY.
If this namespace already ran Airflow against a Postgres metadata DB you are keeping (for
example a PVC retained from a previous release), do NOT generate a new key — supply the exact
key that DB's connections and Variables were encrypted with, or they become permanently
undecryptable. Recover it from whatever this cluster last projected it into:
  kubectl get secret dataspoke-airflow-metadata-encryption-key -n ${ns} \\
    -o jsonpath='{.data.fernet-key}' | base64 --decode
  # or, on a cluster that predates DATASPOKE_AIRFLOW_FERNET_KEY joining the credentials Secret:
  kubectl get secret dataspoke-airflow-fernet-key -n ${ns} \\
    -o jsonpath='{.data.fernet-key}' | base64 --decode
Add DATASPOKE_AIRFLOW_FERNET_KEY=<that value> to '${secret_name}' and re-run the install.
Only if this is a genuinely fresh install (no retained Postgres PVC to decrypt), generate one:
  python3 -c \"import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
      else
        error "prod Secret '${secret_name}' is missing required key: ${key}"
      fi
    fi
  done

  # Shape check for the Fernet key: `openssl rand -hex 32` — the shape every
  # other high-entropy key above uses, and the one the README's own
  # generation block sits directly next to — decodes to 48 raw bytes, not the
  # 32 Fernet requires, so it passes pod startup and fails only the first
  # time Airflow tries to encrypt or decrypt a connection or Variable, long
  # after install reports success. Catch the shape mismatch here instead.
  local fernet_val
  fernet_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' | base64 --decode)"
  if [[ ! "${fernet_val}" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    error "DATASPOKE_AIRFLOW_FERNET_KEY in Secret '${secret_name}' is not shaped like a Fernet key
(must be URL-safe base64 of exactly 32 raw bytes: 43 base64 characters followed by '='). Generate
one with:
  python3 -c \"import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"
— but only for a genuinely fresh install; see the missing-key error above if a Postgres PVC with
existing Airflow connections/Variables survives this namespace."
  fi

  local jwt_val
  jwt_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_JWT_SECRET_KEY}' | base64 --decode)"
  if [[ "${jwt_val}" == "changeme-dev-secret-do-not-use-in-prod" ]]; then
    error "DATASPOKE_JWT_SECRET_KEY is the dev default — operator must set a unique secret."
  fi

  local airflow_user
  airflow_user="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_USER}' | base64 --decode)"
  if [[ "${airflow_user}" == "admin" ]]; then
    error "DATASPOKE_AIRFLOW_USER must not be 'admin' — rename to reduce brute-force exposure."
  fi

  local airflow_password
  airflow_password="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_PASSWORD}' | base64 --decode)"
  if [[ -z "${airflow_password}" || "${airflow_password}" == "admin" ]]; then
    error "DATASPOKE_AIRFLOW_PASSWORD in Secret '${secret_name}' must not be empty or 'admin'."
  fi

  local google_oauth_secret_val
  google_oauth_secret_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET}' | base64 --decode)"
  if [[ "${google_oauth_secret_val}" == placeholder-* ]]; then
    error "DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET is the dev placeholder — operator must set a real Google OAuth client secret."
  fi
}

# _build_airflow_extra_env_file <secret_name>
# Writes the Airflow extraEnv YAML block (with the resolved secret name) to a
# temp file and prints its path. Caller is responsible for cleanup.
_build_airflow_extra_env_file() {
  local secret_name="$1"
  local tmp_env_file
  tmp_env_file="$(mktemp "${INSTALL_TMPDIR}/airflow-extra-env.XXXX.yaml")"
  cat > "${tmp_env_file}" <<EOF
- name: AIRFLOW_CONN_DATASPOKE_API
  value: "http://dataspoke-api:8002"
- name: DATASPOKE_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: ${secret_name}
      key: DATASPOKE_INTERNAL_TOKEN
EOF
  printf '%s' "${tmp_env_file}"
}

# _resolve_existing_secret_name [<overlay_file>]
# Extracts secrets.existingSecret from an operator overlay using python3+yaml.
# Prints the resolved name, or empty string if absent/unset.
_resolve_existing_secret_name() {
  local overlay_file="${1:-}"
  if [[ -z "${overlay_file}" || ! -f "${overlay_file}" ]]; then
    echo ""
    return 0
  fi
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to parse the operator overlay for secrets.existingSecret. Install: pip install pyyaml"
  fi
  python3 - "${overlay_file}" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
print((data.get("secrets") or {}).get("existingSecret", ""))
PYEOF
}

# _api_image_helm_set_args
# Prints the --set flags pinning every workload that runs the API image: the API
# itself, and the event-consumer, which runs the same image under a `command:`
# override and builds nothing of its own. They are emitted from one place so a
# new call site cannot pin one and forget the other — a consumer left on the
# chart default resolves to `dataspoke/api:latest`, which no build produces.
# Output is one token per line; callers read into an array via a while-read loop.
_api_image_helm_set_args() {
  cat <<EOF
--set
api.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api
--set
api.image.tag=${IMAGE_TAG}
--set
event-consumer.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api
--set
event-consumer.image.tag=${IMAGE_TAG}
EOF
}

# _frontend_helm_set_args <domain>
# Prints the --set flags required to enable and wire the frontend subchart.
# Output is one token per line; callers read into an array via a while-read loop.
# The nginx annotation key contains a dot that helm interprets as a path
# separator, so it must be escaped as \\.
_frontend_helm_set_args() {
  local domain="$1"
  local scheme
  scheme="$(ingress_scheme)"
  # Assigned, not inlined into the heredoc: a validation failure inside a
  # command substitution that only produces heredoc text does not trip `set -e`,
  # so an invalid class would be emitted as an empty token instead of aborting.
  local ingress_cls
  ingress_cls="$(ingress_class)"
  cat <<EOF
--set
frontend.enabled=true
--set
frontend.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend
--set
frontend.image.tag=${IMAGE_TAG}
--set
frontend.image.pullPolicy=Always
--set
frontend.ingress.enabled=true
--set
frontend.ingress.className=${ingress_cls}
--set-string
frontend.ingress.annotations.nginx\.ingress\.kubernetes\.io/ssl-redirect=false
--set
frontend.ingress.hosts[0].host=app.${domain}
--set
frontend.ingress.hosts[0].paths[0].path=/
--set
frontend.ingress.hosts[0].paths[0].pathType=Prefix
--set
frontend.config.apiBaseUrl=${scheme}://api.${domain}
--set
frontend.config.airflowUrl=${scheme}://airflow.${domain}
EOF
  local tls_secret
  tls_secret="$(ingress_tls_secret)"
  if [[ -n "$tls_secret" ]]; then
    cat <<EOF
--set
frontend.ingress.tls[0].secretName=${tls_secret}
--set
frontend.ingress.tls[0].hosts[0]=app.${domain}
EOF
  fi
}

# _write_frontend_env_local <domain>
# Overwrites src/frontend/.env.local with NEXT_PUBLIC_* vars pointing at the
# in-cluster API and Airflow. Always overwrites — no backup.
_write_frontend_env_local() {
  local domain="$1"
  local scheme
  scheme="$(ingress_scheme)"
  local env_local_path="${REPO_ROOT}/src/frontend/.env.local"
  cat > "${env_local_path}" <<EOF
# Auto-generated by helm-charts/bin/install.sh --frontend local — safe to edit or delete.
NEXT_PUBLIC_API_BASE_URL=${scheme}://api.${domain}
NEXT_PUBLIC_AIRFLOW_URL=${scheme}://airflow.${domain}
EOF
  info "Wrote ${env_local_path} (API: ${scheme}://api.${domain}, Airflow: ${scheme}://airflow.${domain})"
}

# _api_airflow_tls_helm_set_args <domain>
# Prints the --set flags for per-Ingress TLS on the API and Airflow (chart-
# native) ingresses when DATASPOKE_KUBE_INGRESS_TLS_SECRET is set. Empty
# output when unset. Output is one token per line, same convention as
# _frontend_helm_set_args — callers read into an array via a while-read loop.
_api_airflow_tls_helm_set_args() {
  local domain="$1"
  local tls_secret
  tls_secret="$(ingress_tls_secret)"
  [[ -z "$tls_secret" ]] && return 0
  cat <<EOF
--set
api.ingress.tls[0].secretName=${tls_secret}
--set
api.ingress.tls[0].hosts[0]=api.${domain}
--set
airflow.ingress.apiServer.hosts[0].tls.enabled=true
--set
airflow.ingress.apiServer.hosts[0].tls.secretName=${tls_secret}
EOF
}

# helm upgrade --install for the dataspoke umbrella chart (dev overlay).
# Used by both the full dev install (phase 3) and the --components api fast path.
# Reads the global $FRONTEND_MODE to decide whether to append frontend --set flags.
_helm_upgrade_dataspoke_dev() {
  local ns="$1"

  # This upgrade pins airflow.{apiSecretKeySecretName,jwtSecretName,
  # fernetKeySecretName} below, so every call site of this function — the
  # full install's phase 3 and the `--components api` fast path — must
  # guarantee those projected Secrets (and the credentials Secret they derive
  # from) exist first. The `--components frontend` fast path renders its own
  # separate helm upgrade with the same pins and runs the identical sequence
  # inline rather than through this function. Idempotent: a no-op once
  # everything is already in sync.
  _ensure_dataspoke_secrets "${ns}" "dev" "dataspoke-secrets"
  _derive_airflow_metadata_secret "${ns}" "dataspoke-secrets"
  _ensure_airflow_key_secrets "${ns}" "dataspoke-secrets"
  _ensure_airflow_fernet_secret "${ns}" "dataspoke-secrets"

  local extra_env_file
  extra_env_file="$(_build_airflow_extra_env_file "dataspoke-secrets")"
  local dev_domain="${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
  local scheme
  scheme="$(ingress_scheme)"
  # OIDC post-login redirect = where the UI is served for this frontend mode
  # (host pnpm dev on localhost vs in-cluster app.<domain>).
  local oauth_redirect="${scheme}://app.${dev_domain}/"
  [[ "$FRONTEND_MODE" == "local" ]] && oauth_redirect="http://localhost:3000/"
  # One class for every Ingress this release renders. Note the key names differ:
  # the API and frontend templates read `ingress.className`, the Airflow chart
  # reads `ingress.apiServer.ingressClassName` — the wrong spelling is silently
  # dropped and GKE falls back to provisioning a GCE LoadBalancer.
  local ingress_cls
  ingress_cls="$(ingress_class)"

  local args=(
    upgrade --install dataspoke "$CHART_DIR"
    -f "$CHART_DIR/values-dev.yaml"
    -n "${ns}"
    --set-string global.imageRegistry=""
    --set-string postgresql.image.registry=""
    --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres"
    --set-string "postgresql.image.tag=${IMAGE_TAG}"
    --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow"
    --set-string "airflow.images.airflow.tag=${IMAGE_TAG}"
    --set airflow.images.airflow.pullPolicy=Always
    --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002"
    --set "api.ingress.className=${ingress_cls}"
    --set "api.ingress.hosts[0].host=api.${dev_domain}"
    --set "api.ingress.hosts[0].paths[0].path=/"
    --set "api.ingress.hosts[0].paths[0].pathType=Prefix"
    --set-string "config.corsOrigins=http://localhost:3000\,http://app.${dev_domain}\,https://app.${dev_domain}"
    --set-string "config.oauthPostLoginRedirect=${oauth_redirect}"
    --set "airflow.ingress.apiServer.ingressClassName=${ingress_cls}"
    --set "airflow.ingress.apiServer.hosts[0].name=airflow.${dev_domain}"
    --set-file "airflow.extraEnv=${extra_env_file}"
    --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key"
    --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret"
    --set "airflow.fernetKeySecretName=dataspoke-airflow-metadata-encryption-key"
    --set-string "auth.googleClientId=${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID:-}"
    --timeout 10m
  )

  while IFS= read -r _iarg; do
    args+=("${_iarg}")
  done < <(_api_image_helm_set_args)

  if [[ "${FRONTEND_MODE:-none}" == "cluster" ]]; then
    while IFS= read -r _farg; do
      args+=("${_farg}")
    done < <(_frontend_helm_set_args "${dev_domain}")
  fi

  while IFS= read -r _tlsarg; do
    args+=("${_tlsarg}")
  done < <(_api_airflow_tls_helm_set_args "${dev_domain}")

  helm "${args[@]}"
}

# ---------------------------------------------------------------------------
# DEV PROFILE
# ---------------------------------------------------------------------------
if [[ "$PROFILE" == "dev" ]]; then

  # Default component set for dev
  DEV_ALL=(nginx-ingress datahub langfuse dataspoke-infra dummy-data dev-lock seed)

  # Parse user-supplied --components into an array
  if [[ -n "$COMPONENTS_CSV" ]]; then
    IFS=',' read -ra SELECTED <<< "$COMPONENTS_CSV"
    # Trim whitespace
    COMPONENTS=()
    for c in "${SELECTED[@]}"; do
      COMPONENTS+=("$(echo "$c" | tr -d ' ')")
    done
  else
    COMPONENTS=("${DEV_ALL[@]}")
  fi

  # Resolve start index for --from-component
  START_INDEX=0
  if [[ -n "$FROM_COMPONENT" && -z "$COMPONENTS_CSV" ]]; then
    found=false
    for i in "${!DEV_ALL[@]}"; do
      if [[ "${DEV_ALL[$i]}" == "$FROM_COMPONENT" ]]; then
        START_INDEX=$i
        found=true
        break
      fi
    done
    if [[ "$found" != "true" ]]; then
      error "Unknown component '${FROM_COMPONENT}'. Valid names: ${DEV_ALL[*]}"
    fi
    COMPONENTS=("${DEV_ALL[@]:$START_INDEX}")
    info "Resuming from component '${FROM_COMPONENT}'."
  fi

  # Helpers
  _has_component() { local needle="$1"; for c in "${COMPONENTS[@]}"; do [[ "$c" == "$needle" ]] && return 0; done; return 1; }

  # -------------------------------------------------------------------------
  # Handle --components api (code-iteration fast path)
  # -------------------------------------------------------------------------
  if [[ "${#COMPONENTS[@]}" -eq 1 && "${COMPONENTS[0]}" == "api" ]]; then
    NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
    SCHEME="$(ingress_scheme)"
    info "==> Fast path: rebuild API image + helm upgrade + rollout restart"

    if [[ "$SKIP_BUILD" == "false" ]]; then
      info "Building API image (tag: ${IMAGE_TAG})..."
      bash "$SCRIPT_DIR/build-image.sh" api "${IMAGE_TAG}"
    else
      info "--skip-build: skipping API image build."
    fi

    info "Running helm upgrade for dataspoke umbrella chart..."
    use_context "${DATASPOKE_KUBE_CLUSTER}"

    # Re-package local subcharts so event-consumer template edits ship instead
    # of the stale packaged subchart in charts/. This upgrade is a full-release
    # one, so it deploys the event-consumer too — and the consumer runs this
    # same API image.
    _build_chart_deps "$CHART_DIR"

    _helm_upgrade_dataspoke_dev "${NS}"

    # Roll the Airflow pods still holding a superseded signing key, if
    # _helm_upgrade_dataspoke_dev found the credentials Secret's Airflow keys
    # had drifted from their live projections.
    if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" ]]; then
      _restart_airflow_key_consumers "${NS}"
    fi

    info "Restarting dataspoke-api deployment to pick up new image..."
    kubectl rollout restart deployment/dataspoke-api -n "${NS}"
    kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
      && info "dataspoke-api is ready." \
      || error "dataspoke-api did not become ready in time — check pod logs."

    # The event-consumer runs the same image, so a rebuilt API image leaves it
    # holding stale code. The tag is unchanged, so helm alone rolls nothing.
    info "Restarting dataspoke-event-consumer (shares the API image)..."
    _rollout_restart_workload "${NS}" "dataspoke-event-consumer"

    # Verify Airflow DAGs
    DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"
    if [[ -n "$DOMAIN" ]]; then
      info "Verifying Airflow DAGs..."
      INTERNAL_TOKEN="$(kubectl exec -n "${NS}" deploy/dataspoke-api -c api -- \
        printenv DATASPOKE_INTERNAL_TOKEN 2>/dev/null || true)"
      if [[ -z "$INTERNAL_TOKEN" ]]; then
        warn "Could not read DATASPOKE_INTERNAL_TOKEN — skipping DAG verification."
      elif curl -sf -X POST "${SCHEME}://api.${DOMAIN}/internal/admin/dags/verify" \
            -H "X-Internal-Token: ${INTERNAL_TOKEN}" -o /dev/null; then
        info "Airflow DAGs verified."
      else
        warn "Failed to verify Airflow DAGs — retry after Airflow is ready."
      fi
    fi

    echo ""
    info "API iteration deploy complete (t+$((SECONDS - START_TIME))s)."
    echo ""
    if [[ -n "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
      echo "  API:   ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/api"
      echo "  ReDoc: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/redoc"
    fi
    echo ""
    echo "  To run integration tests:"
    echo "    set -a && source ${ENV_FILE} && set +a && uv run pytest tests/integration/api_wired/ -v"
    echo ""
    echo "  To stop the API:"
    echo "    kubectl scale deployment/dataspoke-api --replicas=0 -n '${NS}'"
    echo ""
    exit 0
  fi

  # -------------------------------------------------------------------------
  # Handle --components frontend (containerised frontend fast path)
  # The default dev install keeps frontend.enabled=false so host `pnpm dev`
  # remains the standard dev workflow. This path explicitly enables and deploys
  # the containerised frontend in-cluster for verification or prod-parity testing.
  # -------------------------------------------------------------------------
  if [[ "${#COMPONENTS[@]}" -eq 1 && "${COMPONENTS[0]}" == "frontend" ]]; then
    NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
    DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
    info "==> Fast path: rebuild frontend image + helm upgrade + rollout"
    info "    Note: deploys the containerised frontend in-cluster (overrides frontend.enabled=false)."

    if [[ "$SKIP_BUILD" == "false" ]]; then
      info "Building frontend image (tag: ${IMAGE_TAG})..."
      bash "$SCRIPT_DIR/build-image.sh" frontend "${IMAGE_TAG}"
    else
      info "--skip-build: skipping frontend image build."
    fi

    info "Running helm upgrade for dataspoke umbrella chart (frontend.enabled=true)..."
    use_context "${DATASPOKE_KUBE_CLUSTER}"

    # Re-package local subcharts so frontend template/config edits ship instead
    # of the stale packaged subchart in charts/.
    _build_chart_deps "$CHART_DIR"

    # This is a full-release helm upgrade (below) that pins
    # airflow.{apiSecretKeySecretName,jwtSecretName,fernetKeySecretName}, so it
    # must guarantee those projected Secrets — and the credentials Secret they
    # derive from — exist first. Idempotent: a no-op once already in sync.
    _ensure_dataspoke_secrets "${NS}" "dev" "dataspoke-secrets"
    _derive_airflow_metadata_secret "${NS}" "dataspoke-secrets"
    _ensure_airflow_key_secrets "${NS}" "dataspoke-secrets"
    _ensure_airflow_fernet_secret "${NS}" "dataspoke-secrets"

    SCHEME="$(ingress_scheme)"
    # One class for every Ingress this release renders. The API and frontend
    # templates read `ingress.className`, the Airflow chart reads
    # `ingress.apiServer.ingressClassName` — the wrong spelling is silently
    # dropped and GKE falls back to provisioning a GCE LoadBalancer.
    INGRESS_CLS="$(ingress_class)"
    local_extra_env_file="$(_build_airflow_extra_env_file "dataspoke-secrets")"
    frontend_fast_args=()
    while IFS= read -r _farg; do
      frontend_fast_args+=("${_farg}")
    done < <(_frontend_helm_set_args "${DOMAIN}")
    tls_fast_args=()
    while IFS= read -r _tlsarg; do
      tls_fast_args+=("${_tlsarg}")
    done < <(_api_airflow_tls_helm_set_args "${DOMAIN}")
    api_image_fast_args=()
    while IFS= read -r _iarg; do
      api_image_fast_args+=("${_iarg}")
    done < <(_api_image_helm_set_args)
    helm upgrade --install dataspoke "$CHART_DIR" \
      -f "$CHART_DIR/values-dev.yaml" \
      -n "${NS}" \
      --set-string global.imageRegistry="" \
      --set-string postgresql.image.registry="" \
      --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
      --set-string postgresql.image.tag="${IMAGE_TAG}" \
      "${api_image_fast_args[@]}" \
      --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
      --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
      --set airflow.images.airflow.pullPolicy=Always \
      --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002" \
      --set "api.ingress.className=${INGRESS_CLS}" \
      --set "api.ingress.hosts[0].host=api.${DOMAIN}" \
      --set "api.ingress.hosts[0].paths[0].path=/" \
      --set "api.ingress.hosts[0].paths[0].pathType=Prefix" \
      --set-string "config.corsOrigins=http://localhost:3000\,http://app.${DOMAIN}\,https://app.${DOMAIN}" \
      --set-string "config.oauthPostLoginRedirect=${SCHEME}://app.${DOMAIN}/" \
      --set "airflow.ingress.apiServer.ingressClassName=${INGRESS_CLS}" \
      --set "airflow.ingress.apiServer.hosts[0].name=airflow.${DOMAIN}" \
      --set-file "airflow.extraEnv=${local_extra_env_file}" \
      --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key" \
      --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret" \
      --set "airflow.fernetKeySecretName=dataspoke-airflow-metadata-encryption-key" \
      --set-string "auth.googleClientId=${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID:-}" \
      "${frontend_fast_args[@]}" \
      ${tls_fast_args[@]+"${tls_fast_args[@]}"} \
      --timeout 10m

    info "Waiting for frontend deployment to become ready..."
    kubectl rollout status deployment/dataspoke-frontend -n "${NS}" --timeout=5m \
      && info "dataspoke-frontend is ready." \
      || error "dataspoke-frontend did not become ready in time — check pod logs."

    # Roll the Airflow pods still holding a superseded signing key, if the
    # ensure-secrets step above found the credentials Secret's Airflow keys
    # had drifted from their live projections.
    if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" ]]; then
      _restart_airflow_key_consumers "${NS}"
    fi

    echo ""
    info "Frontend deploy complete (t+$((SECONDS - START_TIME))s)."
    echo ""
    echo "  Frontend: ${SCHEME}://app.${DOMAIN}/"
    echo "  API:      ${SCHEME}://api.${DOMAIN}/api/v1/"
    echo ""
    echo "  To stop the frontend pod:"
    echo "    kubectl scale deployment/dataspoke-frontend --replicas=0 -n '${NS}'"
    echo ""
    exit 0
  fi

  # -------------------------------------------------------------------------
  # Full dev install — phased
  # -------------------------------------------------------------------------

  # Ensure dev namespaces
  use_context "${DATASPOKE_KUBE_CLUSTER}"
  ensure_namespace "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
  ensure_namespace "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
  ensure_namespace "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
  ensure_namespace "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

  # -----------------------------------------------------------------------
  # Phase 1: nginx-ingress (sequential — must complete before parallel phase)
  # -----------------------------------------------------------------------
  if _has_component nginx-ingress; then
    step 1 5 "nginx-ingress"
    bash "$SCRIPT_DIR/dev-peripherals/nginx-ingress.sh"
    # Re-source .env so DATASPOKE_KUBE_INGRESS_IP/_DOMAIN are available
    source "$ENV_FILE"
  fi

  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  # -----------------------------------------------------------------------
  # Phase 2: Parallel bootstrap
  # Build images ‖ install DataHub ‖ install Langfuse
  # -----------------------------------------------------------------------
  step 2 5 "parallel bootstrap (image builds + DataHub + Langfuse)"

  if [[ "$SKIP_BUILD" == "false" ]]; then
    _run_bg "build-api"      bash "$SCRIPT_DIR/build-image.sh" api      "${IMAGE_TAG}"
    _run_bg "build-airflow"  bash "$SCRIPT_DIR/build-image.sh" airflow  "${IMAGE_TAG}"
    _run_bg "build-postgres" bash "$SCRIPT_DIR/build-image.sh" postgres "${IMAGE_TAG}"
    if [[ "$FRONTEND_MODE" == "cluster" ]]; then
      _run_bg "build-frontend" bash "$SCRIPT_DIR/build-image.sh" frontend "${IMAGE_TAG}"
    fi
  else
    info "  --skip-build: skipping image builds."
  fi

  if _has_component datahub; then
    _run_bg "datahub" bash "$SCRIPT_DIR/dev-peripherals/datahub.sh"
  fi
  if _has_component langfuse; then
    _run_bg "langfuse" bash "$SCRIPT_DIR/dev-peripherals/langfuse.sh"
  fi

  _wait_all
  # Re-source .env to pick up any new values written by parallel tasks
  source "$ENV_FILE"

  # -----------------------------------------------------------------------
  # Phase 3: Umbrella chart (dataspoke-infra)
  # -----------------------------------------------------------------------
  if _has_component dataspoke-infra; then
    step 3 5 "dataspoke-infra (umbrella chart)"

    # The consolidated credential Secret and its Airflow projections
    # (metadata-db connection, webserver/jwt keys, Fernet key) are ensured
    # inside _helm_upgrade_dataspoke_dev below, ahead of the helm upgrade that
    # pins their Secret names — see that function's header comment.

    # LLM API key (out-of-band secret)
    if [[ -n "${DATASPOKE_DEV_LLM_API_KEY:-}" ]]; then
      info "Applying dataspoke-llm-secret (LLM API key)..."
      kubectl create secret generic dataspoke-llm-secret \
        --namespace "${NS}" \
        --from-literal=api_key="${DATASPOKE_DEV_LLM_API_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_DEV_LLM_API_KEY is unset — dataspoke-llm-secret not created."
    fi

    # DataHub token (out-of-band secret)
    if [[ -n "${DATASPOKE_TEST_DATAHUB_TOKEN:-}" ]]; then
      info "Applying dataspoke-datahub-secret (DataHub PAT)..."
      kubectl create secret generic dataspoke-datahub-secret \
        --namespace "${NS}" \
        --from-literal=token="${DATASPOKE_TEST_DATAHUB_TOKEN}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_TEST_DATAHUB_TOKEN is unset — dataspoke-datahub-secret not created."
    fi

    # Langfuse secret key (out-of-band secret)
    if [[ -n "${DATASPOKE_TEST_LANGFUSE_SECRET_KEY:-}" ]]; then
      info "Applying dataspoke-langfuse-secret (Langfuse secret key)..."
      kubectl create secret generic dataspoke-langfuse-secret \
        --namespace "${NS}" \
        --from-literal=secret_key="${DATASPOKE_TEST_LANGFUSE_SECRET_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_TEST_LANGFUSE_SECRET_KEY is unset — dataspoke-langfuse-secret not created."
    fi

    # Source-credential Secret: dummy-data Postgres (dev only)
    # Allows ACTIVE_CUSTOM_MANAGED ingestion recipes that reference ${dummy-data-pg__password}
    # to resolve at run time. Must live in the DataSpoke API namespace so secret_resolver.py
    # (which reads /var/run/secrets/kubernetes.io/serviceaccount/namespace) can reach it.
    info "Applying dataspoke-source-cred-dummy-data-pg (dev source credential)..."
    kubectl create secret generic dataspoke-source-cred-dummy-data-pg \
      --namespace "${NS}" \
      --from-literal=password="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-ExampleDev2024!}" \
      --dry-run=client -o yaml | kubectl apply -f -

    # Helm repo setup
    info "Adding/updating Helm repositories..."
    helm_repo_add_if_missing bitnami        "https://charts.bitnami.com/bitnami"
    helm_repo_add_if_missing apache-airflow "https://airflow.apache.org"
    helm repo update

    # Build chart dependencies
    info "Building Helm chart dependencies..."
    _build_chart_deps "$CHART_DIR"

    # Helm upgrade --install
    info "Installing DataSpoke umbrella chart..."
    _helm_upgrade_dataspoke_dev "${NS}"

    # Roll the Airflow pods still holding a superseded signing key
    if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" ]]; then
      _restart_airflow_key_consumers "${NS}"
    fi

    # Ensure pgvector + AGE extensions
    info "Ensuring pgvector + age extensions in the dataspoke database..."

    # Read postgres credentials from the consolidated Secret (never from .env)
    DS_POSTGRES_USER="$(kubectl get secret dataspoke-secrets -n "${NS}" \
      -o jsonpath='{.data.DATASPOKE_POSTGRES_USER}' | base64 --decode)"
    DS_POSTGRES_PASSWORD="$(kubectl get secret dataspoke-secrets -n "${NS}" \
      -o jsonpath='{.data.DATASPOKE_POSTGRES_PASSWORD}' | base64 --decode)"
    DS_POSTGRES_DB="$(kubectl get secret dataspoke-secrets -n "${NS}" \
      -o jsonpath='{.data.DATASPOKE_POSTGRES_DB}' | base64 --decode)"

    # Validate the postgres username before interpolating it into SQL.
    if [[ ! "${DS_POSTGRES_USER}" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
      error "DATASPOKE_POSTGRES_USER '${DS_POSTGRES_USER}' is not a valid SQL identifier."
    fi

    kubectl rollout status statefulset/dataspoke-postgresql -n "${NS}" --timeout=5m >/dev/null 2>&1 || true
    kubectl exec -n "${NS}" dataspoke-postgresql-0 -- \
      env PGPASSWORD="${DS_POSTGRES_PASSWORD}" \
      psql -U postgres -d "${DS_POSTGRES_DB}" -c "
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS age;
        GRANT USAGE ON SCHEMA ag_catalog TO ${DS_POSTGRES_USER};
        GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO ${DS_POSTGRES_USER};
      " >/dev/null 2>&1 \
      && info "  Extensions ready (vector + age)." \
      || warn "  Could not create extensions — run manually via kubectl exec."

    # Wait for Airflow api-server
    info "Waiting for Airflow api-server to become ready..."
    kubectl rollout status deployment/dataspoke-airflow-api-server -n "${NS}" --timeout=5m \
      && info "Airflow api-server is ready." \
      || error "Airflow api-server did not become ready in time — check pod logs."

    # Wait for DataSpoke API
    info "Waiting for DataSpoke API to become ready..."
    kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
      && info "DataSpoke API is ready." \
      || warn "DataSpoke API did not become ready in time."

    # Wait for the event-consumer. Warn rather than error: the consumer is not
    # on the path of any UC1–UC5 flow, so a failure here should not abort an
    # otherwise-good install — but it must be visible, since nothing else in
    # the dev loop looks at this pod.
    info "Waiting for DataSpoke event-consumer to become ready..."
    kubectl rollout status deployment/dataspoke-event-consumer -n "${NS}" --timeout=5m \
      && info "DataSpoke event-consumer is ready." \
      || warn "DataSpoke event-consumer did not become ready in time — check pod logs."

    # Wait for frontend (only when deployed in-cluster)
    if [[ "$FRONTEND_MODE" == "cluster" ]]; then
      info "Waiting for DataSpoke frontend to become ready..."
      kubectl rollout status deployment/dataspoke-frontend -n "${NS}" --timeout=5m \
        && info "DataSpoke frontend is ready." \
        || warn "DataSpoke frontend did not become ready in time — check pod logs."
    fi

    # Populate DATASPOKE_TEST_* block in .env for laptop-side test access
    info "Writing DATASPOKE_TEST_* values to .env..."
    _sync_env_from_secret "${NS}" "DATASPOKE_POSTGRES_USER"     "DATASPOKE_TEST_POSTGRES_USER"
    _sync_env_from_secret "${NS}" "DATASPOKE_POSTGRES_PASSWORD" "DATASPOKE_TEST_POSTGRES_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_POSTGRES_DB"       "DATASPOKE_TEST_POSTGRES_DB"
    _sync_env_from_secret "${NS}" "DATASPOKE_REDIS_PASSWORD"    "DATASPOKE_TEST_REDIS_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_AIRFLOW_USER"      "DATASPOKE_TEST_AIRFLOW_USER"
    _sync_env_from_secret "${NS}" "DATASPOKE_AIRFLOW_PASSWORD"  "DATASPOKE_TEST_AIRFLOW_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_INTERNAL_TOKEN"    "DATASPOKE_TEST_INTERNAL_TOKEN"
    _sync_env_from_secret "${NS}" "DATASPOKE_JWT_SECRET_KEY"    "DATASPOKE_TEST_JWT_SECRET_KEY"

    # Laptop-side host/port for direct DB/cache access. In managed mode this is
    # the ingress LoadBalancer IP; in shared mode it is 127.0.0.1, reached via
    # `kubectl port-forward` (bin/port-forward.sh) on the same canonical ports.
    TCP_HOST="$(tcp_access_host)"
    _write_env_var "DATASPOKE_TEST_POSTGRES_HOST" "${TCP_HOST}"
    _write_env_var "DATASPOKE_TEST_POSTGRES_PORT" "9201"
    _write_env_var "DATASPOKE_TEST_REDIS_HOST"    "${TCP_HOST}"
    _write_env_var "DATASPOKE_TEST_REDIS_PORT"    "9202"
    _write_env_var "DATASPOKE_TEST_AIRFLOW_URL"   "$(ingress_scheme)://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"

    # Dummy-data source access. In shared mode TCP_HOST is 127.0.0.1 (port-forward);
    # in managed mode it is the LoadBalancer IP (nginx TCP passthrough).
    # _POSTGRES_HOST_PORT is the in-cluster cluster-DNS address used by the
    # DataSpoke API pod when building ingestion source recipes — it is the same
    # in both modes because the API always runs in-cluster.
    _write_env_var "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST"      "${TCP_HOST}"
    _write_env_var "DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS"      "${TCP_HOST}:9104"
    _write_env_var "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT" \
      "example-postgres.${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}.svc.cluster.local:5432"

    # Dev-lock URL — same pattern: 127.0.0.1 in shared mode, LoadBalancer IP in managed.
    _write_env_var "DATASPOKE_TEST_LOCK_URL" "http://${TCP_HOST}:9221"

    info ".env updated with DATASPOKE_TEST_* values."
  fi

  # -----------------------------------------------------------------------
  # Phase 4: Parallel post-bootstrap
  # -----------------------------------------------------------------------
  step 4 5 "parallel post-bootstrap (dummy-data + dev-lock)"

  if _has_component dummy-data; then
    _run_bg "dummy-data" bash "$SCRIPT_DIR/dev-peripherals/dummy-data.sh"
  fi
  if _has_component dev-lock; then
    _run_bg "dev-lock" bash "$SCRIPT_DIR/dev-peripherals/dev-lock.sh"
  fi

  _wait_all

  # -----------------------------------------------------------------------
  # Phase 5: Post-install seeding
  # -----------------------------------------------------------------------
  if _has_component seed && [[ "$SKIP_SEED" == "false" ]]; then
    step 5 5 "post-install seeding"
    bash "$SCRIPT_DIR/post-install/seed-peripheral-config.sh"
    bash "$SCRIPT_DIR/post-install/seed-runtime-config.sh"
    bash "$SCRIPT_DIR/post-install/seed-admin-user.sh"
  else
    info "Skipping seeding (--skip-seed or 'seed' not in components)."
  fi

  # Write src/frontend/.env.local when local mode is requested
  if [[ "$FRONTEND_MODE" == "local" ]]; then
    _write_frontend_env_local "${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
  fi

  # -----------------------------------------------------------------------
  # Re-read .env for summary
  # -----------------------------------------------------------------------
  source "$ENV_FILE"
  SCHEME="$(ingress_scheme)"

  echo ""
  echo "=== Installation complete (profile: dev) ==="
  echo ""
  echo "Namespaces:"
  kubectl get namespaces \
    "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}" \
    "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}" \
    "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}" \
    "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}" 2>/dev/null || true
  echo ""
  if [[ "$(ingress_mode)" == "shared" ]]; then
    echo "Ingress endpoints (via shared cluster ingress; domain ${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}):"
  else
    echo "Ingress endpoints (via nginx-ingress at ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}):"
  fi
  echo ""
  echo "  DataHub UI:    ${SCHEME}://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  DataHub GMS:   ${SCHEME}://datahub-gms.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  DataSpoke API: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/api/v1/"
  echo "  Airflow UI:    ${SCHEME}://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  Langfuse UI:   ${DATASPOKE_TEST_LANGFUSE_HOST:-${SCHEME}://langfuse.<not set>}/"
  echo ""
  if [[ "$(ingress_mode)" == "shared" ]]; then
    echo "  TCP services (Postgres/Redis/Kafka/lock) are not on the shared ingress."
    echo "  Open them on 127.0.0.1 with: ./helm-charts/bin/port-forward.sh"
    echo "    PostgreSQL 127.0.0.1:9201   Redis 127.0.0.1:9202   DataHub Kafka 127.0.0.1:9005"
    echo "    Example PG 127.0.0.1:9102   Example Kafka 127.0.0.1:9104   Lock API 127.0.0.1:9221"
  else
    echo "  PostgreSQL:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9201"
    echo "  Redis:         ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9202"
    echo "  DataHub Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9005"
    echo "  Example PG:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9102"
    echo "  Example Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9104"
    echo "  Lock API:      ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9221"
  fi
  echo ""
  echo "  Credentials (auto-generated): see DATASPOKE_TEST_AIRFLOW_{USER,PASSWORD} in ${ENV_FILE}"
  echo "  Langfuse: ${DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL:-dataspoke@dataspoke.local} / ${DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD:-<see .env>}"
  echo ""
  case "$FRONTEND_MODE" in
    none)
      echo "  Frontend:      not deployed (--frontend none). Use --frontend local | cluster to deploy."
      ;;
    local)
      echo "  Frontend (host dev):"
      echo "    src/frontend/.env.local written (API: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}, DataHub: ${SCHEME}://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>})"
      echo "    Run:   pnpm -C src/frontend install && pnpm -C src/frontend dev"
      echo "    Open:  http://localhost:3000"
      if [[ "$SKIP_SEED" == "true" ]]; then
        echo "    Login: (admin not seeded — --skip-seed)"
      else
        echo "    Login: dataspoke@dataspoke.local / dataspoke  (rotate via PATCH /auth/me)"
      fi
      ;;
    cluster)
      echo "  Frontend (in-cluster):"
      echo "    Web UI: ${SCHEME}://app.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
      if [[ "$SKIP_SEED" == "true" ]]; then
        echo "    Login:  (admin not seeded — --skip-seed)"
      else
        echo "    Login:  dataspoke@dataspoke.local / dataspoke  (rotate via PATCH /auth/me)"
      fi
      ;;
  esac
  echo ""
  echo "API iteration:"
  echo "  ./helm-charts/bin/install.sh --profile dev --components api"
  echo ""
  echo "Seed dummy data:"
  echo "  uv run python -m tests.integration.util --reset-seed"
  echo ""
  echo "Health check:"
  echo "  ./helm-charts/bin/health-check.sh"
  echo ""
  info "Total elapsed: $((SECONDS - START_TIME))s ($(printf '%dm%02ds' $(( (SECONDS - START_TIME) / 60 )) $(( (SECONDS - START_TIME) % 60 ))))"
  echo ""

# ---------------------------------------------------------------------------
# PROD PROFILE
# ---------------------------------------------------------------------------
elif [[ "$PROFILE" == "prod" ]]; then

  if [[ "${IMAGE_TAG_EXPLICIT}" != true ]]; then
    error "--profile prod requires an explicit --image-tag <tag> to avoid deploying the mutable ':dev' tag onto a shared registry."
  fi

  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  # -----------------------------------------------------------------------
  # Phase 1: Pre-flight (no nginx-ingress — operator's controller)
  # -----------------------------------------------------------------------
  step 1 3 "pre-flight"
  use_context "${DATASPOKE_KUBE_CLUSTER}"
  ensure_namespace "${NS}"

  # Verify the operator's shared ingress controller is installed (fail fast).
  #
  # Required explicitly in prod — no `nginx` default. The --set below overrides
  # whatever class the operator's --values overlay pins, so defaulting would
  # silently republish the API, frontend, and Airflow UI on any IngressClass
  # that happens to be named `nginx` (often another team's internet-facing
  # controller). The existence check that follows proves the class is real, not
  # that it is the one the operator meant.
  : "${DATASPOKE_KUBE_INGRESS_CLASS:?must be set explicitly in the prod .env — install.sh --sets this class onto every DataSpoke Ingress, overriding the className in your --values overlay}"
  INGRESS_CLASS="$(ingress_class)"
  if ! kubectl get ingressclass "${INGRESS_CLASS}" >/dev/null 2>&1; then
    error "IngressClass '${INGRESS_CLASS}' not found in the cluster. Install a controller or set DATASPOKE_KUBE_INGRESS_CLASS."
  fi
  info "IngressClass '${INGRESS_CLASS}' is present."

  # Determine which Secret name is in play (default or BYO overlay)
  EXISTING_SECRET_NAME=""
  if [[ -n "${EXTRA_VALUES:-}" && -f "${EXTRA_VALUES}" ]]; then
    EXISTING_SECRET_NAME="$(_resolve_existing_secret_name "${EXTRA_VALUES}")"
  fi
  SECRET_TO_CHECK="${EXISTING_SECRET_NAME:-dataspoke-secrets}"

  # Verify operator-pre-created Secret (fail fast; never auto-generate in prod)
  _ensure_dataspoke_secrets "${NS}" "prod" "${SECRET_TO_CHECK}"

  # Validate ALL required keys are present and not insecure defaults
  _check_airflow_credentials_prod "${NS}" "${SECRET_TO_CHECK}"

  # Compare the Fernet key first and before any other Secret in this phase is
  # mutated: on a mismatch it aborts non-mutating (it only writes when there is
  # nothing yet to disagree with), so ordering it ahead of
  # _derive_airflow_metadata_secret / _ensure_airflow_key_secrets keeps the
  # "pre-flight fails before any resources are created" promise (README.md
  # §2) true even for this check.
  _ensure_airflow_fernet_secret "${NS}" "${SECRET_TO_CHECK}"

  # Derive Airflow metadata Secret from the operator Secret
  _derive_airflow_metadata_secret "${NS}" "${SECRET_TO_CHECK}"

  # Derive Airflow key secrets from the operator Secret
  _ensure_airflow_key_secrets "${NS}" "${SECRET_TO_CHECK}"

  # -----------------------------------------------------------------------
  # Phase 2: Image builds (skippable)
  # -----------------------------------------------------------------------
  if [[ "$SKIP_BUILD" == "false" ]]; then
    step 2 3 "image builds (parallel)"

    _run_bg "build-api"      bash "$SCRIPT_DIR/build-image.sh" api      "${IMAGE_TAG}"
    _run_bg "build-airflow"  bash "$SCRIPT_DIR/build-image.sh" airflow  "${IMAGE_TAG}"
    _run_bg "build-postgres" bash "$SCRIPT_DIR/build-image.sh" postgres "${IMAGE_TAG}"
    if [[ "$FRONTEND_MODE" == "cluster" ]]; then
      _run_bg "build-frontend" bash "$SCRIPT_DIR/build-image.sh" frontend "${IMAGE_TAG}"
    fi
    _wait_all
  else
    step 2 3 "image builds (skipped via --skip-build)"
    info "Using pre-built images tagged '${IMAGE_TAG}'."
  fi

  # -----------------------------------------------------------------------
  # Phase 3: Umbrella chart
  # -----------------------------------------------------------------------
  step 3 3 "umbrella chart (prod)"

  VALUES_ARGS=(-f "$CHART_DIR/values.yaml")
  if [[ -n "$EXTRA_VALUES" ]]; then
    if [[ ! -f "$EXTRA_VALUES" ]]; then
      error "Extra values file not found: $EXTRA_VALUES"
    fi
    VALUES_ARGS+=(-f "$EXTRA_VALUES")
    info "Using extra values file: $EXTRA_VALUES"
  else
    info "No --values overlay provided. Using values.yaml defaults only."
    info "Production deployments typically require an operator overlay."
  fi

  info "Adding/updating Helm repositories..."
  helm_repo_add_if_missing bitnami        "https://charts.bitnami.com/bitnami"
  helm_repo_add_if_missing apache-airflow "https://airflow.apache.org"
  helm repo update

  info "Building Helm chart dependencies..."
  _build_chart_deps "$CHART_DIR"

  # Build resolved extraEnv referencing the operator secret name
  local_extra_env_file="$(_build_airflow_extra_env_file "${SECRET_TO_CHECK}")"

  info "Installing DataSpoke umbrella chart (prod)..."
  # Derive frontend.enabled from FRONTEND_MODE (true=cluster, false=none)
  if [[ "$FRONTEND_MODE" == "cluster" ]]; then
    _prod_frontend_enabled="true"
  else
    _prod_frontend_enabled="false"
  fi
  api_image_prod_args=()
  while IFS= read -r _iarg; do
    api_image_prod_args+=("${_iarg}")
  done < <(_api_image_helm_set_args)

  # One class for every Ingress this release renders, taken from
  # DATASPOKE_KUBE_INGRESS_CLASS (required and verified against the cluster in
  # pre-flight above). These --set flags outrank a class written into the
  # operator's --values overlay: the env var is the single place an operator on
  # `alb` or `traefik` changes it. The API and frontend templates read
  # `ingress.className`, the Airflow chart reads
  # `ingress.apiServer.ingressClassName` — the wrong spelling is silently
  # dropped and GKE falls back to provisioning a GCE LoadBalancer.
  INGRESS_CLS="$(ingress_class)"

  helm upgrade --install dataspoke "$CHART_DIR" \
    "${VALUES_ARGS[@]}" \
    -n "${NS}" \
    --set "api.ingress.className=${INGRESS_CLS}" \
    --set "frontend.ingress.className=${INGRESS_CLS}" \
    --set "airflow.ingress.apiServer.ingressClassName=${INGRESS_CLS}" \
    "${api_image_prod_args[@]}" \
    --set-string postgresql.image.registry="" \
    --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
    --set-string "postgresql.image.tag=${IMAGE_TAG}" \
    --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
    --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
    --set "frontend.enabled=${_prod_frontend_enabled}" \
    --set "frontend.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend" \
    --set "frontend.image.tag=${IMAGE_TAG}" \
    --set-file "airflow.extraEnv=${local_extra_env_file}" \
    --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key" \
    --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret" \
    --set "airflow.fernetKeySecretName=dataspoke-airflow-metadata-encryption-key" \
    --set "secrets.existingSecret=${SECRET_TO_CHECK}" \
    --set "postgresql.auth.existingSecret=${SECRET_TO_CHECK}" \
    --set "redis.auth.existingSecret=${SECRET_TO_CHECK}" \
    --set "event-consumer.existingSecretName=${SECRET_TO_CHECK}" \
    --timeout 15m

  # -----------------------------------------------------------------------
  # Roll the Airflow pods still holding a superseded signing key
  # -----------------------------------------------------------------------
  if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" ]]; then
    _restart_airflow_key_consumers "${NS}"
  fi

  # -----------------------------------------------------------------------
  # Seed default admin user (idempotent)
  # -----------------------------------------------------------------------
  if [[ "$SKIP_SEED" == "false" ]]; then
    # The seed script kubectl execs into the API pod. `--timeout 15m` above
    # only waits for the Helm release, not pod readiness — a cold install can
    # still be running the API's wait-for-postgres/alembic-migrate init
    # containers here. Gate on the same idiom the dev branch uses (see
    # "Wait for DataSpoke API" above) so a not-yet-ready pod produces a
    # truthful error instead of the seed script's misleading "is the API
    # running?" — the seed hard-depends on this pod, so `error` (not `warn`)
    # is correct here.
    info "Waiting for DataSpoke API to become ready before seeding..."
    kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
      && info "DataSpoke API is ready." \
      || error "DataSpoke API did not become ready in time — check pod logs (kubectl logs -n '${NS}' deploy/dataspoke-api)."

    info "Seeding default admin user..."
    bash "$SCRIPT_DIR/post-install/seed-admin-user.sh"
  else
    info "Skipping admin user seed (--skip-seed)."
  fi

  echo ""
  echo "=== Installation complete (profile: prod) ==="
  echo ""
  echo "  Helm release: dataspoke  namespace: ${NS}"
  echo ""
  if [[ "$FRONTEND_MODE" == "cluster" ]]; then
    # Best-effort: resolve the deployed frontend ingress host
    _frontend_host="$(kubectl get ingress -n "${NS}" \
      -o jsonpath='{.items[?(@.metadata.name=="dataspoke-frontend")].spec.rules[0].host}' 2>/dev/null || true)"
    if [[ -n "${_frontend_host}" ]]; then
      echo "  Web UI: http://${_frontend_host}/"
    else
      echo "  Web UI: served at your configured frontend.ingress host (see your operator overlay)."
    fi
    if [[ "$SKIP_SEED" == "true" ]]; then
      echo "  Login:  (admin not seeded — --skip-seed)"
    else
      echo "  Login:  dataspoke@dataspoke.local / dataspoke  (rotate via PATCH /auth/me)"
    fi
  else
    echo "  Frontend: disabled (--frontend none)."
  fi
  echo ""
  echo "  Post-install: configure peripherals and runtime settings via:"
  echo "    /api/v1/admin/peripherals/{datahub,langfuse}"
  echo "    /api/v1/admin/conf"
  echo ""
  info "Total elapsed: $((SECONDS - START_TIME))s"
  echo ""
fi
