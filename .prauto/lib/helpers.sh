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

# prauto_comment_prefix
# The marker every prauto comment starts with. comment_exists parses it to decide
# idempotency, so the posting side and the reading side must agree exactly —
# re-typing it at each call site is how they drift.
prauto_comment_prefix() {
  printf 'prauto(%s): ' "$PRAUTO_WORKER_ID"
}

# prauto_issue_comment <issue_number> <body> [failure_msg]
# Post a prauto comment on an issue, prefixed and best-effort.
#
# Best-effort by design: a comment is a report, and losing one must not fail the
# work it describes. The warning names what was lost so a silent drop is still
# visible in the log.
prauto_issue_comment() {
  local issue_number="$1" body="$2"
  local failure_msg="${3:-Failed to post a comment on issue #${issue_number}.}"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "$(prauto_comment_prefix)${body}" 2>/dev/null || warn "$failure_msg"
}

# prauto_pr_comment <pr_number> <body> [failure_msg]
# The PR-side twin of prauto_issue_comment. Same prefix, same best-effort
# contract; comment_exists matches both.
prauto_pr_comment() {
  local pr_number="$1" body="$2"
  local failure_msg="${3:-Failed to post a comment on PR #${pr_number}.}"
  gh pr comment "$pr_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "$(prauto_comment_prefix)${body}" 2>/dev/null || warn "$failure_msg"
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
  local prefix="$(prauto_comment_prefix)${keyword}"

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

# wait_for_pid_bounded <pid> <timeout_secs>
# Reap <pid>, giving up after <timeout_secs> rather than blocking forever.
#
# Bash's `wait` has no timeout, and a child stuck in uninterruptible sleep never
# returns from it. The heartbeat's EXIT trap runs after every other backstop has
# been torn down, so an unbounded wait there holds the heartbeat lock and the
# worktree indefinitely — the one failure this harness cannot recover from on a
# later wake. Returns 0 if the process was reaped, 1 on timeout.
wait_for_pid_bounded() {
  local pid="$1" timeout_secs="${2:-10}" waited=0
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  while kill -0 "$pid" 2>/dev/null; do
    if (( waited >= timeout_secs )); then
      warn "Gave up waiting for pid ${pid} after ${timeout_secs}s."
      return 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  wait "$pid" 2>/dev/null || true
  return 0
}
