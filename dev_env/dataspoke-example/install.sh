#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$SCRIPT_DIR/../lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/../.env" ]]; then
  error ".env not found at $SCRIPT_DIR/../.env"
fi
source "$SCRIPT_DIR/../.env"

echo ""
echo "=== Installing dataspoke-example ==="
echo ""

NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"
PG_USER="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER:-postgres}"
PG_PASS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD:-ExampleDev2024!}"
PG_DB="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB:-example_db}"

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Namespace '${NS}' already exists."
else
  info "Creating namespace '${NS}'..."
  kubectl create namespace "${NS}"
fi

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
info "Applying manifests from $SCRIPT_DIR/manifests/..."
kubectl apply -f "$SCRIPT_DIR/manifests/" --namespace "${NS}"

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

info "Waiting for Kafka topic-init job to complete (timeout: 2m)..."
kubectl wait --for=condition=complete job/example-kafka-topic-init \
  --namespace "${NS}" \
  --timeout=2m

# ---------------------------------------------------------------------------
# Print connection info
# ---------------------------------------------------------------------------
echo ""
info "dataspoke-example installation complete."
echo ""
echo "Port-forward with:  ../dummy-data-port-forward.sh"
echo ""
echo "  PostgreSQL: localhost:${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT:-9102}  (-> example-postgres:5432)"
echo "  Connection: ${PG_USER} / ${PG_PASS} — database: ${PG_DB}"
echo ""
KAFKA_BROKERS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS:-localhost:9104}"
echo "  Kafka:      ${KAFKA_BROKERS}  (-> example-kafka:9094 EXTERNAL)"
echo ""
