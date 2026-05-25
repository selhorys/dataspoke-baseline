#!/usr/bin/env bash
# DataSpoke uninstaller.
#
# Usage: uninstall.sh --profile {dev|prod} [--yes] [--delete-namespaces]
#
#   --profile {dev|prod}   Required. Selects which component set to tear down.
#   --yes                  Skip the confirmation prompt.
#   --delete-namespaces    Delete namespaces after uninstalling releases.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
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
YES=false
DELETE_NAMESPACES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --yes) YES=true; shift ;;
    --delete-namespaces) DELETE_NAMESPACES=true; shift ;;
    *) shift ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  error "--profile {dev|prod} is required."
fi

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE"
fi
source "$ENV_FILE"

echo ""
echo "=== Uninstalling DataSpoke (profile: ${PROFILE}) ==="
echo ""

# ---------------------------------------------------------------------------
# Confirm before proceeding
# ---------------------------------------------------------------------------
if [[ "${YES}" != true ]]; then
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
    PERIPHERALS_DIR="$(cd "$SCRIPT_DIR/../peripherals" && pwd)"
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
  info "Deleting dataspoke PVCs..."
  for pvc in $(kubectl get pvc -n "${NS}" -l app.kubernetes.io/instance=dataspoke \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    kubectl delete pvc "$pvc" -n "${NS}" 2>/dev/null \
      && info "  Deleted PVC '${pvc}'." \
      || warn "  Could not delete PVC '${pvc}'."
  done
  for SECRET in dataspoke-postgres-secret dataspoke-redis-secret \
                dataspoke-internal-auth dataspoke-llm-secret \
                dataspoke-datahub-secret dataspoke-langfuse-secret; do
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
  info "Deleting Langfuse PVCs..."
  for pvc in $(kubectl get pvc -n "${LANGFUSE_NS}" \
      -l app.kubernetes.io/instance=langfuse \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    kubectl delete pvc "$pvc" -n "${LANGFUSE_NS}" 2>/dev/null \
      && info "  Deleted PVC '${pvc}'." \
      || warn "  Could not delete PVC '${pvc}'."
  done
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
  info "Removing nginx-ingress controller..."
  if helm status ingress-nginx -n "ingress-nginx" >/dev/null 2>&1; then
    helm uninstall ingress-nginx -n "ingress-nginx"
  else
    warn "Helm release 'ingress-nginx' not found — skipping."
  fi
  if kubectl get namespace "ingress-nginx" >/dev/null 2>&1; then
    kubectl delete namespace "ingress-nginx"
  fi

  # 7. Optionally delete application namespaces
  NAMESPACES=("${DATAHUB_NS}" "${NS}" "${LANGFUSE_NS}" "${DUMMY_NS}")
  echo ""
  if [[ "${DELETE_NAMESPACES}" != true ]]; then
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

  echo ""
  if [[ "${DELETE_NAMESPACES}" != true ]]; then
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
  fi
fi

echo ""
info "Uninstall complete."
echo ""
