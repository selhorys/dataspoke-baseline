#!/usr/bin/env bash
# Pre-flight for test-manual-ui: health-check, bootstrap env+token, locate the
# reachable frontend, and print the app URL + login. Re-run idempotently.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/helm-charts/.env"
SIBLING="${REPO_ROOT}/.claude/skills/test-manual-api-wired/helpers"

# ── 1. Health-check ──────────────────────────────────────────────────────────
echo "── health-check ──────────────────────────────────────────────"
"${REPO_ROOT}/helm-charts/bin/health-check.sh"

# ── 2. Bootstrap env + admin JWT (reuses the api-wired helper) ───────────────
echo
echo "── env bootstrap (setup_env.sh) ─────────────────────────────"
bash "${SIBLING}/setup_env.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run /k8s-deploy configure first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"
LOCAL_URL="http://localhost:3000"
CLUSTER_URL="http://app.${DOMAIN}/"

# ── 3. Locate the reachable frontend ─────────────────────────────────────────
# --frontend local  → host `pnpm dev` at :3000 (talks to the in-cluster API)
# --frontend cluster → containerised UI at app.<INGRESS_IP>.nip.io
probe() { curl -sS -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null || echo 000; }

echo
echo "── frontend reachability ────────────────────────────────────"
LOCAL_CODE="$(probe "$LOCAL_URL")"
CLUSTER_CODE="$(probe "$CLUSTER_URL")"
echo "  local   ${LOCAL_URL}        → HTTP ${LOCAL_CODE}"
echo "  cluster ${CLUSTER_URL}  → HTTP ${CLUSTER_CODE}"

APP_URL=""
if [[ "$LOCAL_CODE" =~ ^(200|307|308|302)$ ]]; then
  APP_URL="$LOCAL_URL"
elif [[ "$CLUSTER_CODE" =~ ^(200|307|308|302)$ ]]; then
  APP_URL="$CLUSTER_URL"
fi

if [[ -z "$APP_URL" ]]; then
  cat >&2 <<EOF

ERROR: no reachable frontend.
  • --frontend local : start host dev server →  pnpm -C src/frontend dev
                       (needs src/frontend/.env.local; re-run install.sh
                        --profile dev --frontend local to write it)
  • --frontend cluster: build+deploy the UI →   ./helm-charts/bin/install.sh \\
                        --profile dev --components frontend
EOF
  exit 1
fi

# ── 4. Print the access summary ──────────────────────────────────────────────
cat <<EOF

── ready ────────────────────────────────────────────────────
  App URL : ${APP_URL}
  API base: http://api.${DOMAIN}
  Login   : dataspoke@dataspoke.local / dataspoke   (Admin)
  Env     : /tmp/_manual_test_env  (BASE, ADMIN_TOKEN, GMS, PG creds)

Log in at the App URL above, then return here to start Step 1.
EOF
