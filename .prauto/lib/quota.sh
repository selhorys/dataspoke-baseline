# Token quota checking for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, claude CLI available.

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
