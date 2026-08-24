# Agent quota probing and the pause/resume notification protocol for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, config loaded, agent CLIs available.
#
# Two distinct quota events share this module:
#   1. Pre-flight (check_quota) — no worker started yet. Posts a pause marker
#      with NO session id (nothing to resume) and exits the wake.
#   2. Mid-run (post_quota_paused_comment with a session id) — a dispatched
#      worker died on a rate/session-limit exit. Posts a pause marker carrying
#      the session id so a later wake can resume the SAME session.

# run_with_timeout <seconds> <command> [args...]
# Run a command, killing it after <seconds>. Returns the command's exit code,
# or 124 on timeout. macOS-compatible — no GNU coreutils `timeout` assumed.
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
  # A process our timer killed reports 137 (SIGKILL) or 143 (SIGTERM); normalize
  # both to 124, the convention GNU timeout uses.
  if [[ "$exit_code" -eq 137 ]] || [[ "$exit_code" -eq 143 ]]; then
    return 124
  fi
  return "$exit_code"
}

# check_quota [agent]
# Probe whether an agent's quota is available. Returns 0 if available, 1 if
# exhausted/auth-invalid. A dry-run TIMEOUT is not exhaustion — returns 0.
#
# `agent` is `claude` (default) or `codex`. The probe must invoke the CLI itself:
# both agents are login-based OAuth, so there is no token to inspect out-of-band.
check_quota() {
  local agent="${1:-claude}"
  local quota_timeout="${PRAUTO_QUOTA_TIMEOUT:-45}"

  if [[ "$agent" == "claude" ]]; then
    claude auth status >/dev/null 2>&1 || { warn "Claude auth check failed."; return 1; }
    local stderr_file="${STATE_DIR}/.quota-check-$$.stderr"
    if run_with_timeout "$quota_timeout" \
        claude -p "Reply with exactly: OK" \
          --output-format json --max-turns 1 --max-budget-usd 0.01 --allowedTools "" \
          2>"$stderr_file" >/dev/null; then
      rm -f "$stderr_file"; return 0
    fi
    local code=$?; local stderr; stderr=$(cat "$stderr_file" 2>/dev/null || printf ''); rm -f "$stderr_file"
    if [[ "$code" -eq 124 ]]; then
      warn "Claude dry-run timed out after ${quota_timeout}s — proceeding anyway."
      return 0
    elif printf '%s' "$stderr" | grep -qi "rate limit\|quota\|session limit"; then
      warn "Claude quota exhausted or rate-limited."
    else
      warn "Claude dry-run failed (exit ${code}): $(printf '%s' "$stderr" | head -c 200)"
    fi
    return 1
  fi

  # codex
  [[ -f ~/.codex/auth.json ]] || { warn "Codex auth.json missing."; return 1; }
  local stderr_file="${STATE_DIR}/.quota-check-$$.stderr"
  if run_with_timeout "$quota_timeout" \
      codex exec "Reply with exactly: OK" 2>"$stderr_file" >/dev/null; then
    rm -f "$stderr_file"; return 0
  fi
  local code=$?; local stderr; stderr=$(cat "$stderr_file" 2>/dev/null || printf ''); rm -f "$stderr_file"
  if [[ "$code" -eq 124 ]]; then
    warn "Codex dry-run timed out after ${quota_timeout}s — proceeding anyway."
    return 0
  elif printf '%s' "$stderr" | grep -qi "rate limit\|quota\|session limit"; then
    warn "Codex quota exhausted or rate-limited."
  else
    warn "Codex dry-run failed (exit ${code})."
  fi
  return 1
}

# pause_marker_body <agent> [session_id]
# Render the pause comment body. The `prauto:quota-paused` / `prauto:agent=` /
# `prauto:session=` lines are the machine-readable state the next wake parses;
# the prose is the human-facing instruction. They are plain text, not HTML
# comments — they are not secret (comments require write access, and the session
# id is already in the local state dir), so hiding them buys nothing while
# costing parse robustness across email digests and mobile clients.
pause_marker_body() {
  local agent="$1" session_id="${2:-}"
  local body="prauto(${PRAUTO_WORKER_ID}): Paused — ${agent} quota exhausted. Will resume automatically on the next quota window.

To abandon this session and restart from scratch instead of resuming, comment \"abandon previous session\".

prauto:quota-paused
prauto:agent=${agent}"
  [[ -n "$session_id" ]] && body="${body}
prauto:session=${session_id}"
  printf '%s' "$body"
}

# has_quota_paused_comment <issue_number>
# Returns 0 if the LATEST prauto pause/resume/restart marker on the issue is a
# PAUSE (i.e. no "Resumed" or "Restarting" has been posted after the most recent
# pause). Scoped to the current lifecycle via READY_LABEL_TIMESTAMP.
has_quota_paused_comment() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}):"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local latest
  latest=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" --json comments --jq '.comments' 2>/dev/null \
    | jq -r --arg prefix "$prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts)
            | select(.body | startswith($prefix))
            | select(.body | test("prauto:quota-paused|Resumed|Restarting"))]
      | last | .body // ""
    ') || return 1
  printf '%s' "$latest" | grep -q 'prauto:quota-paused'
}

# read_pause_marker <issue_number>
# Extract the latest pause marker's agent and (optional) session id.
# Sets: PAUSED_AGENT, PAUSED_SESSION_ID (may be empty). Returns 1 if no marker.
read_pause_marker() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}):"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local body
  body=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" --json comments --jq '.comments' 2>/dev/null \
    | jq -r --arg prefix "$prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts)
            | select(.body | startswith($prefix))
            | select(.body | contains("prauto:quota-paused"))]
      | last | .body // ""
    ') || return 1
  [[ -n "$body" ]] || return 1
  PAUSED_AGENT=$(printf '%s' "$body" | sed -n 's/^prauto:agent=\([a-z]*\)$/\1/p' | head -1)
  PAUSED_SESSION_ID=$(printf '%s' "$body" | sed -n 's/^prauto:session=\([^ ]*\)$/\1/p' | head -1)
  [[ -n "$PAUSED_AGENT" ]] || PAUSED_AGENT="claude"
  return 0
}

# has_abandon_override <issue_number>
# Returns 0 if a NON-prauto comment reading "abandon previous session" (trimmed,
# case-insensitive) appears AFTER the latest pause marker. This is the human
# escape hatch from the resume path: it forces a restart, not a resume.
has_abandon_override() {
  local issue_number="$1"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" --json comments --jq '.comments' 2>/dev/null \
    | jq -r --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts)] as $scoped
      | ($scoped | [.[] | select(.body | contains("prauto:quota-paused"))] | last | .createdAt // "") as $pause_ts
      | ($pause_ts | length > 0) as $has_pause
      | [ $scoped[] | select(($pause_ts == "" or .createdAt > $pause_ts))
                    | select(.body | startswith("prauto(") | not)
                    | select((.body | gsub("^\\s+|\\s+$"; "") | ascii_downcase) == "abandon previous session") ]
      | (if $has_pause then length > 0 else false end)
    ' | grep -q 'true'
}

# post_quota_paused_comment <issue_number> [agent] [session_id]
# Idempotent: skips if the latest marker is already a pause for this agent.
post_quota_paused_comment() {
  local issue_number="$1" agent="${2:-claude}" session_id="${3:-}"
  if has_quota_paused_comment "$issue_number"; then
    info "Quota-pause marker already present on issue #${issue_number}. Skipping."
    return 0
  fi
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "$(pause_marker_body "$agent" "$session_id")" 2>/dev/null \
    || warn "Failed to post quota-pause comment on issue #${issue_number}."
  info "Quota-pause marker posted on issue #${issue_number} (agent=${agent}, session=${session_id:-none})."
}

# post_quota_resumed_comment <issue_number>
# No idempotency guard — gated externally by has_quota_paused_comment.
post_quota_resumed_comment() {
  local issue_number="$1"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Resumed — ${PAUSED_AGENT:-agent} quota is now available. Continuing work." 2>/dev/null \
    || warn "Failed to post quota-resumed comment on issue #${issue_number}."
  info "Quota-resumed comment posted on issue #${issue_number}."
}

# post_restart_comment <issue_number> <agent>
# Posted when the human abandoned the previous session: the next wake restarts
# from scratch, re-selecting the agent (under `auto`, codex can take over if the
# paused agent's quota has not reset).
post_restart_comment() {
  local issue_number="$1" agent="$2"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Restarting — previous session abandoned per instruction. Fresh ${agent} session." 2>/dev/null \
    || warn "Failed to post restart comment on issue #${issue_number}."
  info "Restart marker posted on issue #${issue_number} (agent=${agent})."
}
