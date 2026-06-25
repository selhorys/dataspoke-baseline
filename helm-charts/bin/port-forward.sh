#!/usr/bin/env bash
# Open kubectl port-forwards for DataSpoke TCP services on 127.0.0.1.
#
# In shared ingress mode the cluster's ingress controller exposes only HTTP
# (80/443), so the TCP services (Postgres, Redis, Kafka, dev-lock) are not
# reachable through it. This script forwards them to localhost on the exact
# ports that integration tests and helm-charts/.env expect (the DATASPOKE_TEST_*
# ports), so laptop-side tests and health-check.sh work against 127.0.0.1.
#
# Runs in the foreground and holds all forwards open; Ctrl-C tears them all down.
#
# Usage:
#   ./helm-charts/bin/port-forward.sh           # forward all TCP services
#   ./helm-charts/bin/port-forward.sh --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(cd "$SCRIPT_DIR/.." && pwd)/.env"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  print_usage "$0"
  exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE — copy helm-charts/.env.example and edit it."
fi
source "$ENV_FILE"

require_tools kubectl
use_context "${DATASPOKE_KUBE_CLUSTER}"

# This script is the TCP surface for shared ingress mode. In managed mode the
# same services are already reachable on the LoadBalancer IP, so the forwards
# only shadow localhost — warn, but proceed (harmless).
if [[ "$(ingress_mode)" != "shared" ]]; then
  warn "Ingress mode is '$(ingress_mode)', not 'shared'. TCP services are already on the LoadBalancer IP; port-forward is normally only needed in shared mode."
fi

DS_NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
DH_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
DD_NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

# Each spec: "<local-port>:<namespace>/<service>:<remote-port>".
# Mirrors the Tier-B map in dev-peripherals/nginx-ingress/values.yaml — the
# local ports are the canonical DATASPOKE_TEST_* ports.
PF_SPECS=(
  "9201:${DS_NS}/dataspoke-postgresql:5432"
  "9202:${DS_NS}/dataspoke-redis-master:6379"
  "9221:${DS_NS}/dev-lock:8080"
  "9005:${DH_NS}/datahub-kafka-external:9095"
  "9102:${DD_NS}/example-postgres:5432"
  "9104:${DD_NS}/example-kafka:9094"
)

PIDS=()
cleanup() {
  [[ ${#PIDS[@]} -gt 0 ]] && kill "${PIDS[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo ""
info "Opening port-forwards on 127.0.0.1 (Ctrl-C to stop)..."
echo ""
for spec in "${PF_SPECS[@]}"; do
  local_port="${spec%%:*}"
  rest="${spec#*:}"
  ns="${rest%%/*}"
  svc_remote="${rest#*/}"
  svc="${svc_remote%%:*}"
  remote="${svc_remote##*:}"

  if ! kubectl get "svc/${svc}" -n "${ns}" >/dev/null 2>&1; then
    warn "  skip 127.0.0.1:${local_port} — service ${ns}/${svc} not found (not installed?)"
    continue
  fi
  kubectl port-forward -n "${ns}" "svc/${svc}" \
    "${local_port}:${remote}" --address 127.0.0.1 >/dev/null 2>&1 &
  PIDS+=($!)
  info "  127.0.0.1:${local_port} -> ${ns}/${svc}:${remote} (pid $!)"
done

if [[ ${#PIDS[@]} -eq 0 ]]; then
  error "No services found to forward — is the dev stack installed?"
fi

echo ""
info "${#PIDS[@]} port-forward(s) active. Leave this running while you test."
info "Run integration tests in another shell:"
echo "  set -a && source helm-charts/.env && set +a && uv run pytest tests/integration/api_wired/ -v"
echo ""
wait
