# Token quota checking and notifications for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced (for comment_exists), claude CLI available, config loaded.

# Run a command with a timeout (macOS-compatible, no coreutils needed).
# Usage: run_with_timeout <seconds> <command> [args...]
# Returns the command's exit code, or 124 on timeout.
run_with_timeout() {
  local timeout_secs="$1"; shift
  "$@" &
  local cmd_pid=$!
  ( sleep "$timeout_secs" && kill "$cmd_pid" 2>/dev/null ) &
  local timer_pid=$!
  wait "$cmd_pid" 2>/dev/null
  local exit_code=$?
  kill "$timer_pid" 2>/dev/null
  wait "$timer_pid" 2>/dev/null || true
  # If killed by our timer, exit code is 137 (128+9); normalize to 124 like GNU timeout
  if [[ "$exit_code" -eq 137 ]] || [[ "$exit_code" -eq 143 ]]; then
    return 124
  fi
  return "$exit_code"
}

# Check whether Claude Code API tokens are available.
# Returns 0 if available, 1 if exhausted or auth invalid.
check_quota() {
  local quota_timeout="${PRAUTO_QUOTA_TIMEOUT:-45}"

  # Step 1: Auth validation (fast — just checks local credentials)
  if ! claude auth status >/dev/null 2>&1; then
    warn "Claude auth check failed — credentials may be invalid or expired."
    return 1
  fi

  # Step 2: Minimal dry-run with timeout
  # NOTE: claude -p stdout is invisible to the Bash tool's stdout capture,
  # so we discard it (>/dev/null) and rely solely on the exit code.
  local stderr_file
  stderr_file=$(mktemp)

  if run_with_timeout "$quota_timeout" \
    claude -p "Reply with exactly: OK" \
      --output-format json \
      --max-turns 1 \
      --max-budget-usd 0.01 \
      --allowedTools "" 2>"$stderr_file" >/dev/null; then
    rm -f "$stderr_file"
    return 0
  else
    local exit_code=$?
    local stderr_content
    stderr_content=$(cat "$stderr_file" 2>/dev/null || echo "")
    rm -f "$stderr_file"

    if [[ "$exit_code" -eq 124 ]]; then
      warn "Claude dry-run timed out after ${quota_timeout}s — proceeding anyway."
      return 0
    elif echo "$stderr_content" | grep -qi "rate limit\|quota"; then
      warn "Claude token quota exhausted or rate-limited."
    else
      warn "Claude dry-run failed (exit ${exit_code}): $stderr_content"
    fi
    return 1
  fi
}

# Check if the LATEST prauto comment on an issue has the quota-paused marker.
# Usage: has_quota_paused_comment <issue_number>
# Returns 0 if found, 1 if not found.
has_quota_paused_comment() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}):"

  local latest_body
  latest_body=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq "[.comments[] | select(.body | startswith(\"${prefix}\"))] | last | .body // \"\"" \
    2>/dev/null) || return 1

  echo "$latest_body" | grep -q '<!-- prauto:quota-paused -->'
}

# Post a quota-paused notification on an issue.
# Idempotent: skips if the LATEST prauto comment already has the marker.
# Usage: post_quota_paused_comment <issue_number>
post_quota_paused_comment() {
  local issue_number="$1"

  if has_quota_paused_comment "$issue_number"; then
    info "Quota-paused comment already present on issue #${issue_number}. Skipping."
    return 0
  fi

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Paused — Claude token quota exhausted. Will resume automatically when quota is available.

<!-- prauto:quota-paused -->" \
    2>/dev/null || warn "Failed to post quota-paused comment on issue #${issue_number}."

  info "Quota-paused comment posted on issue #${issue_number}."
}

# Post a quota-resumed notification on an issue.
# No idempotency guard — gated externally by has_quota_paused_comment.
# Usage: post_quota_resumed_comment <issue_number>
post_quota_resumed_comment() {
  local issue_number="$1"

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Resumed — Claude token quota is now available. Continuing work." \
    2>/dev/null || warn "Failed to post quota-resumed comment on issue #${issue_number}."

  info "Quota-resumed comment posted on issue #${issue_number}."
}
