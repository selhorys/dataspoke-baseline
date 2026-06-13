#!/usr/bin/env bash
# Re-issue admin JWT and replace in /tmp/_manual_test_env. Idempotent.
set -euo pipefail

ENV_FILE="/tmp/_manual_test_env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run setup_env.sh first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

NEW_TOKEN=$(curl -sS -X POST "${BASE}/api/v1/auth/token" \
  -H "Content-Type: application/json" \
  -d '{"email":"dataspoke@dataspoke.local","password":"dataspoke"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

if [[ -z "${NEW_TOKEN}" ]]; then
  echo "ERROR: failed to refresh admin JWT" >&2
  exit 1
fi

# Portable in-place edit (BSD/GNU sed) without leaving a .bak file lying around.
sed -i.bak "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=${NEW_TOKEN}|" "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

echo "Refreshed ADMIN_TOKEN (len=${#NEW_TOKEN})"
