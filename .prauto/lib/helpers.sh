# Shared shell helpers for .prauto scripts.
# Source this file — do not execute directly.
# Usage: source "${PRAUTO_DIR}/lib/helpers.sh"

# Logging uses printf, never echo -e. `info`/`warn`/`error` take the message as
# a printf ARGUMENT (%s), so a `%` or a literal backslash in a probed value
# (an issue title, a claude error line, a reviewer finding) is printed verbatim
# and can never be expanded into a terminal escape or a format directive.
# This mirrors the discipline in helm-charts/bin/lib/helpers.sh — remote text
# enters these functions, and echo -e would reinterpret its escapes.
info()  { printf '\033[0;32m[INFO]\033[0m  %s\n' "$*"; }
warn()  { printf '\033[0;33m[WARN]\033[0m  %s\n' "$*"; }

# error <msg> — print a red [ERROR] line on stderr and end the run with status 1.
error() { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# error_no_exit <msg> — error()'s voice, but `return 1` instead of `exit 1`,
# for a helper that must be able to fail ONE item and hand the stop decision
# back to its caller (e.g. a resolver inside $( ... )).
error_no_exit() { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; return 1; }

# verify a command exists or abort.
ensure_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || error "'$cmd' is not installed or not in PATH."
}

# Load config.env (committed repo defaults) then config.local.env (gitignored
# instance identity + secrets). config.local.env overrides config.env; both are
# plain `KEY=value` files with no `export` prefix, so this is plain sourcing.
# Usage: load_config "$PRAUTO_DIR"
load_config() {
  local prauto_dir="$1"
  [[ -f "$prauto_dir/config.env" ]] || error "config.env not found at $prauto_dir/config.env"
  # shellcheck source=../config.env
  source "$prauto_dir/config.env"
  [[ -f "$prauto_dir/config.local.env" ]] || error \
    "config.local.env not found at $prauto_dir/config.local.env — copy config.local.env.example and edit it."
  # shellcheck source=../config.local.env
  source "$prauto_dir/config.local.env"
}

# labels_contain <json_array_string> <value>
# Returns 0 if the JSON array (of strings) contains the value, 1 otherwise.
labels_contain() {
  printf '%s' "$1" | jq -e --arg v "$2" 'index($v) != null' >/dev/null 2>&1
}

# comment_exists <"issue"|"pr"> <number> <keyword>
# Idempotency guard: returns 0 if a prauto comment starting with
# "prauto(<worker>): <keyword>" already exists on the target.
#
# For issues, the scan is scoped to the current lifecycle: only comments posted
# after READY_LABEL_TIMESTAMP (the last prauto:ready label event) are considered,
# so stale comments from a prior attempt are ignored. PR comments are not
# lifecycle-scoped (a PR has no ready-label anchor).
comment_exists() {
  local target_type="$1" target_number="$2" keyword="$3"
  local prefix="prauto(${PRAUTO_WORKER_ID}): ${keyword}"

  if [[ "$target_type" == "issue" ]] && [[ -n "${READY_LABEL_TIMESTAMP:-}" ]]; then
    gh issue view "$target_number" -R "$PRAUTO_GITHUB_REPO" --json comments --jq '.comments' 2>/dev/null \
      | jq -r --arg prefix "$prefix" --arg ready_ts "$READY_LABEL_TIMESTAMP" \
        '[.[] | select(.createdAt > $ready_ts) | select(.body | startswith($prefix))] | length > 0' \
      | grep -q 'true'
  else
    gh "${target_type}" view "$target_number" -R "$PRAUTO_GITHUB_REPO" --json comments \
      --jq ".comments[] | select(.body | startswith(\"${prefix}\")) | .id" \
      | head -1 | grep -q .
  fi
}

