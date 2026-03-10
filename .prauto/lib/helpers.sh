# Shared shell helpers for prauto scripts.
# Source this file — do not execute directly.
# Usage: source "${SCRIPT_DIR}/lib/helpers.sh"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
info()  { echo -e "\033[0;32m[$(_ts) - INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[$(_ts) - WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[$(_ts) - ERROR]\033[0m $*" >&2; exit 1; }

# Verify a command exists or exit with error.
ensure_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || error "'$cmd' is not installed or not in PATH."
}

# Load config.env and config.local.env from the prauto root.
# Usage: load_config "$PRAUTO_DIR"
load_config() {
  local prauto_dir="$1"

  if [[ ! -f "$prauto_dir/config.env" ]]; then
    error "config.env not found at $prauto_dir/config.env"
  fi
  # shellcheck source=../config.env
  source "$prauto_dir/config.env"

  if [[ ! -f "$prauto_dir/config.local.env" ]]; then
    error "config.local.env not found at $prauto_dir/config.local.env — copy config.local.env.example and edit it."
  fi
  # shellcheck source=../config.local.env
  source "$prauto_dir/config.local.env"
}

# Check if a JSON array of strings contains a specific value.
# Usage: labels_contain <json_array_string> <value>
# Returns 0 if found, 1 if not.
labels_contain() {
  echo "$1" | jq -e --arg v "$2" 'index($v) != null' >/dev/null 2>&1
}

# Check if a matching comment already exists (idempotency guard).
# For issue comments, respects READY_LABEL_TIMESTAMP — only considers comments
# posted after the last prauto:ready label event (ignores stale comments from
# previous lifecycles). PR comments are not filtered.
# Usage: comment_exists <"issue"|"pr"> <number> <keyword>
# Returns 0 if found, 1 if not found.
comment_exists() {
  local target_type="$1"
  local target_number="$2"
  local keyword="$3"
  local prefix="prauto(${PRAUTO_WORKER_ID}): ${keyword}"

  if [[ "$target_type" == "issue" ]] && [[ -n "${READY_LABEL_TIMESTAMP:-}" ]]; then
    gh issue view "$target_number" \
      -R "$PRAUTO_GITHUB_REPO" \
      --json comments \
      --jq '.comments' 2>/dev/null \
      | jq -r --arg prefix "$prefix" --arg ready_ts "$READY_LABEL_TIMESTAMP" '
        [.[] | select(.createdAt > $ready_ts) | select(.body | startswith($prefix))] | length > 0
      ' | grep -q 'true'
  else
    gh "${target_type}" view "$target_number" \
      -R "$PRAUTO_GITHUB_REPO" \
      --json comments \
      --jq ".comments[] | select(.body | startswith(\"${prefix}\")) | .id" \
    | head -1 | grep -q .
  fi
}
