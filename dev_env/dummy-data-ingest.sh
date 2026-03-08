#!/usr/bin/env bash
# dummy-data-ingest.sh — Register example-postgres tables in DataHub.
#
# Usage:
#   cd dev_env && ./dummy-data-ingest.sh           # reset + ingest (default)
#   cd dev_env && ./dummy-data-ingest.sh --no-reset # ingest only (skip delete)
#   cd dev_env && ./dummy-data-ingest.sh --reset-only # delete only
#
# Prerequisites:
#   - dummy-data-reset.sh has been run (tables exist in example-postgres)
#   - Port-forwards active: dummy-data (9102), DataHub GMS (9004)
#   - uv sync has been run (acryl-datahub installed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/lib/helpers.sh"

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error ".env not found at $SCRIPT_DIR/.env — copy .env.example and edit it."
fi

# ── Verify port-forwards ──────────────────────────────────────────────────
_check_port() {
  if ! nc -z localhost "$1" 2>/dev/null; then
    error "Port $1 not reachable. Ensure port-forwards are active."
  fi
}

source "$SCRIPT_DIR/.env"
PG_PORT="${DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT:-9102}"
GMS_PORT="${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_GMS_PORT:-9004}"

_check_port "$PG_PORT"
_check_port "$GMS_PORT"

# ── Determine flags ───────────────────────────────────────────────────────
PYTHON_ARGS=("--reset")
if [[ "${1:-}" == "--no-reset" ]]; then
  PYTHON_ARGS=()
elif [[ "${1:-}" == "--reset-only" ]]; then
  PYTHON_ARGS=("--reset-only")
fi

# ── Run the Python ingestion script ───────────────────────────────────────
info "Running DataHub ingestion for example-postgres..."
cd "$REPO_ROOT"
uv run python dev_env/dummy-data/datahub/ingest.py "${PYTHON_ARGS[@]}"

info "============================================"
info "DataHub ingestion complete!"
info ""
info "Verify at: http://localhost:${GMS_PORT}"
info "  or: DataHub UI http://localhost:${DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_UI_PORT:-9002}"
info "============================================"
