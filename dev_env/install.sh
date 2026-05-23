#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
FROM_COMPONENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-component) FROM_COMPONENT="${2:-}"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--from-component <name>]"
      echo ""
      echo "Components (in order): nginx-ingress datahub langfuse dataspoke-infra dataspoke-example dataspoke-lock"
      exit 0
      ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env — copy and edit it before running this script."
fi
source "$SCRIPT_DIR/.env"

# Capture start time for step progress markers.
START_TIME=$SECONDS
export START_TIME

echo ""
echo "=== Installing DataSpoke dev environment ==="
echo ""

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Switch Kubernetes context
# ---------------------------------------------------------------------------
use_context "${DATASPOKE_DEV_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Create namespaces (idempotent — always runs regardless of --from-component)
# ---------------------------------------------------------------------------
ensure_namespace "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
ensure_namespace "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}"
ensure_namespace "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
ensure_namespace "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

# ---------------------------------------------------------------------------
# Component list (single source of truth for ordering)
# ---------------------------------------------------------------------------
COMPONENTS=(nginx-ingress datahub langfuse dataspoke-infra dataspoke-example dataspoke-lock)
TOTAL=${#COMPONENTS[@]}

# Resolve start index for --from-component
START_INDEX=0
if [[ -n "$FROM_COMPONENT" ]]; then
  found=false
  for i in "${!COMPONENTS[@]}"; do
    if [[ "${COMPONENTS[$i]}" == "$FROM_COMPONENT" ]]; then
      START_INDEX=$i
      found=true
      break
    fi
  done
  if [[ "$found" != "true" ]]; then
    error "Unknown component '${FROM_COMPONENT}'. Valid names: ${COMPONENTS[*]}"
  fi
fi

# ---------------------------------------------------------------------------
# Install components
# ---------------------------------------------------------------------------
for i in "${!COMPONENTS[@]}"; do
  comp="${COMPONENTS[$i]}"
  n=$(( i + 1 ))

  if (( i < START_INDEX )); then
    info "Skipping ${comp} (--from-component ${FROM_COMPONENT})"
    continue
  fi

  step "$n" "$TOTAL" "$comp"
  bash "$SCRIPT_DIR/${comp}/install.sh"
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
# Re-read .env so the summary prints values written by child scripts.
source "$SCRIPT_DIR/.env"

echo ""
echo "=== Installation complete ==="
echo ""
echo "Namespaces:"
kubectl get namespaces "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}" \
  "${DATASPOKE_DEV_KUBE_DATASPOKE_NAMESPACE}" \
  "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}" \
  "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}" 2>/dev/null || true
echo ""
echo "Ingress endpoints (via nginx-ingress at ${DATASPOKE_DEV_INGRESS_IP:-<not set>}):"
echo ""
echo "  DataHub UI:    http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/"
echo "  DataHub GMS:   http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/gms/"
echo "  DataSpoke API: http://app.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/api/v1/"
echo "  Airflow UI:    http://airflow.${DATASPOKE_DEV_INGRESS_DOMAIN:-<not set>}/"
echo "  Langfuse UI:   ${DATASPOKE_DEV_LANGFUSE_HOST:-http://langfuse.<not set>}/"
echo ""
echo "  PostgreSQL:    ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9201"
echo "  Redis:         ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9202"
echo "  DataHub Kafka: ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9005"
echo "  Example PG:    ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9102"
echo "  Example Kafka: ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9104"
echo "  Lock API:      ${DATASPOKE_DEV_INGRESS_IP:-<not set>}:9221"
echo ""
echo "  Credentials:"
echo "    DataHub:  datahub / datahub"
echo "    Airflow:  ${DATASPOKE_AIRFLOW_USER:-admin} / ${DATASPOKE_AIRFLOW_PASSWORD:-admin}"
echo ""
echo "Environment:"
echo "  .env has been populated with ingress-derived variables."
echo "  Run 'source .env' to load them into your shell."
echo ""
echo "API is deployed in-cluster. To rebuild after code changes:"
echo "  ./dataspoke-test-mode.sh"
echo ""
echo "Seed dummy data:"
echo "  cd .. && uv sync"
echo "  uv run python -m tests.integration.util --reset-seed"
echo ""
info "Total elapsed: $((SECONDS - START_TIME))s ($(printf '%dm%02ds' $(( (SECONDS - START_TIME) / 60 )) $(( (SECONDS - START_TIME) % 60 ))))"
echo ""
