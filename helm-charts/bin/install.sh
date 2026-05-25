#!/usr/bin/env bash
# DataSpoke installation entry point.
#
# Usage: install.sh --profile {dev|prod} [OPTIONS]
#
# OPTIONS
#   --components <csv>    Subset of components to install (default: all-for-profile).
#                         Names: nginx-ingress, datahub, langfuse, dataspoke-infra,
#                                api, dummy-data, dev-lock, seed
#   --from-component <n>  Resume an interrupted full install at <n>.
#   --skip-build          Skip Docker image rebuilds (api/airflow/postgres).
#   --skip-seed           Skip post-install admin-API seeding (dev only).
#   --values <path>       Extra values file for the umbrella chart (prod).
#   --image-tag <tag>     Override image tag (default: dev).
#   --help, -h            Print this usage message.
#
# The --components api path rebuilds the API image, runs helm upgrade, and
# waits for rollout. This replaces the former dataspoke-test-mode.sh workflow.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HELM_CHARTS_DIR/.." && pwd)"
CHART_DIR="$HELM_CHARTS_DIR/dataspoke"
ENV_FILE="$HELM_CHARTS_DIR/.env"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
PROFILE=""
COMPONENTS_CSV=""
FROM_COMPONENT=""
SKIP_BUILD=false
SKIP_SEED=false
EXTRA_VALUES=""
IMAGE_TAG="dev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)         PROFILE="${2:-}"; shift 2 ;;
    --components)      COMPONENTS_CSV="${2:-}"; shift 2 ;;
    --from-component)  FROM_COMPONENT="${2:-}"; shift 2 ;;
    --skip-build)      SKIP_BUILD=true; shift ;;
    --skip-seed)       SKIP_SEED=true; shift ;;
    --values)          EXTRA_VALUES="${2:-}"; shift 2 ;;
    --image-tag)       IMAGE_TAG="${2:-dev}"; shift 2 ;;
    --help|-h)
      grep '^#' "$0" | head -20 | sed 's/^# \{0,2\}//'
      exit 0
      ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  error "--profile {dev|prod} is required. Use --help for usage."
fi
if [[ "$PROFILE" != "dev" && "$PROFILE" != "prod" ]]; then
  error "Invalid profile '${PROFILE}'. Must be 'dev' or 'prod'."
fi

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE — copy helm-charts/.env.example and edit it."
fi
source "$ENV_FILE"
# Harden permissions immediately — the file may have been created via cp or
# an editor that inherits a permissive umask.
chmod 600 "$ENV_FILE" 2>/dev/null || true

START_TIME=$SECONDS
export START_TIME

echo ""
echo "=== DataSpoke installation (profile: ${PROFILE}) ==="
echo ""

# ---------------------------------------------------------------------------
# Pre-flight: required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm
info "kubectl and helm are available."

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

# ---------------------------------------------------------------------------
# Secret management helpers
# ---------------------------------------------------------------------------

# _ensure_dataspoke_secrets <namespace> <profile> [<secret_name>]
# Idempotent: creates the consolidated credential Secret in dev with
# auto-generated values (including Airflow webserver/jwt secrets).
# Migrates legacy dataspoke-postgres-secret / dataspoke-redis-secret if found.
# In prod: fails fast unless the Secret already exists.
# <secret_name> defaults to "dataspoke-secrets".
_ensure_dataspoke_secrets() {
  local ns="$1"
  local profile="$2"
  local secret_name="${3:-dataspoke-secrets}"

  if kubectl get secret "${secret_name}" -n "${ns}" >/dev/null 2>&1; then
    info "'${secret_name}' already exists in '${ns}' — leaving untouched."
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
    -n ${ns}
or pass --values <overlay.yaml> with secrets.existingSecret: <name>"
  fi

  # Dev: check for legacy Secrets to migrate (preserves existing PV data)
  local pg_user pg_password pg_db redis_password

  if kubectl get secret dataspoke-postgres-secret -n "${ns}" >/dev/null 2>&1; then
    info "Migrating credentials from legacy dataspoke-postgres-secret..."
    pg_password="$(kubectl get secret dataspoke-postgres-secret -n "${ns}" \
      -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 --decode)"
    pg_user="$(kubectl get secret dataspoke-postgres-secret -n "${ns}" \
      -o jsonpath='{.data.POSTGRES_USER}' 2>/dev/null | base64 --decode 2>/dev/null || echo "dataspoke")"
    pg_db="$(kubectl get secret dataspoke-postgres-secret -n "${ns}" \
      -o jsonpath='{.data.POSTGRES_DB}' 2>/dev/null | base64 --decode 2>/dev/null || echo "dataspoke")"

    # Validate migrated password contains only safe characters
    if [[ -n "${pg_password}" ]] && \
       ! [[ "${pg_password}" =~ ^[A-Za-z0-9+/_=.,!@#$%^\*-]{8,256}$ ]]; then
      error "Legacy Postgres password contains unsafe characters (whitespace, quotes, or backslash). Cannot migrate safely. Manually rotate the password or reset the PV: kubectl delete pvc -l app.kubernetes.io/instance=dataspoke -n ${ns}"
    fi
  else
    pg_password="$(openssl rand -hex 32)"
    pg_user="dataspoke"
    pg_db="dataspoke"
  fi

  if kubectl get secret dataspoke-redis-secret -n "${ns}" >/dev/null 2>&1; then
    info "Migrating password from legacy dataspoke-redis-secret..."
    redis_password="$(kubectl get secret dataspoke-redis-secret -n "${ns}" \
      -o jsonpath='{.data.REDIS_PASSWORD}' | base64 --decode)"
  else
    redis_password="$(openssl rand -hex 32)"
  fi

  local airflow_password internal_token jwt_secret airflow_webserver_secret airflow_jwt_secret
  airflow_password="$(openssl rand -hex 32)"
  internal_token="$(openssl rand -hex 32)"
  jwt_secret="$(openssl rand -hex 32)"
  airflow_webserver_secret="$(openssl rand -hex 16)"
  airflow_jwt_secret="$(openssl rand -hex 16)"

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
EOF
}

# _delete_legacy_secrets <namespace>
# Removes legacy Secrets that have been consolidated into dataspoke-secrets.
# Called after a successful helm upgrade.
_delete_legacy_secrets() {
  local ns="$1"
  for secret in dataspoke-postgres-secret dataspoke-redis-secret dataspoke-internal-auth; do
    if kubectl get secret "${secret}" -n "${ns}" >/dev/null 2>&1; then
      info "Removing legacy Secret '${secret}'..."
      kubectl delete secret "${secret}" -n "${ns}"
    fi
  done
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

# _ensure_airflow_key_secrets <namespace> <secret_name>
# Derives Airflow webserver/jwt secrets from the consolidated Secret and
# creates the two Airflow-chart-compatible Secrets. Idempotent.
_ensure_airflow_key_secrets() {
  local ns="$1"
  local secret_name="$2"

  local webserver_key jwt_key
  webserver_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY}' | base64 --decode)"
  jwt_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_JWT_SECRET}' | base64 --decode)"

  if [[ -z "${webserver_key}" || -z "${jwt_key}" ]]; then
    error "Secret '${secret_name}' is missing DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY or DATASPOKE_AIRFLOW_JWT_SECRET."
  fi

  if ! kubectl get secret dataspoke-airflow-api-secret-key -n "${ns}" >/dev/null 2>&1; then
    info "Creating dataspoke-airflow-api-secret-key..."
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
    info "  dataspoke-airflow-api-secret-key already exists — skipping."
  fi

  if ! kubectl get secret dataspoke-airflow-jwt-secret -n "${ns}" >/dev/null 2>&1; then
    info "Creating dataspoke-airflow-jwt-secret..."
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
    info "  dataspoke-airflow-jwt-secret already exists — skipping."
  fi
}

# _sync_env_from_secret <namespace> <secret_key> <env_var_name> [<secret_name>]
# Extracts <secret_key> from the consolidated Secret and writes/updates
# <env_var_name>=<value> in helm-charts/.env. Idempotent.
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
# Writes/updates a plain (non-Secret) value in helm-charts/.env. Idempotent.
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
# Validates ALL 8 required keys are present, non-empty, and not equal to known
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
  )

  for key in "${required_keys[@]}"; do
    local val
    val="$(kubectl get secret "${secret_name}" -n "${ns}" \
      -o jsonpath="{.data.${key}}" 2>/dev/null | base64 --decode 2>/dev/null || true)"
    if [[ -z "${val}" ]]; then
      error "prod Secret '${secret_name}' is missing required key: ${key}"
    fi
  done

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

# helm upgrade --install for the dataspoke umbrella chart (dev overlay).
# Used by both the full dev install (phase 3) and the --components api fast path.
_helm_upgrade_dataspoke_dev() {
  local ns="$1"
  local extra_env_file
  extra_env_file="$(_build_airflow_extra_env_file "dataspoke-secrets")"

  helm upgrade --install dataspoke "$CHART_DIR" \
    -f "$CHART_DIR/values-dev.yaml" \
    -n "${ns}" \
    --set-string global.imageRegistry="" \
    --set-string postgresql.image.registry="" \
    --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
    --set-string postgresql.image.tag="${IMAGE_TAG}" \
    --set "api.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api" \
    --set "api.image.tag=${IMAGE_TAG}" \
    --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
    --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
    --set airflow.images.airflow.pullPolicy=Always \
    --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002" \
    --set "api.ingress.hosts[0].host=app.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
    --set "api.ingress.hosts[0].paths[0].path=/" \
    --set "api.ingress.hosts[0].paths[0].pathType=Prefix" \
    --set "airflow.ingress.apiServer.hosts[0].name=airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
    --set-file "airflow.extraEnv=${extra_env_file}" \
    --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key" \
    --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret" \
    --timeout 10m
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
    info "==> Fast path: rebuild API image + helm upgrade + rollout restart"

    if [[ "$SKIP_BUILD" == "false" ]]; then
      info "Building API image (tag: ${IMAGE_TAG})..."
      bash "$SCRIPT_DIR/build-image.sh" api "${IMAGE_TAG}"
    else
      info "--skip-build: skipping API image build."
    fi

    info "Running helm upgrade for dataspoke umbrella chart..."
    use_context "${DATASPOKE_KUBE_CLUSTER}"
    _helm_upgrade_dataspoke_dev "${NS}"

    info "Restarting dataspoke-api deployment to pick up new image..."
    kubectl rollout restart deployment/dataspoke-api -n "${NS}"
    kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
      && info "dataspoke-api is ready." \
      || error "dataspoke-api did not become ready in time — check pod logs."

    # Verify Airflow DAGs
    DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"
    if [[ -n "$DOMAIN" ]]; then
      info "Verifying Airflow DAGs..."
      INTERNAL_TOKEN="$(kubectl exec -n "${NS}" deploy/dataspoke-api -c api -- \
        printenv DATASPOKE_INTERNAL_TOKEN 2>/dev/null || true)"
      if [[ -z "$INTERNAL_TOKEN" ]]; then
        warn "Could not read DATASPOKE_INTERNAL_TOKEN — skipping DAG verification."
      elif curl -sf -X POST "http://app.${DOMAIN}/internal/admin/dags/verify" \
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
      echo "  API:   http://app.${DATASPOKE_KUBE_INGRESS_DOMAIN}/api"
      echo "  ReDoc: http://app.${DATASPOKE_KUBE_INGRESS_DOMAIN}/redoc"
    fi
    echo ""
    echo "  To run integration tests:"
    echo "    DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/ -v"
    echo ""
    echo "  To stop the API:"
    echo "    kubectl scale deployment/dataspoke-api --replicas=0 -n '${NS}'"
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
    bash "$SCRIPT_DIR/peripherals/nginx-ingress.sh"
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
  else
    info "  --skip-build: skipping image builds."
  fi

  if _has_component datahub; then
    _run_bg "datahub" bash "$SCRIPT_DIR/peripherals/datahub.sh"
  fi
  if _has_component langfuse; then
    _run_bg "langfuse" bash "$SCRIPT_DIR/peripherals/langfuse.sh"
  fi

  _wait_all
  # Re-source .env to pick up any new values written by parallel tasks
  source "$ENV_FILE"

  # -----------------------------------------------------------------------
  # Phase 3: Umbrella chart (dataspoke-infra)
  # -----------------------------------------------------------------------
  if _has_component dataspoke-infra; then
    step 3 5 "dataspoke-infra (umbrella chart)"

    # Consolidated credential Secret (idempotent; migrates legacy if present)
    _ensure_dataspoke_secrets "${NS}" "dev" "dataspoke-secrets"

    # Airflow metadata DB connection Secret
    _derive_airflow_metadata_secret "${NS}" "dataspoke-secrets"

    # Airflow webserver/jwt key secrets (derived from dataspoke-secrets)
    _ensure_airflow_key_secrets "${NS}" "dataspoke-secrets"

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

    # Helm repo setup
    info "Adding/updating Helm repositories..."
    helm_repo_add_if_missing bitnami        "https://charts.bitnami.com/bitnami"
    helm_repo_add_if_missing apache-airflow "https://airflow.apache.org"
    helm repo update

    # Build chart dependencies
    info "Building Helm chart dependencies..."
    helm dependency build "$CHART_DIR"

    # Helm upgrade --install
    info "Installing DataSpoke umbrella chart..."
    _helm_upgrade_dataspoke_dev "${NS}"

    # Remove legacy Secrets that have been consolidated into dataspoke-secrets
    _delete_legacy_secrets "${NS}"

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

    # Laptop-side host/port for direct DB/cache access (via NodePort / LoadBalancer)
    _write_env_var "DATASPOKE_TEST_POSTGRES_HOST" "${DATASPOKE_KUBE_INGRESS_IP:-}"
    _write_env_var "DATASPOKE_TEST_POSTGRES_PORT" "9201"
    _write_env_var "DATASPOKE_TEST_REDIS_HOST"    "${DATASPOKE_KUBE_INGRESS_IP:-}"
    _write_env_var "DATASPOKE_TEST_REDIS_PORT"    "9202"
    _write_env_var "DATASPOKE_TEST_AIRFLOW_URL"   "http://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"

    info ".env updated with DATASPOKE_TEST_* values."
  fi

  # -----------------------------------------------------------------------
  # Phase 4: Parallel post-bootstrap
  # -----------------------------------------------------------------------
  step 4 5 "parallel post-bootstrap (dummy-data + dev-lock)"

  if _has_component dummy-data; then
    _run_bg "dummy-data" bash "$SCRIPT_DIR/peripherals/dummy-data.sh"
  fi
  if _has_component dev-lock; then
    _run_bg "dev-lock" bash "$SCRIPT_DIR/peripherals/dev-lock.sh"
  fi

  _wait_all

  # -----------------------------------------------------------------------
  # Phase 5: Post-install seeding
  # -----------------------------------------------------------------------
  if _has_component seed && [[ "$SKIP_SEED" == "false" ]]; then
    step 5 5 "post-install seeding"
    bash "$SCRIPT_DIR/post-install/seed-peripheral-config.sh"
    bash "$SCRIPT_DIR/post-install/seed-runtime-config.sh"
  else
    info "Skipping seeding (--skip-seed or 'seed' not in components)."
  fi

  # -----------------------------------------------------------------------
  # Re-read .env for summary
  # -----------------------------------------------------------------------
  source "$ENV_FILE"

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
  echo "Ingress endpoints (via nginx-ingress at ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}):"
  echo ""
  echo "  DataHub UI:    http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  DataHub GMS:   http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/gms/"
  echo "  DataSpoke API: http://app.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/api/v1/"
  echo "  Airflow UI:    http://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  Langfuse UI:   ${DATASPOKE_TEST_LANGFUSE_HOST:-http://langfuse.<not set>}/"
  echo ""
  echo "  PostgreSQL:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9201"
  echo "  Redis:         ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9202"
  echo "  DataHub Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9005"
  echo "  Example PG:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9102"
  echo "  Example Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9104"
  echo "  Lock API:      ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9221"
  echo ""
  echo "  Credentials (auto-generated): see DATASPOKE_TEST_AIRFLOW_{USER,PASSWORD} in helm-charts/.env"
  echo "  Langfuse: ${DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL:-dataspoke@dataspoke.local} / ${DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD:-<see .env>}"
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

  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  # -----------------------------------------------------------------------
  # Phase 1: Pre-flight (no nginx-ingress — operator's controller)
  # -----------------------------------------------------------------------
  step 1 3 "pre-flight"
  use_context "${DATASPOKE_KUBE_CLUSTER}"
  ensure_namespace "${NS}"

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
  helm dependency build "$CHART_DIR"

  # Build resolved extraEnv referencing the operator secret name
  local_extra_env_file="$(_build_airflow_extra_env_file "${SECRET_TO_CHECK}")"

  info "Installing DataSpoke umbrella chart (prod)..."
  helm upgrade --install dataspoke "$CHART_DIR" \
    "${VALUES_ARGS[@]}" \
    -n "${NS}" \
    --set "api.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api" \
    --set "api.image.tag=${IMAGE_TAG}" \
    --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
    --set-string "postgresql.image.tag=${IMAGE_TAG}" \
    --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
    --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
    --set-file "airflow.extraEnv=${local_extra_env_file}" \
    --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key" \
    --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret" \
    --set "secrets.existingSecret=${SECRET_TO_CHECK}" \
    --set "frontend.existingSecretName=${SECRET_TO_CHECK}" \
    --set "postgresql.auth.existingSecret=${SECRET_TO_CHECK}" \
    --set "redis.auth.existingSecret=${SECRET_TO_CHECK}" \
    --timeout 15m

  echo ""
  echo "=== Installation complete (profile: prod) ==="
  echo ""
  echo "  Helm release: dataspoke  namespace: ${NS}"
  echo ""
  echo "  Post-install: configure peripherals and runtime settings via:"
  echo "    /api/v1/admin/peripherals/{datahub,langfuse}"
  echo "    /api/v1/admin/conf"
  echo ""
  info "Total elapsed: $((SECONDS - START_TIME))s"
  echo ""
fi
