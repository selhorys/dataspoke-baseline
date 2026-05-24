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

    # Fail-fast on default Airflow credentials — the Airflow UI is reachable
    # via public nip.io ingress; default admin/admin credentials are a
    # public-ingress risk.
    if [[ -z "${DATASPOKE_AIRFLOW_PASSWORD:-}" || "${DATASPOKE_AIRFLOW_PASSWORD:-}" == "admin" ]]; then
      error "DATASPOKE_AIRFLOW_PASSWORD must be set to a non-default value (Airflow UI is reachable via public ingress). Edit helm-charts/.env."
    fi
    if [[ "${DATASPOKE_AIRFLOW_USER:-}" == "admin" ]]; then
      error "DATASPOKE_AIRFLOW_USER is set to 'admin' — rename the account to reduce brute-force exposure. Edit helm-charts/.env."
    fi

    if [[ "$SKIP_BUILD" == "false" ]]; then
      info "Building API image (tag: ${IMAGE_TAG})..."
      bash "$SCRIPT_DIR/build-image.sh" api "${IMAGE_TAG}"
    else
      info "--skip-build: skipping API image build."
    fi

    info "Running helm upgrade for dataspoke umbrella chart..."
    use_context "${DATASPOKE_KUBE_CLUSTER}"
    helm upgrade --install dataspoke "$CHART_DIR" \
      -f "$CHART_DIR/values-dev.yaml" \
      -n "${NS}" \
      --set postgresql.auth.existingSecret=dataspoke-postgres-secret \
      --set postgresql.auth.username="${DATASPOKE_POSTGRES_USER}" \
      --set postgresql.auth.database="${DATASPOKE_POSTGRES_DB}" \
      --set redis.auth.existingSecret=dataspoke-redis-secret \
      --set airflow.data.metadataConnection.user="${DATASPOKE_POSTGRES_USER}" \
      --set airflow.data.metadataConnection.pass="${DATASPOKE_POSTGRES_PASSWORD}" \
      --set global.postgresql.auth.password="${DATASPOKE_POSTGRES_PASSWORD}" \
      --set-string global.imageRegistry="" \
      --set-string postgresql.image.registry="" \
      --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
      --set-string postgresql.image.tag="${IMAGE_TAG}" \
      --set "api.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api" \
      --set "api.image.tag=${IMAGE_TAG}" \
      --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
      --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
      --set airflow.images.airflow.pullPolicy=Always \
      --set-string "secrets.postgres.user=${DATASPOKE_POSTGRES_USER}" \
      --set-string "secrets.postgres.password=${DATASPOKE_POSTGRES_PASSWORD}" \
      --set-string "secrets.redis.password=${DATASPOKE_REDIS_PASSWORD}" \
      --set-string "secrets.airflow.user=${DATASPOKE_AIRFLOW_USER}" \
      --set-string "secrets.airflow.password=${DATASPOKE_AIRFLOW_PASSWORD}" \
      --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002" \
      --set "api.ingress.hosts[0].host=app.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
      --set "api.ingress.hosts[0].paths[0].path=/" \
      --set "api.ingress.hosts[0].paths[0].pathType=Prefix" \
      --set "airflow.ingress.apiServer.hosts[0].name=airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
      --timeout 10m

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
  PIDS=()
  LABELS=()

  _run_bg() {
    local label="$1"; shift
    ( "$@" > /tmp/dataspoke-install-${label//\//-}.log 2>&1 ) &
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
        cat "/tmp/dataspoke-install-${label//\//-}.log" >&2 || true
        (( failed++ ))
      fi
    done
    PIDS=()
    LABELS=()
    if (( failed > 0 )); then
      error "${failed} background task(s) failed — see output above."
    fi
  }

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

    # Fail-fast on default Airflow credentials — the Airflow UI is reachable
    # via public nip.io ingress; default admin/admin credentials are a
    # public-ingress risk.
    if [[ -z "${DATASPOKE_AIRFLOW_PASSWORD:-}" || "${DATASPOKE_AIRFLOW_PASSWORD:-}" == "admin" ]]; then
      error "DATASPOKE_AIRFLOW_PASSWORD must be set to a non-default value (Airflow UI is reachable via public ingress). Edit helm-charts/.env."
    fi
    if [[ "${DATASPOKE_AIRFLOW_USER:-}" == "admin" ]]; then
      error "DATASPOKE_AIRFLOW_USER is set to 'admin' — rename the account to reduce brute-force exposure. Edit helm-charts/.env."
    fi

    # Create secrets from .env
    info "Creating dataspoke-postgres-secret..."
    kubectl create secret generic dataspoke-postgres-secret \
      --namespace "${NS}" \
      --from-literal=POSTGRES_USER="${DATASPOKE_POSTGRES_USER}" \
      --from-literal=POSTGRES_PASSWORD="${DATASPOKE_POSTGRES_PASSWORD}" \
      --from-literal=POSTGRES_DB="${DATASPOKE_POSTGRES_DB}" \
      --dry-run=client -o yaml | kubectl apply -f -

    info "Creating dataspoke-redis-secret..."
    kubectl create secret generic dataspoke-redis-secret \
      --namespace "${NS}" \
      --from-literal=REDIS_PASSWORD="${DATASPOKE_REDIS_PASSWORD}" \
      --dry-run=client -o yaml | kubectl apply -f -

    # Internal-auth token — generate on first run, persist to .env
    if [[ -z "${DATASPOKE_INTERNAL_TOKEN:-}" ]]; then
      info "DATASPOKE_INTERNAL_TOKEN unset — generating and appending to .env..."
      DATASPOKE_INTERNAL_TOKEN="$(openssl rand -hex 32)"
      printf '\nDATASPOKE_INTERNAL_TOKEN=%s\n' "${DATASPOKE_INTERNAL_TOKEN}" >> "$ENV_FILE"
    fi
    info "Applying dataspoke-internal-auth..."
    kubectl create secret generic dataspoke-internal-auth \
      --namespace "${NS}" \
      --from-literal=token="${DATASPOKE_INTERNAL_TOKEN}" \
      --dry-run=client -o yaml | kubectl apply -f -

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
    if [[ -n "${DATASPOKE_DEV_DATAHUB_TOKEN:-}" ]]; then
      info "Applying dataspoke-datahub-secret (DataHub PAT)..."
      kubectl create secret generic dataspoke-datahub-secret \
        --namespace "${NS}" \
        --from-literal=token="${DATASPOKE_DEV_DATAHUB_TOKEN}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_DEV_DATAHUB_TOKEN is unset — dataspoke-datahub-secret not created."
    fi

    # Langfuse secret key (out-of-band secret)
    if [[ -n "${DATASPOKE_DEV_LANGFUSE_SECRET_KEY:-}" ]]; then
      info "Applying dataspoke-langfuse-secret (Langfuse secret key)..."
      kubectl create secret generic dataspoke-langfuse-secret \
        --namespace "${NS}" \
        --from-literal=secret_key="${DATASPOKE_DEV_LANGFUSE_SECRET_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_DEV_LANGFUSE_SECRET_KEY is unset — dataspoke-langfuse-secret not created."
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
    helm upgrade --install dataspoke "$CHART_DIR" \
      -f "$CHART_DIR/values-dev.yaml" \
      -n "${NS}" \
      --set postgresql.auth.existingSecret=dataspoke-postgres-secret \
      --set postgresql.auth.username="${DATASPOKE_POSTGRES_USER}" \
      --set postgresql.auth.database="${DATASPOKE_POSTGRES_DB}" \
      --set redis.auth.existingSecret=dataspoke-redis-secret \
      --set airflow.data.metadataConnection.user="${DATASPOKE_POSTGRES_USER}" \
      --set airflow.data.metadataConnection.pass="${DATASPOKE_POSTGRES_PASSWORD}" \
      --set global.postgresql.auth.password="${DATASPOKE_POSTGRES_PASSWORD}" \
      --set-string global.imageRegistry="" \
      --set-string postgresql.image.registry="" \
      --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
      --set-string postgresql.image.tag="${IMAGE_TAG}" \
      --set "api.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api" \
      --set "api.image.tag=${IMAGE_TAG}" \
      --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
      --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
      --set airflow.images.airflow.pullPolicy=Always \
      --set-string "secrets.postgres.user=${DATASPOKE_POSTGRES_USER}" \
      --set-string "secrets.postgres.password=${DATASPOKE_POSTGRES_PASSWORD}" \
      --set-string "secrets.redis.password=${DATASPOKE_REDIS_PASSWORD}" \
      --set-string "secrets.airflow.user=${DATASPOKE_AIRFLOW_USER}" \
      --set-string "secrets.airflow.password=${DATASPOKE_AIRFLOW_PASSWORD}" \
      --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002" \
      --set "api.ingress.hosts[0].host=app.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
      --set "api.ingress.hosts[0].paths[0].path=/" \
      --set "api.ingress.hosts[0].paths[0].pathType=Prefix" \
      --set "airflow.ingress.apiServer.hosts[0].name=airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
      --timeout 10m

    # Ensure pgvector + AGE extensions
    info "Ensuring pgvector + age extensions in the dataspoke database..."

    # Validate the postgres username before interpolating it into SQL.
    # A typo or malicious write to .env with ';', '--', or backticks would
    # otherwise execute arbitrary SQL as the postgres superuser.
    if [[ ! "${DATASPOKE_POSTGRES_USER}" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
      error "DATASPOKE_POSTGRES_USER '${DATASPOKE_POSTGRES_USER}' is not a valid SQL identifier (^[a-zA-Z_][a-zA-Z0-9_]{0,62}$). Edit helm-charts/.env."
    fi

    kubectl rollout status statefulset/dataspoke-postgresql -n "${NS}" --timeout=5m >/dev/null 2>&1 || true
    # PGPASSWORD via env (not cmdline) is the right approach here: the password
    # does not appear in /proc/<pid>/cmdline. The postgres container is
    # single-tenant in the dev profile, so env-based credential passing is
    # acceptable; mounting a .pgpass Secret would add complexity without
    # meaningful security gain in this context.
    kubectl exec -n "${NS}" dataspoke-postgresql-0 -- \
      env PGPASSWORD="${DATASPOKE_POSTGRES_PASSWORD}" \
      psql -U postgres -d "${DATASPOKE_POSTGRES_DB}" -c "
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS age;
        GRANT USAGE ON SCHEMA ag_catalog TO ${DATASPOKE_POSTGRES_USER};
        GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO ${DATASPOKE_POSTGRES_USER};
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
  fi

  # -----------------------------------------------------------------------
  # Phase 4: Parallel post-bootstrap
  # -----------------------------------------------------------------------
  PIDS=()
  LABELS=()
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
  echo "  Langfuse UI:   ${DATASPOKE_DEV_LANGFUSE_HOST:-http://langfuse.<not set>}/"
  echo ""
  echo "  PostgreSQL:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9201"
  echo "  Redis:         ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9202"
  echo "  DataHub Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9005"
  echo "  Example PG:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9102"
  echo "  Example Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9104"
  echo "  Lock API:      ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9221"
  echo ""
  echo "  Credentials:"
  echo "    DataHub:  datahub / datahub"
  echo "    Airflow:  ${DATASPOKE_AIRFLOW_USER} / ${DATASPOKE_AIRFLOW_PASSWORD}"
  echo "    Langfuse: ${DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL:-dataspoke@dataspoke.local} / ${DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD:-<see .env>}"
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

  # -----------------------------------------------------------------------
  # Phase 2: Image builds (skippable)
  # -----------------------------------------------------------------------
  if [[ "$SKIP_BUILD" == "false" ]]; then
    step 2 3 "image builds (parallel)"
    PIDS=()
    LABELS=()

    _run_bg() {
      local label="$1"; shift
      ( "$@" > /tmp/dataspoke-install-${label//\//-}.log 2>&1 ) &
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
          cat "/tmp/dataspoke-install-${label//\//-}.log" >&2 || true
          (( failed++ ))
        fi
      done
      PIDS=()
      LABELS=()
      if (( failed > 0 )); then
        error "${failed} background task(s) failed — see output above."
      fi
    }

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
