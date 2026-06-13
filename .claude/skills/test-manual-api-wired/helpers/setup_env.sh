#!/usr/bin/env bash
# Bootstrap manual-test env into /tmp/_manual_test_env.
# Sources helm-charts/.env, acquires an admin JWT, writes BASE/tokens/PG creds.
# Re-run idempotently to refresh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/helm-charts/.env"
OUT="/tmp/_manual_test_env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run /k8s-deploy configure first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

BASE="http://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}"

# Ensure the bootstrap admin user exists (reset-seed wipes it) — mirrors
# tests/integration/api_wired/conftest.py require_server. Best-effort: token
# login below surfaces real failures.
if [[ -n "${DATASPOKE_TEST_INTERNAL_TOKEN:-}" ]]; then
  curl -sS -o /dev/null -X POST "${BASE}/internal/admin/bootstrap" \
    -H "X-Internal-Token: ${DATASPOKE_TEST_INTERNAL_TOKEN}" \
    -H "Content-Type: application/json" -d '{}' || true
fi

ADMIN_TOKEN=$(curl -sS -X POST "${BASE}/api/v1/auth/token" \
  -H "Content-Type: application/json" \
  -d '{"email":"dataspoke@dataspoke.local","password":"dataspoke"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

if [[ -z "${ADMIN_TOKEN}" ]]; then
  echo "ERROR: failed to obtain admin JWT" >&2
  exit 1
fi

cat > "$OUT" <<EOF
BASE=${BASE}
ADMIN_TOKEN=${ADMIN_TOKEN}
GMS=${DATASPOKE_TEST_DATAHUB_GMS_URL:-}
GMS_TOKEN=${DATASPOKE_TEST_DATAHUB_TOKEN:-}
INTERNAL_TOKEN=${DATASPOKE_TEST_INTERNAL_TOKEN:-}
PG_HOST=${DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST:-dataspoke-example-postgresql}
PG_PORT=${DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT:-9102}
PG_DB=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB:-example_db}
PG_USER=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER:-postgres}
PG_PASSWORD=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-}
DATASPOKE_PG_HOST=${DATASPOKE_TEST_POSTGRES_HOST:-}
DATASPOKE_PG_PORT=${DATASPOKE_TEST_POSTGRES_PORT:-}
DATASPOKE_PG_USER=${DATASPOKE_TEST_POSTGRES_USER:-}
DATASPOKE_PG_PASSWORD=${DATASPOKE_TEST_POSTGRES_PASSWORD:-}
DATASPOKE_PG_DB=${DATASPOKE_TEST_POSTGRES_DB:-}
DATASPOKE_K8S_NAMESPACE=${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-dataspoke-01}
EOF
chmod 600 "$OUT"

echo "Wrote ${OUT}"
echo "  BASE=${BASE}"
echo "  ADMIN_TOKEN=${ADMIN_TOKEN:0:24}…(len=${#ADMIN_TOKEN})"
echo "  GMS=${DATASPOKE_TEST_DATAHUB_GMS_URL:-<unset>}"
echo "  PG (source)  = ${DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST:-?}:${DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT:-?}"
echo "  PG (dataspoke)= ${DATASPOKE_TEST_POSTGRES_HOST:-?}:${DATASPOKE_TEST_POSTGRES_PORT:-?}"
echo "  k8s ns       = ${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-dataspoke-01}"
