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
INGRESS_IP="${DATASPOKE_DEV_INGRESS_IP:-localhost}"
info "Applying manifests (EXTERNAL listener → ${INGRESS_IP}:9104)..."
for manifest in "$SCRIPT_DIR"/manifests/*.yaml; do
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
echo "  PostgreSQL: ${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9102  (-> example-postgres:5432)"
echo "  Connection: ${PG_USER} / ${PG_PASS} — database: ${PG_DB}"
echo ""
echo "  Kafka:      ${DATASPOKE_DEV_INGRESS_IP:-<ingress-ip>}:9104  (-> example-kafka:9094 EXTERNAL)"
echo ""
