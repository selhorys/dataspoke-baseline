#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Parse flags
#   --yes                skip the "remove all resources?" confirmation prompt
#   --delete-namespaces  skip the "delete namespaces?" prompt and delete them
# ---------------------------------------------------------------------------
YES=false
DELETE_NAMESPACES=false
for arg in "$@"; do
  case "$arg" in
    --yes) YES=true ;;
    --delete-namespaces) DELETE_NAMESPACES=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env"
fi
source "$SCRIPT_DIR/.env"

echo ""
echo "=== Uninstalling DataSpoke dev environment ==="
echo ""

# ---------------------------------------------------------------------------
# Confirm before proceeding
# ---------------------------------------------------------------------------
if [[ "${YES}" != true ]]; then
  read -r -p "Remove all dev_env resources? [y/N] " CONFIRM
  if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    info "Aborted — no changes made."
    exit 0
  fi
fi

echo ""

# ---------------------------------------------------------------------------
# Uninstall lock service
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/dataspoke-lock/uninstall.sh" ]]; then
  info "Running dataspoke-lock/uninstall.sh..."
  bash "$SCRIPT_DIR/dataspoke-lock/uninstall.sh"
else
  warn "dataspoke-lock/uninstall.sh not found — skipping."
fi

# ---------------------------------------------------------------------------
# Uninstall dataspoke-example
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/dataspoke-example/uninstall.sh" ]]; then
  info "Running dataspoke-example/uninstall.sh..."
  bash "$SCRIPT_DIR/dataspoke-example/uninstall.sh"
else
  warn "dataspoke-example/uninstall.sh not found — skipping."
fi

# ---------------------------------------------------------------------------
# Uninstall DataSpoke infrastructure
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/dataspoke-infra/uninstall.sh" ]]; then
  info "Running dataspoke-infra/uninstall.sh..."
  bash "$SCRIPT_DIR/dataspoke-infra/uninstall.sh"
else
  warn "dataspoke-infra/uninstall.sh not found — skipping."
fi

# ---------------------------------------------------------------------------
# Uninstall Langfuse (independent namespace — no shared infra dependency)
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/langfuse/uninstall.sh" ]]; then
  info "Running langfuse/uninstall.sh..."
  bash "$SCRIPT_DIR/langfuse/uninstall.sh"
else
  warn "langfuse/uninstall.sh not found — skipping."
fi

# ---------------------------------------------------------------------------
# Uninstall DataHub
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/datahub/uninstall.sh" ]]; then
  info "Running datahub/uninstall.sh..."
  bash "$SCRIPT_DIR/datahub/uninstall.sh"
else
  warn "datahub/uninstall.sh not found — skipping."
fi

# ---------------------------------------------------------------------------
# Uninstall nginx-ingress controller
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/nginx-ingress/uninstall.sh" ]]; then
  info "Running nginx-ingress/uninstall.sh..."
  bash "$SCRIPT_DIR/nginx-ingress/uninstall.sh"
else
  warn "nginx-ingress/uninstall.sh not found — skipping."
fi

# ---------------------------------------------------------------------------
# Optionally delete namespaces
# ---------------------------------------------------------------------------
echo ""
NAMESPACES=(
  "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
  "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
  "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
  "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"
)

if [[ "${DELETE_NAMESPACES}" != true ]]; then
  read -r -p "Delete namespaces (${NAMESPACES[*]})? [y/N] " CONFIRM_NS
  [[ "${CONFIRM_NS}" =~ ^[Yy]$ ]] && DELETE_NAMESPACES=true
fi
if [[ "${DELETE_NAMESPACES}" == true ]]; then
  for NS in "${NAMESPACES[@]}"; do
    if kubectl get namespace "${NS}" >/dev/null 2>&1; then
      info "Deleting namespace '${NS}'..."
      kubectl delete namespace "${NS}"
    else
      info "Namespace '${NS}' does not exist — skipping."
    fi
  done
  info "Namespaces deleted."
else
  info "Namespaces retained."
fi

echo ""
info "Uninstall complete."
echo ""
