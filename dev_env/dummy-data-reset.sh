#!/usr/bin/env bash
# dummy-data-reset.sh — Idempotent reset of dummy data in example-postgres and example-kafka.
#
# Usage:
#   cd dev_env && ./dummy-data-reset.sh
#
# Prerequisites:
#   - Kubernetes cluster running with dataspoke-dummy-data-01 namespace
#   - example-postgres and example-kafka deployments are Ready
#   - dev_env/.env is populated

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/helpers.sh"
source "$SCRIPT_DIR/.env"

NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"
PG_USER="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER}"
PG_DB="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB}"

SQL_DIR="$SCRIPT_DIR/dummy-data/sql"
KAFKA_DIR="$SCRIPT_DIR/dummy-data/kafka"

# ── 1. Verify pods are Ready ──────────────────────────────────────────────

info "Waiting for example-postgres to be Ready..."
kubectl wait --for=condition=Ready pod -l app=example-postgres \
  -n "${NS}" --timeout=120s

info "Waiting for example-kafka to be Ready..."
kubectl wait --for=condition=Ready pod -l app=example-kafka \
  -n "${NS}" --timeout=120s

# ── 2. Get pod names ──────────────────────────────────────────────────────

PG_POD=$(kubectl get pod -l app=example-postgres -n "${NS}" \
  -o jsonpath='{.items[0].metadata.name}') \
  || error "No example-postgres pod found in namespace '${NS}'."

info "Using PostgreSQL pod: ${PG_POD}"

# ── 3. Teardown: drop all custom schemas CASCADE ─────────────────────────

SCHEMAS=(
  catalog orders customers reviews publishers
  shipping inventory marketing products content storefront
)

info "Dropping custom schemas (CASCADE)..."
for schema in "${SCHEMAS[@]}"; do
  kubectl exec -n "${NS}" "${PG_POD}" -- \
    psql -U "${PG_USER}" -d "${PG_DB}" -c \
    "DROP SCHEMA IF EXISTS ${schema} CASCADE;" \
    2>/dev/null || true
done
info "Schemas dropped."

# ── 4. Execute SQL seed files in order ───────────────────────────────────

info "Executing SQL seed files..."
for sql_file in "$SQL_DIR"/*.sql; do
  filename=$(basename "$sql_file")
  info "  Running ${filename}..."
  kubectl exec -i -n "${NS}" "${PG_POD}" -- \
    psql -U "${PG_USER}" -d "${PG_DB}" < "$sql_file"
done
info "PostgreSQL seed complete."

# ── 5. Reset Kafka topics and produce seed messages ──────────────────────

info "Resetting Kafka topics..."
bash "$KAFKA_DIR/init-topics.sh"

info "Producing seed messages..."
bash "$KAFKA_DIR/seed-messages.sh"

# ── 6. Summary ───────────────────────────────────────────────────────────

info "============================================"
info "Dummy data reset complete!"
info ""
info "PostgreSQL (${PG_DB}):"
info "  11 schemas, 17 tables, ~600 rows"
info ""
info "Kafka:"
info "  3 topics, ~45 messages"
info ""
info "Verify with:"
info "  psql -h localhost -p ${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT} -U ${PG_USER} -d ${PG_DB}"
info "============================================"
