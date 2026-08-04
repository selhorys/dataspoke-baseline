#!/usr/bin/env bash
# Install the nginx-ingress controller and write the assigned external IP/domain
# back to helm-charts/.env.dev for downstream scripts.
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

echo ""
echo "=== Configuring ingress (nginx-ingress) ==="
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
use_context "${DATASPOKE_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Shared mode: reuse a pre-existing cluster ingress controller.
#
# We do NOT install, upgrade, or own the controller (other systems on the
# cluster depend on it). We only verify it is present and that the operator
# has pre-set DATASPOKE_KUBE_INGRESS_DOMAIN. HTTP services ride that domain
# (records published by the cluster's external-dns); TCP services are reached
# via `kubectl port-forward` (bin/port-forward.sh), not the ingress — so no
# IP or TCP test variables are derived here.
# ---------------------------------------------------------------------------
if [[ "$(ingress_mode)" == "shared" ]]; then
  info "Ingress mode: shared — using the pre-existing cluster ingress controller."

  : "${DATASPOKE_KUBE_INGRESS_DOMAIN:?DATASPOKE_KUBE_INGRESS_DOMAIN must be pre-set in .env for shared ingress mode (e.g. dataspoke-dev.your-host.com)}"

  INGRESS_CLASS="$(ingress_class)"
  if ! kubectl get ingressclass "${INGRESS_CLASS}" >/dev/null 2>&1; then
    error "IngressClass '${INGRESS_CLASS}' not found in the cluster. Set DATASPOKE_KUBE_INGRESS_CLASS to the shared controller's class, or install a controller."
  fi
  info "IngressClass '${INGRESS_CLASS}' is present."

  SCHEME="$(ingress_scheme)"
  GMS_HOST="$(datahub_gms_host)"
  echo ""
  info "Shared ingress verified. DataSpoke virtual hosts will be published (${SCHEME}) under:"
  echo "  DataSpoke UI:  ${SCHEME}://app.${DATASPOKE_KUBE_INGRESS_DOMAIN}/"
  echo "  DataSpoke API: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/api/v1/..."
  echo "  DataHub UI:    ${SCHEME}://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN}/"
  echo "  DataHub GMS:   ${SCHEME}://${GMS_HOST}/"
  echo "  Airflow UI:    ${SCHEME}://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN}/"
  echo "  Langfuse UI:   ${SCHEME}://langfuse.${DATASPOKE_KUBE_INGRESS_DOMAIN}/"
  echo ""
  echo "TCP services (Postgres/Redis/Kafka/lock) are not exposed via the shared"
  echo "controller — reach them from a laptop with: ./helm-charts/bin/port-forward.sh"
  echo ""
  info "nginx-ingress step complete (shared mode — controller left untouched)."
  exit 0
fi

# ---------------------------------------------------------------------------
# Managed mode: install and own an nginx-ingress controller (GKE/minikube).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Add / update Helm repo
# ---------------------------------------------------------------------------
info "Adding/updating ingress-nginx Helm repository..."
helm_repo_add_if_missing ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update ingress-nginx

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "ingress-nginx"
NS="ingress-nginx"

# ---------------------------------------------------------------------------
# Install / upgrade ingress-nginx
# ---------------------------------------------------------------------------
PERIPHERALS_DIR="$(cd "$BIN_DIR/../dev-peripherals" && pwd)"

# ---------------------------------------------------------------------------
# Render namespace placeholders in values.yaml before passing to Helm.
# This mirrors the __DATAHUB_GMS_INGRESS_HOST__ sed pattern in bin/dev-peripherals/datahub.sh.
# The tcp: map carries __*_NS__ tokens that must resolve to actual namespace names.
# ---------------------------------------------------------------------------
: "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:?required in .env for nginx-ingress tcp services}"
: "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE:?required in .env for nginx-ingress tcp services}"
: "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE:?required in .env for nginx-ingress tcp services}"

RENDERED_VALUES="$(mktemp)"
trap 'rm -f "$RENDERED_VALUES"' EXIT
sed \
  -e "s|__DATASPOKE_NS__|${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}|g" \
  -e "s|__DATAHUB_NS__|${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}|g" \
  -e "s|__DUMMY_DATA_NS__|${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}|g" \
  "$PERIPHERALS_DIR/nginx-ingress/values.yaml" > "$RENDERED_VALUES"

# The controller registers its IngressClass under the same name every DataSpoke
# Ingress binds to (ingress_class()), so controller and resources cannot drift.
INGRESS_CLASS="$(ingress_class)"

info "Installing ingress-nginx controller (IngressClass '${INGRESS_CLASS}')..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace "${NS}" \
  --values "$RENDERED_VALUES" \
  --set "controller.ingressClassResource.name=${INGRESS_CLASS}" \
  --timeout 5m

# ---------------------------------------------------------------------------
# Wait for the controller Deployment to roll out.
#
# On GKE Autopilot the cluster scales from 0 nodes, so the controller pod and
# its backing node can take a few minutes to come up. Block on the rollout
# condition (not a fixed sleep) before polling for the LoadBalancer IP, which
# GCP only assigns once the controller has a schedulable node.
# ---------------------------------------------------------------------------
info "Waiting for ingress-nginx-controller rollout (up to 5m)..."
kubectl rollout status deployment/ingress-nginx-controller \
  -n "${NS}" --timeout=5m

# ---------------------------------------------------------------------------
# Wait for LoadBalancer external IP
#
# On a GKE Autopilot cluster scaling from 0 nodes, the LB controller has no
# hosts to attach until nodes provision ("cannot EnsureLoadBalancer() with no
# hosts"), then the GCP target pool churns for a few minutes while Autopilot
# rebalances. A warm-cluster budget (120s) is too short for this cold start, so
# default to 5m; override with DATASPOKE_INGRESS_IP_TIMEOUT.
# ---------------------------------------------------------------------------
TIMEOUT="${DATASPOKE_INGRESS_IP_TIMEOUT:-300}"
info "Waiting for LoadBalancer external IP (up to ${TIMEOUT}s)..."
EXTERNAL_IP=""
ELAPSED=0

while [[ -z "${EXTERNAL_IP}" && ${ELAPSED} -lt ${TIMEOUT} ]]; do
  EXTERNAL_IP=$(kubectl get svc ingress-nginx-controller \
    -n "${NS}" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)

  if [[ -z "${EXTERNAL_IP}" ]]; then
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    if (( ELAPSED % 30 == 0 )); then
      info "  Still waiting... (${ELAPSED}s elapsed)"
    fi
  fi
done

if [[ -z "${EXTERNAL_IP}" ]]; then
  error "LoadBalancer did not receive an external IP within ${TIMEOUT}s. On an Autopilot cold start this may just be slow node provisioning — re-run, or raise DATASPOKE_INGRESS_IP_TIMEOUT. Otherwise check GKE firewall rules and Autopilot quotas."
fi

info "External IP assigned: ${EXTERNAL_IP}"

# ---------------------------------------------------------------------------
# Compute domain and write to .env
# ---------------------------------------------------------------------------
INGRESS_DOMAIN="${EXTERNAL_IP}.nip.io"

# Write ingress IP/domain
upsert_env_var DATASPOKE_KUBE_INGRESS_IP     "${EXTERNAL_IP}"     "${ENV_FILE}"
upsert_env_var DATASPOKE_KUBE_INGRESS_DOMAIN "${INGRESS_DOMAIN}"  "${ENV_FILE}"

# ---------------------------------------------------------------------------
# Derive and write runtime variables that depend on the ingress IP/domain.
# ---------------------------------------------------------------------------

# Tier A: HTTP endpoints (use domain-based URLs)
SCHEME="$(ingress_scheme)"
upsert_env_var DATASPOKE_DEV_AIRFLOW_URL      "${SCHEME}://airflow.${INGRESS_DOMAIN}"      "${ENV_FILE}"

# Tier B: TCP endpoints (use IP directly)
upsert_env_var DATASPOKE_DEV_POSTGRES_HOST    "${EXTERNAL_IP}"                        "${ENV_FILE}"
upsert_env_var DATASPOKE_DEV_REDIS_HOST       "${EXTERNAL_IP}"                        "${ENV_FILE}"

# Example data sources
upsert_env_var DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST "${EXTERNAL_IP}"                "${ENV_FILE}"
upsert_env_var DATASPOKE_DEV_DUMMY_DATA_KAFKA_BROKERS "${EXTERNAL_IP}:9104"           "${ENV_FILE}"

info "Written to .env:"
info "  DATASPOKE_KUBE_INGRESS_IP=${EXTERNAL_IP}"
info "  DATASPOKE_KUBE_INGRESS_DOMAIN=${INGRESS_DOMAIN}"
info "  + 5 derived test variables (DATASPOKE_DEV_POSTGRES_HOST, DATASPOKE_DEV_REDIS_HOST, etc.)"

# ---------------------------------------------------------------------------
# Print access summary
# ---------------------------------------------------------------------------
echo ""
info "nginx-ingress controller is ready."
kubectl get pods -n "${NS}"
echo ""
echo "Ingress external IP: ${EXTERNAL_IP}"
echo ""
echo "HTTP endpoints (Tier A):"
echo "  DataSpoke UI:  ${SCHEME}://app.${INGRESS_DOMAIN}/"
echo "  DataSpoke API: ${SCHEME}://api.${INGRESS_DOMAIN}/api/v1/..."
echo "  DataHub UI:    ${SCHEME}://datahub.${INGRESS_DOMAIN}/"
echo "  DataHub GMS:   ${SCHEME}://datahub-gms.${INGRESS_DOMAIN}/"
echo "  Airflow UI:    ${SCHEME}://airflow.${INGRESS_DOMAIN}/"
echo ""
echo "TCP endpoints (Tier B):"
echo "  PostgreSQL:      ${EXTERNAL_IP}:9201"
echo "  Redis:           ${EXTERNAL_IP}:9202"
echo "  DataHub Kafka:   ${EXTERNAL_IP}:9005"
echo "  Example PG:      ${EXTERNAL_IP}:9102"
echo "  Example Kafka:   ${EXTERNAL_IP}:9104"
echo "  Lock API:        ${EXTERNAL_IP}:9221"
echo ""
