#!/usr/bin/env bash
# Install dummy data sources (PostgreSQL + Kafka) for integration testing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$(cd "$BIN_DIR/.." && pwd)/.env"
PERIPHERALS_DIR="$(cd "$BIN_DIR/../dev-peripherals" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error ".env not found at $ENV_FILE — copy helm-charts/.env.example and edit it."
fi
source "$ENV_FILE"

echo ""
echo "=== Installing dummy-data sources ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"
PG_USER="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER:-postgres}"
PG_PASS="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-ExampleDev2024!}"
PG_DB="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB:-example_db}"

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "${NS}"

# ---------------------------------------------------------------------------
# Create Postgres secret (idempotent)
# ---------------------------------------------------------------------------
info "Creating example-postgres-secret..."
kubectl create secret generic example-postgres-secret \
  --namespace "${NS}" \
  --from-literal=POSTGRES_USER="${PG_USER}" \
  --from-literal=POSTGRES_PASSWORD="${PG_PASS}" \
  --from-literal=POSTGRES_DB="${PG_DB}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# Apply manifests
# ---------------------------------------------------------------------------
# Host advertised by the example-kafka EXTERNAL listener. In managed mode this
# is the ingress LoadBalancer IP (nginx TCP passthrough); in shared mode it is
# 127.0.0.1, reached via `kubectl port-forward` (bin/port-forward.sh). Clients
# reconnect to this host after the initial metadata lookup, so it must be
# reachable from the test environment.
INGRESS_IP="$(tcp_access_host)"
[[ -z "${INGRESS_IP}" ]] && INGRESS_IP="localhost"
info "Applying manifests (EXTERNAL listener → ${INGRESS_IP}:9104)..."
for manifest in "$PERIPHERALS_DIR/dummy-data/manifests/"*.yaml; do
  sed "s/__INGRESS_IP__/${INGRESS_IP}/g" "$manifest" | kubectl apply -n "${NS}" -f -
done

# ---------------------------------------------------------------------------
# Wait for deployments to be ready
# ---------------------------------------------------------------------------
info "Waiting for PostgreSQL deployment to be ready (timeout: 3m)..."
kubectl rollout status deployment/example-postgres \
  --namespace "${NS}" \
  --timeout=3m

info "Waiting for Kafka deployment to be ready (timeout: 3m)..."
kubectl rollout status deployment/example-kafka \
  --namespace "${NS}" \
  --timeout=3m

info "Waiting for Kafka topic-init job to complete (timeout: 5m)..."
kubectl wait --for=condition=complete job/example-kafka-topic-init \
  --namespace "${NS}" \
  --timeout=5m

# ---------------------------------------------------------------------------
# Print connection info
# ---------------------------------------------------------------------------
echo ""
info "dummy-data installation complete."
echo ""
echo "  PostgreSQL: ${INGRESS_IP}:9102  (-> example-postgres:5432)"
echo "  Connection: ${PG_USER} / ${PG_PASS} — database: ${PG_DB}"
echo ""
echo "  Kafka:      ${INGRESS_IP}:9104  (-> example-kafka:9094 EXTERNAL)"
echo ""
echo "  Seed test data with:"
echo "    uv run python -m tests.integration.util --reset-seed"
echo ""
