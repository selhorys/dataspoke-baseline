#!/usr/bin/env bash
# DataSpoke uninstaller.
#
# Usage: uninstall.sh --profile {dev|prod} [OPTIONS]
#
#   --env-file <path>      Path to the env file (default: helm-charts/.env.<PROFILE>).
#   --profile {dev|prod}   Required. Selects which component set to tear down.
#   --components frontend  Targeted teardown of the frontend only — helm upgrade
#                          with frontend.enabled=false (the frontend is an optional
#                          umbrella subchart). Everything else is left untouched.
#                          Only `frontend` is supported; the api subchart is the
#                          core service and has no partial teardown (stop it with
#                          `kubectl scale deployment/dataspoke-api --replicas=0`).
#   --no-question          Skip every interactive prompt (gate, PVC, namespace).
#   --delete-pvcs          Also delete PersistentVolumeClaims (dev only).
#   --delete-namespaces    Also delete the application namespaces.
#   --delete-all           Shortcut for --delete-pvcs --delete-namespaces.
#   --help, -h             Print this usage message.
#
# Default behaviour: uninstalls Helm releases and chart-derived Secrets.
# PVCs and namespaces are preserved unless explicitly opted in.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
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
NO_QUESTION=false
DELETE_PVCS=false
DELETE_NAMESPACES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --components) COMPONENTS_CSV="${2:-}"; shift 2 ;;
    --no-question) NO_QUESTION=true; shift ;;
    --delete-pvcs) DELETE_PVCS=true; shift ;;
    --delete-namespaces) DELETE_NAMESPACES=true; shift ;;
    --delete-all) DELETE_PVCS=true; DELETE_NAMESPACES=true; shift ;;
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

echo ""
echo "=== Uninstalling DataSpoke (profile: ${PROFILE}) ==="
echo ""

# ---------------------------------------------------------------------------
# Targeted component teardown (--components)
# Only `frontend` is supported: it is an optional umbrella subchart, so teardown
# is a helm upgrade with frontend.enabled=false (not a `helm uninstall`). The api
# subchart is the core service (Airflow callbacks + seeding depend on it) and has
# no coherent partial teardown — stop it with `kubectl scale --replicas=0`.
# ---------------------------------------------------------------------------
if [[ -n "$COMPONENTS_CSV" ]]; then
  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
  case "$COMPONENTS_CSV" in
    frontend)
      use_context "${DATASPOKE_KUBE_CLUSTER}"
      if ! helm status dataspoke --namespace "${NS}" >/dev/null 2>&1; then
        error "Helm release 'dataspoke' not found in namespace '${NS}' — nothing to do."
      fi
      if [[ "${NO_QUESTION}" != true ]]; then
        read -r -p "Disable the frontend (helm upgrade frontend.enabled=false) in '${NS}'? [y/N] " CONFIRM
        if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
          info "Aborted — no changes made."
          exit 0
        fi
      fi
      info "Disabling frontend subchart (frontend.enabled=false)..."
      helm dependency build "$CHART_DIR" >/dev/null 2>&1 || true
      helm upgrade dataspoke "$CHART_DIR" \
        --namespace "${NS}" \
        --reuse-values \
        --set frontend.enabled=false \
        --wait --timeout 120s
      kubectl wait --for=delete deployment/dataspoke-frontend -n "${NS}" --timeout=120s 2>/dev/null || true
      echo ""
      info "Frontend removed; other components untouched."
      info "Redeploy with: ./helm-charts/bin/install.sh --profile ${PROFILE} --components frontend"
      echo ""
      exit 0
      ;;
    api)
      error "uninstall --components api is unsupported: api is the core service (Airflow callbacks + seeding depend on it). To stop it temporarily: kubectl scale deployment/dataspoke-api --replicas=0 -n '${NS}'"
      ;;
    *)
      error "uninstall --components supports only 'frontend' (got '${COMPONENTS_CSV}'). Omit --components for a full teardown."
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# Confirm before proceeding
# ---------------------------------------------------------------------------
if [[ "${NO_QUESTION}" != true ]]; then
  read -r -p "Remove all ${PROFILE} resources? [y/N] " CONFIRM
  if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    info "Aborted — no changes made."
    exit 0
  fi
fi

echo ""
use_context "${DATASPOKE_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# DEV PROFILE — reverse install order
# ---------------------------------------------------------------------------
if [[ "$PROFILE" == "dev" ]]; then
  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
  DATAHUB_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
  LANGFUSE_NS="${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
  DUMMY_NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

  # 1. dev-lock
  info "Removing dev-lock resources..."
  for RESOURCE in deployment/dev-lock service/dev-lock configmap/dev-lock-script; do
    if kubectl get "${RESOURCE}" -n "${NS}" >/dev/null 2>&1; then
      kubectl delete "${RESOURCE}" -n "${NS}"
    fi
  done

  # 2. dummy-data
  info "Removing dummy-data resources..."
  if kubectl get namespace "${DUMMY_NS}" >/dev/null 2>&1; then
    PERIPHERALS_DIR="$(cd "$SCRIPT_DIR/../dev-peripherals" && pwd)"
    kubectl delete -f "$PERIPHERALS_DIR/dummy-data/manifests/" \
      --namespace "${DUMMY_NS}" --ignore-not-found=true || true
    if kubectl get secret/example-postgres-secret -n "${DUMMY_NS}" >/dev/null 2>&1; then
      kubectl delete secret/example-postgres-secret -n "${DUMMY_NS}"
    fi
  else
    info "Namespace '${DUMMY_NS}' does not exist — skipping dummy-data cleanup."
  fi

  # 3. dataspoke umbrella chart
  info "Removing DataSpoke umbrella Helm release..."
  if helm status dataspoke --namespace "${NS}" >/dev/null 2>&1; then
    helm uninstall dataspoke --namespace "${NS}" --wait --timeout 60s 2>/dev/null \
      || warn "Helm uninstall timed out — force-deleting remaining pods."
  else
    warn "Helm release 'dataspoke' not found in namespace '${NS}' — skipping."
  fi
  kubectl delete pod -n "${NS}" -l app.kubernetes.io/instance=dataspoke \
    --force --grace-period=0 2>/dev/null || true
  for SECRET in dataspoke-secrets \
                dataspoke-airflow-metadata-db \
                dataspoke-airflow-api-secret-key \
                dataspoke-airflow-jwt-secret \
                dataspoke-llm-secret \
                dataspoke-datahub-secret \
                dataspoke-langfuse-secret; do
    if kubectl get secret "${SECRET}" -n "${NS}" >/dev/null 2>&1; then
      kubectl delete secret "${SECRET}" -n "${NS}"
    fi
  done

  # 4. Langfuse
  info "Removing Langfuse..."
  if helm status langfuse --namespace "${LANGFUSE_NS}" >/dev/null 2>&1; then
    helm uninstall langfuse --namespace "${LANGFUSE_NS}" --wait --timeout 60s 2>/dev/null \
      || warn "Langfuse Helm uninstall timed out — force-deleting remaining pods."
  else
    warn "Helm release 'langfuse' not found in namespace '${LANGFUSE_NS}' — skipping."
  fi
  kubectl delete pod -n "${LANGFUSE_NS}" \
    -l app.kubernetes.io/instance=langfuse \
    --force --grace-period=0 2>/dev/null || true
  if kubectl get secret dataspoke-langfuse-secret -n "${LANGFUSE_NS}" >/dev/null 2>&1; then
    kubectl delete secret dataspoke-langfuse-secret -n "${LANGFUSE_NS}"
  fi

  # 5. DataHub
  info "Removing DataHub..."
  if helm status datahub --namespace "${DATAHUB_NS}" >/dev/null 2>&1; then
    helm uninstall datahub --namespace "${DATAHUB_NS}"
  else
    warn "Helm release 'datahub' not found — skipping."
  fi
  if helm status datahub-prerequisites --namespace "${DATAHUB_NS}" >/dev/null 2>&1; then
    helm uninstall datahub-prerequisites --namespace "${DATAHUB_NS}"
  else
    warn "Helm release 'datahub-prerequisites' not found — skipping."
  fi
  for RESOURCE in ingress/datahub-gms service/datahub-kafka-external secret/mysql-secrets; do
    if kubectl get "${RESOURCE}" -n "${DATAHUB_NS}" >/dev/null 2>&1; then
      kubectl delete "${RESOURCE}" -n "${DATAHUB_NS}"
    fi
  done

  # 6. nginx-ingress
  if [[ "$(ingress_mode)" == "shared" ]]; then
    info "Ingress mode: shared — leaving the pre-existing cluster ingress controller untouched."
  else
    info "Removing nginx-ingress controller..."
    if helm status ingress-nginx -n "ingress-nginx" >/dev/null 2>&1; then
      helm uninstall ingress-nginx -n "ingress-nginx"
    else
      warn "Helm release 'ingress-nginx' not found — skipping."
    fi
    if kubectl get namespace "ingress-nginx" >/dev/null 2>&1; then
      kubectl delete namespace "ingress-nginx"
    fi
  fi

  # 7. Optionally delete PVCs (dataspoke + Langfuse)
  echo ""
  if [[ "${DELETE_PVCS}" != true && "${NO_QUESTION}" != true ]]; then
    read -r -p "Delete PVCs in '${NS}' and '${LANGFUSE_NS}'? [y/N] " CONFIRM_PVC
    [[ "${CONFIRM_PVC}" =~ ^[Yy]$ ]] && DELETE_PVCS=true
  fi
  if [[ "${DELETE_PVCS}" == true ]]; then
    for PVC_NS_LABEL in "${NS}:app.kubernetes.io/instance=dataspoke" \
                        "${LANGFUSE_NS}:app.kubernetes.io/instance=langfuse"; do
      PVC_NS="${PVC_NS_LABEL%%:*}"
      PVC_LABEL="${PVC_NS_LABEL#*:}"
      info "Deleting PVCs in '${PVC_NS}' (label ${PVC_LABEL})..."
      for pvc in $(kubectl get pvc -n "${PVC_NS}" -l "${PVC_LABEL}" \
          -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        kubectl delete pvc "$pvc" -n "${PVC_NS}" 2>/dev/null \
          && info "  Deleted PVC '${pvc}'." \
          || warn "  Could not delete PVC '${pvc}'."
      done
    done
  else
    info "PVCs retained."
  fi

  # 8. Optionally delete application namespaces
  NAMESPACES=("${DATAHUB_NS}" "${NS}" "${LANGFUSE_NS}" "${DUMMY_NS}")
  echo ""
  if [[ "${DELETE_NAMESPACES}" != true && "${NO_QUESTION}" != true ]]; then
    read -r -p "Delete namespaces (${NAMESPACES[*]})? [y/N] " CONFIRM_NS
    [[ "${CONFIRM_NS}" =~ ^[Yy]$ ]] && DELETE_NAMESPACES=true
  fi
  if [[ "${DELETE_NAMESPACES}" == true ]]; then
    for NS_TO_DEL in "${NAMESPACES[@]}"; do
      if kubectl get namespace "${NS_TO_DEL}" >/dev/null 2>&1; then
        info "Deleting namespace '${NS_TO_DEL}'..."
        kubectl delete namespace "${NS_TO_DEL}"
      else
        info "Namespace '${NS_TO_DEL}' does not exist — skipping."
      fi
    done
    info "Namespaces deleted."
  else
    info "Namespaces retained."
  fi

# ---------------------------------------------------------------------------
# PROD PROFILE
# ---------------------------------------------------------------------------
elif [[ "$PROFILE" == "prod" ]]; then
  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  info "Removing DataSpoke umbrella Helm release (prod)..."
  if helm status dataspoke --namespace "${NS}" >/dev/null 2>&1; then
    helm uninstall dataspoke --namespace "${NS}" --wait --timeout 120s
  else
    warn "Helm release 'dataspoke' not found in namespace '${NS}' — skipping."
  fi

  # Delete only the chart-derived Secrets; operator-owned dataspoke-secrets is preserved.
  for SECRET in dataspoke-airflow-metadata-db \
                dataspoke-airflow-api-secret-key \
                dataspoke-airflow-jwt-secret; do
    if kubectl get secret "${SECRET}" -n "${NS}" >/dev/null 2>&1; then
      info "Deleting chart-derived Secret '${SECRET}'..."
      kubectl delete secret "${SECRET}" -n "${NS}"
    fi
  done
  info "Operator-owned Secret 'dataspoke-secrets' (or secrets.existingSecret) retained."

  # ---------------------------------------------------------------------------
  # Delete namespace? Ask BEFORE printing the retained-resources summary below —
  # printing a "here's what survives" list and then immediately asking "delete
  # the namespace?" invites a 'y' that destroys everything just listed. If the
  # namespace ends up deleted, the summary is moot (it takes the PVCs/Secrets
  # below with it) and is skipped entirely.
  # ---------------------------------------------------------------------------
  echo ""
  if [[ "${DELETE_NAMESPACES}" != true && "${NO_QUESTION}" != true ]]; then
    read -r -p "Delete namespace '${NS}'? [y/N] " CONFIRM_NS
    [[ "${CONFIRM_NS}" =~ ^[Yy]$ ]] && DELETE_NAMESPACES=true
  fi
  if [[ "${DELETE_NAMESPACES}" == true ]]; then
    if kubectl get namespace "${NS}" >/dev/null 2>&1; then
      kubectl delete namespace "${NS}"
      info "Namespace '${NS}' deleted."
    fi
  else
    info "Namespace '${NS}' retained."

    # -------------------------------------------------------------------------
    # Retained-resources summary (echo/info only — this uninstaller never
    # deletes PVCs in prod; --delete-pvcs is dev-only).
    # -------------------------------------------------------------------------
    echo ""
    info "Resources retained in '${NS}' after this uninstall:"
    info "  PVCs:"
    CORE_PVCS_FOUND=()
    for pvc in data-dataspoke-postgresql-0 \
               redis-data-dataspoke-redis-master-0 \
               redis-data-dataspoke-redis-replicas-0; do
      if kubectl get pvc "${pvc}" -n "${NS}" >/dev/null 2>&1; then
        CORE_PVCS_FOUND+=("${pvc}")
        SIZE="$(kubectl get pvc "${pvc}" -n "${NS}" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null || echo '?')"
        info "    ${pvc}   ${SIZE}"
      else
        warn "    ${pvc} not found — expected to exist alongside a running install; check for a naming drift."
      fi
    done

    # Airflow log PVCs only exist when your overlay enables Airflow log
    # persistence (disabled in the shipped chart default — see
    # values-prod.example.yaml §Airflow log persistence) — probe first so the
    # common case (persistence off) prints nothing.
    LOG_PVCS=()
    for pvc in logs-dataspoke-airflow-scheduler-0 logs-dataspoke-airflow-triggerer-0; do
      if kubectl get pvc "${pvc}" -n "${NS}" >/dev/null 2>&1; then
        LOG_PVCS+=("${pvc}")
      fi
    done
    if [[ "${#LOG_PVCS[@]}" -gt 0 ]]; then
      info "  Also found Airflow log PVCs (present because your overlay enables Airflow log"
      info "  persistence — disabled in the shipped chart default):"
      for pvc in "${LOG_PVCS[@]}"; do
        SIZE="$(kubectl get pvc "${pvc}" -n "${NS}" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null || echo '?')"
        info "    ${pvc}   ${SIZE}"
      done
      warn "  These hold retained task logs by design — delete only if you no longer need that"
      warn "  post-mortem history."
    fi

    info "  Secrets:"
    info "    dataspoke-secrets (or your secrets.existingSecret name) — operator-owned"
    if kubectl get secret dataspoke-airflow-fernet-key -n "${NS}" >/dev/null 2>&1; then
      info "    dataspoke-airflow-fernet-key — keep-annotated by the Airflow chart"
      warn "      Coupled to the Postgres PVC above: keeping one without the other breaks Airflow —"
      warn "      if the Postgres PVC survives, existing encrypted Airflow connections are only"
      warn "      decryptable with this same fernet key."
    fi
    for oob_secret in dataspoke-llm-secret dataspoke-datahub-secret \
                      dataspoke-langfuse-secret dataspoke-smtp-secret; do
      if kubectl get secret "${oob_secret}" -n "${NS}" >/dev/null 2>&1; then
        info "    ${oob_secret} — out-of-band, not managed by this script"
      fi
    done

    info "  To delete manually:"
    if [[ "${#CORE_PVCS_FOUND[@]}" -gt 0 ]]; then
      info "    kubectl delete pvc ${CORE_PVCS_FOUND[*]} -n '${NS}'"
    fi
    if [[ "${#LOG_PVCS[@]}" -gt 0 ]]; then
      info "    kubectl delete pvc ${LOG_PVCS[*]} -n '${NS}'"
    fi
    info "    kubectl delete secret dataspoke-secrets dataspoke-airflow-fernet-key -n '${NS}'"
    warn "  Deleting 'dataspoke-secrets' (or your secrets.existingSecret) destroys the only copy"
    warn "  of all 12 credentials unless they also live in an external secrets manager, AND"
    warn "  strands the Postgres PVC above if you keep it (the running cluster still expects the"
    warn "  old DATASPOKE_POSTGRES_PASSWORD). Delete the Secret only together with the PVCs above,"
    warn "  or not at all."
    info "  Or delete the namespace '${NS}' for a full wipe — the only sanctioned full teardown in prod."
    echo ""
  fi
fi

echo ""
info "Uninstall complete."
echo ""
