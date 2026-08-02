#!/usr/bin/env bash
# Seed the built-in default admin user (dataspoke@dataspoke.local / dataspoke) via the
# internal bootstrap endpoint. Safe to re-run — the endpoint is idempotent:
# if any Admin already exists, it returns {created: false} and this script
# exits cleanly.
#
# Auth: the API pod's own DATASPOKE_INTERNAL_TOKEN, read from inside the pod
# by api_internal_request (bin/lib/helpers.sh) and sent as X-Internal-Token —
# never extracted to this machine.
# Endpoint: POST /internal/admin/bootstrap, reached over the API's own
# loopback port from inside its pod (no ingress, no DNS).
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

# ---------------------------------------------------------------------------
# Bootstrap default admin user
# ---------------------------------------------------------------------------
info "Calling POST /internal/admin/bootstrap to seed default admin user..."
RESPONSE="$(api_internal_request "${NS}" POST "/internal/admin/bootstrap" '{}')"
HTTP_CODE="$(printf '%s\n' "$RESPONSE" | head -n1)"
BODY="$(printf '%s\n' "$RESPONSE" | tail -n +2)"

# Parse error_code from body when present (used by the 503 branch). A raw
# FastAPI HTTPException(detail={"error_code": ..., ...}) serializes as a
# NESTED {"detail": {"error_code": ...}} envelope, not a top-level
# error_code key — check both shapes.
ERROR_CODE="$(printf '%s' "$BODY" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
detail = d.get('detail')
ec = d.get('error_code') or (detail.get('error_code', '') if isinstance(detail, dict) else '')
print(ec)
" 2>/dev/null || true)"

case "$HTTP_CODE" in
  200|201)
    CREATED="$(printf '%s' "$BODY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('created',''))" 2>/dev/null || true)"
    if [[ "$CREATED" == "True" || "$CREATED" == "true" ]]; then
      info "Seeded default admin user 'dataspoke@dataspoke.local'."
      warn "Default admin 'dataspoke@dataspoke.local / dataspoke' seeded. Rotate via PATCH /auth/me before production use."
    else
      info "Admin user already exists; skipping seed."
    fi
    ;;
  503)
    if [[ "$ERROR_CODE" == "INTERNAL_AUTH_NOT_CONFIGURED" ]]; then
      error "Bootstrap got HTTP 503 (error_code=INTERNAL_AUTH_NOT_CONFIGURED) from POST /internal/admin/bootstrap — the API pod's own DATASPOKE_INTERNAL_TOKEN is unset or blank. Check dataspoke-secrets and roll the dataspoke-api deployment."
    else
      error "Bootstrap got HTTP 503 (error_code=${ERROR_CODE:-unknown}) from POST /internal/admin/bootstrap. The bootstrap endpoint makes no external call, so any other 503 means the API's own storage (Postgres) is unavailable; fix that and re-run. Response body: ${BODY}"
    fi
    ;;
  000)
    error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
    ;;
  *)
    error "POST failed (HTTP ${HTTP_CODE}) from /internal/admin/bootstrap. Response body: ${BODY}"
    ;;
esac
