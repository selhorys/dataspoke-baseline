#!/usr/bin/env bash
# Pre-flight for test-manual-ui: health-check, bootstrap env+token, locate the
# reachable frontend, and print the app URL + login. Re-run idempotently.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/helm-charts/.env.dev"
SIBLING="${REPO_ROOT}/.agents/skills/test-manual-api-wired/helpers"

# ── 1. Health-check ──────────────────────────────────────────────────────────
echo "── health-check ──────────────────────────────────────────────"
# Exit 2 is not a verdict on the cluster: the check could not be set up on this
# machine and probed nothing. Saying so here stops the operator from reinstalling
# components in response to a missing kubectl or a kubeconfig typo.
hc_rc=0
"${REPO_ROOT}/helm-charts/bin/health-check.sh" --keep-lock || hc_rc=$?
if (( hc_rc == 2 )); then
  echo "ERROR: health-check could not run (exit 2) — a LOCAL CONFIGURATION fault, not a sick cluster." >&2
  echo "       Nothing was probed. Usual causes: kubectl not on PATH, DATASPOKE_KUBE_CLUSTER unset in" >&2
  echo "       helm-charts/.env.dev, a context missing from your kubeconfig, or an unreadable env file." >&2
  exit 2
elif (( hc_rc != 0 )); then
  exit "$hc_rc"
fi

# ── 2. Bootstrap env + admin JWT (reuses the api-wired helper) ───────────────
echo
echo "── env bootstrap (setup_env.sh) ─────────────────────────────"
bash "${SIBLING}/setup_env.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy helm-charts/.env.dev.example and edit it, or run /k8s-deploy configure first." >&2
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
# --frontend cluster → containerised UI at app.<INGRESS_DOMAIN> (<IP>.nip.io managed / operator host shared)
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

# ── 4. Provision dataspoke-source-cred-dummy-data-pg (create-if-absent) ──────
# Required for UC1 Case 2 (ACTIVE_CUSTOM_MANAGED). Idempotent on re-run.
# spec: feature/SECRET_RESOLUTION.md §Reference-only model — out-of-band provisioning
NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
PG_PASS="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-}"
if [[ -n "$PG_PASS" ]] && command -v kubectl &>/dev/null; then
  echo
  echo "── source-cred secret (create-if-absent) ────────────────────"
  kubectl create secret generic dataspoke-source-cred-dummy-data-pg \
    --from-literal=password="${PG_PASS}" \
    -n "${NS}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "  dataspoke-source-cred-dummy-data-pg  ✓"
else
  echo
  echo "── source-cred secret ───────────────────────────────────────"
  echo "  SKIP: DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD unset or kubectl absent."
  echo "  UC1 Case 2 will skip if the secret is missing in-cluster."
fi

# ── 5. Print the access summary ──────────────────────────────────────────────
cat <<EOF

── ready ────────────────────────────────────────────────────
  App URL : ${APP_URL}
  API base: http://api.${DOMAIN}
  Login   : dataspoke@dataspoke.local / dataspoke   (Admin)
  Env     : /tmp/_manual_test_env  (BASE, ADMIN_TOKEN, GMS, PG creds)

Log in at the App URL above, then return here to start Step 1.
EOF
