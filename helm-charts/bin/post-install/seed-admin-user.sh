#!/usr/bin/env bash
# Seed the built-in default admin user (dataspoke@dataspoke.local / dataspoke) via the
# internal bootstrap endpoint. Safe to re-run — the endpoint is idempotent:
# if any Admin already exists, it returns {created: false} and this script
# exits cleanly.
#
# Auth: retrieves DATASPOKE_INTERNAL_TOKEN from the running API pod.
# Endpoint: <scheme>://api.<DOMAIN>/internal/admin/bootstrap (scheme per
# DATASPOKE_KUBE_INGRESS_SCHEME, default http)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$(cd "$BIN_DIR/.." && pwd)/.env.dev}"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example and edit it."
fi
source "$ENV_FILE"

NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"

if [[ -z "$DOMAIN" ]]; then
  error "DATASPOKE_KUBE_INGRESS_DOMAIN not set in .env — cannot reach the admin API."
fi
SCHEME="$(ingress_scheme)"

# ---------------------------------------------------------------------------
# Retrieve internal token from the running API pod
# ---------------------------------------------------------------------------
info "Retrieving DATASPOKE_INTERNAL_TOKEN from dataspoke-api pod..."
INTERNAL_TOKEN="$(kubectl exec -n "${NS}" deploy/dataspoke-api -c api -- \
  printenv DATASPOKE_INTERNAL_TOKEN 2>/dev/null || true)"

if [[ -z "$INTERNAL_TOKEN" ]]; then
  error "Could not read DATASPOKE_INTERNAL_TOKEN from dataspoke-api pod — is the API running?"
fi
info "Internal token retrieved."

# ---------------------------------------------------------------------------
# Bootstrap default admin user
# ---------------------------------------------------------------------------
info "Calling POST /internal/admin/bootstrap to seed default admin user..."
HTTP_CODE=$(curl -sS -o /tmp/seed-admin-resp.json -w "%{http_code}" -X POST \
  "${SCHEME}://api.${DOMAIN}/internal/admin/bootstrap" \
  -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{}' \
  || echo "000")

# Parse error_code from body when present (used by all non-2xx branches).
ERROR_CODE="$(python3 -c "import json; d=json.load(open('/tmp/seed-admin-resp.json')); print(d.get('error_code',''))" 2>/dev/null || true)"

case "$HTTP_CODE" in
  200|201)
    CREATED="$(python3 -c "import json,sys; d=json.load(open('/tmp/seed-admin-resp.json')); print(d.get('created',''))" 2>/dev/null || true)"
    if [[ "$CREATED" == "True" || "$CREATED" == "true" ]]; then
      info "Seeded default admin user 'dataspoke@dataspoke.local'."
      warn "Default admin 'dataspoke@dataspoke.local / dataspoke' seeded. Rotate via PATCH /auth/me before production use."
    else
      info "Admin user already exists; skipping seed."
    fi
    ;;
  503)
    error "Bootstrap got HTTP 503 (error_code=${ERROR_CODE:-unknown}): ${SCHEME}://api.${DOMAIN}/internal/admin/bootstrap — see /tmp/seed-admin-resp.json. The bootstrap endpoint makes no external call, so a 503 here means the API's own storage (Postgres) is unavailable; fix that and re-run."
    ;;
  401|403)
    error "Bootstrap rejected with HTTP ${HTTP_CODE} — X-Internal-Token mismatch. Re-check dataspoke-secrets and the API pod env."
    ;;
  000)
    error "Could not reach ${SCHEME}://api.${DOMAIN}/internal/admin/bootstrap — check ingress, DNS, and that the dataspoke-api pod is Ready."
    ;;
  *)
    error "POST failed (HTTP ${HTTP_CODE}): ${SCHEME}://api.${DOMAIN}/internal/admin/bootstrap — see /tmp/seed-admin-resp.json"
    ;;
esac
