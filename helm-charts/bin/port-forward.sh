#!/usr/bin/env bash
# Open kubectl port-forwards for DataSpoke TCP services on 127.0.0.1.
#
# In shared ingress mode the cluster's ingress controller exposes only HTTP
# (80/443), so the TCP services (Postgres, Redis, Kafka, dev-lock) are not
# reachable through it. This script forwards them to localhost on the exact
# ports that integration tests and helm-charts/.env.dev expect (the DATASPOKE_DEV_*
# ports), so laptop-side tests and health-check.sh work against 127.0.0.1.
#
# Runs in the foreground and holds all forwards open; Ctrl-C tears them all down.
#
# Usage:
#   ./helm-charts/bin/port-forward.sh                   # forward all TCP services
#   ./helm-charts/bin/port-forward.sh --env-file <path> # use a specific env file
#   ./helm-charts/bin/port-forward.sh --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

ENV_FILE_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --help|-h) print_usage "$0"; exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

ENV_FILE="${ENV_FILE_ARG:-${ENV_FILE:-$(cd "$SCRIPT_DIR/.." && pwd)/.env.dev}}"

if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy the matching helm-charts/.env.<profile>.example and edit it."
fi
source "$ENV_FILE"

require_tools kubectl

DATASPOKE_KUBE_CLUSTER="${DATASPOKE_KUBE_CLUSTER:-}"
if [[ -z "$DATASPOKE_KUBE_CLUSTER" ]]; then
  error "DATASPOKE_KUBE_CLUSTER must be set in ${ENV_FILE}."
fi
use_context "${DATASPOKE_KUBE_CLUSTER}"

# This script is the TCP surface for shared ingress mode. In managed mode the
# same services are already reachable on the LoadBalancer IP, so the forwards
# only shadow localhost — warn, but proceed (harmless).
if [[ "$(ingress_mode)" != "shared" ]]; then
  warn "Ingress mode is '$(ingress_mode)', not 'shared'. TCP services are already on the LoadBalancer IP; port-forward is normally only needed in shared mode."
fi

DS_NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-}"
if [[ -z "$DS_NS" ]]; then
  error "DATASPOKE_KUBE_DATASPOKE_NAMESPACE must be set in ${ENV_FILE}."
fi
DH_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE:-}"
DD_NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE:-}"

# dev-lock is a dev-only peripheral (install.sh's DEV_ALL list; the prod
# branch never installs it) that happens to live in the same namespace as the
# three core services (DS_NS, set in both profiles), so it can't be told
# apart from them by namespace alone. Key its namespace on the profile the
# env file itself declares (seed_profile) instead: blank under a non-dev env
# file routes it through the same empty-namespace skip as DH_NS/DD_NS below,
# with no change to the PF_SPECS spec format.
DL_NS=""
if [[ "$(seed_profile "$ENV_FILE")" == "dev" ]]; then
  DL_NS="${DS_NS}"
fi

# Each spec: "<local-port>:<namespace>/<service>:<remote-port>".
# Mirrors the Tier-B map in dev-peripherals/nginx-ingress/values.yaml — the
# local ports are the canonical DATASPOKE_DEV_* ports.
PF_SPECS=(
  "9201:${DS_NS}/dataspoke-postgresql:5432"
  "9202:${DS_NS}/dataspoke-redis-master:6379"
  "9221:${DL_NS}/dev-lock:8080"
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
echo "DataSpoke port-forward"
echo "======================"
# Named before the first forward, matching health-check.sh's convention: this
# script binds fixed localhost ports that the dev stack and the integration
# suites treat as canonical, so an operator who meant one deployment and
# resolved another sees it here rather than reading its forwards as their own.
echo "  Env file:  ${ENV_FILE}"
echo "  Cluster:   ${DATASPOKE_KUBE_CLUSTER}"
echo "  Namespace: ${DS_NS}"
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

  if [[ -z "$ns" ]]; then
    warn "  skip 127.0.0.1:${local_port} — no namespace resolved for ${svc} in this env file (either a dev-only peripheral this profile doesn't install, or the namespace variable is present but left blank)"
    continue
  fi

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
  error "No services found to forward. Check ${ENV_FILE} — DATASPOKE_KUBE_DATASPOKE_NAMESPACE and any DATASPOKE_DEV_KUBE_*_NAMESPACE vars it declares must name namespaces that actually exist on the cluster."
fi

echo ""
info "${#PIDS[@]} port-forward(s) active. Leave this running while you test."
# The api_wired suite truncates data (tests/integration/util/__main__.py
# --reset-all) — only point an operator at it against a dev env file.
if [[ "$(seed_profile "$ENV_FILE")" == "dev" ]]; then
  info "Run integration tests in another shell:"
  echo "  set -a && source ${ENV_FILE} && set +a && uv run pytest tests/integration/api_wired/ -v"
fi
echo ""
wait
