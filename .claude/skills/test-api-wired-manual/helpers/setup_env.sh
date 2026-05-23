#!/usr/bin/env bash
# Bootstrap manual-test env into /tmp/_manual_test_env.
# Sources dev_env/.env, acquires an admin JWT, writes BASE/tokens/PG creds.
# Re-run idempotently to refresh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/dev_env/.env"
OUT="/tmp/_manual_test_env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run /dev-env configure first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

BASE="http://app.${DATASPOKE_DEV_INGRESS_DOMAIN}"

ADMIN_TOKEN=$(curl -sS -X POST "${BASE}/api/v1/auth/token" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin","password":"admin"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

if [[ -z "${ADMIN_TOKEN}" ]]; then
  echo "ERROR: failed to obtain admin JWT" >&2
  exit 1
fi

cat > "$OUT" <<EOF
BASE=${BASE}
ADMIN_TOKEN=${ADMIN_TOKEN}
GMS=${DATASPOKE_DATAHUB_GMS_URL:-}
GMS_TOKEN=${DATASPOKE_DATAHUB_TOKEN:-}
INTERNAL_TOKEN=${DATASPOKE_INTERNAL_TOKEN:-}
PG_HOST=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST:-dataspoke-example-postgresql}
PG_PORT=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PORT:-9102}
PG_DB=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB:-example_db}
PG_USER=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER:-postgres}
PG_PASSWORD=${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-}
DATASPOKE_PG_HOST=${DATASPOKE_POSTGRES_HOST:-}
DATASPOKE_PG_PORT=${DATASPOKE_POSTGRES_PORT:-}
DATASPOKE_PG_USER=${DATASPOKE_POSTGRES_USER:-}
DATASPOKE_PG_PASSWORD=${DATASPOKE_POSTGRES_PASSWORD:-}
DATASPOKE_PG_DB=${DATASPOKE_POSTGRES_DB:-}
DATASPOKE_K8S_NAMESPACE=${DATASPOKE_K8S_NAMESPACE:-dataspoke-01}
EOF
chmod 600 "$OUT"

echo "Wrote ${OUT}"
echo "  BASE=${BASE}"
echo "  ADMIN_TOKEN=${ADMIN_TOKEN:0:24}…(len=${#ADMIN_TOKEN})"
echo "  GMS=${DATASPOKE_DATAHUB_GMS_URL:-<unset>}"
echo "  PG (source)  = ${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST:-?}:${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PORT:-?}"
echo "  PG (dataspoke)= ${DATASPOKE_POSTGRES_HOST:-?}:${DATASPOKE_POSTGRES_PORT:-?}"
echo "  k8s ns       = ${DATASPOKE_K8S_NAMESPACE:-dataspoke-01}"
