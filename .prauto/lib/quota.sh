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

# codex_jsonl_has_quota_signal <jsonl_file>
# A Codex quota pause is allowed only for explicit protocol fields/codes, never
# for arbitrary agent text or a human-readable diagnostic. These are the
# installed CLI's usage-limit codes plus structured rate-limit variants.
codex_jsonl_has_quota_signal() {
  local output_file="$1"
  jq -se '
    def terminal_failure:
      .type == "error"
      or .type == "turn.failed"
      or .payload.type == "error"
      or .payload.type == "turn.failed";
    def quota_code:
      . == "usage_limit_exceeded"
      or . == "workspace_owner_usage_limit_reached"
      or . == "workspace_member_usage_limit_reached"
      or . == "rate_limit_exceeded"
      or . == "rate_limited"
      or . == "rate_limit";
    any(.[];
      type == "object" and terminal_failure and
      ([
          .error.code, .error.type, .error.kind, .error.reason,
          .error.data.code, .error.data.type, .error.data.kind, .error.data.reason,
          .payload.error.code, .payload.error.type, .payload.error.kind, .payload.error.reason,
          .payload.error.data.code, .payload.error.data.type,
          .payload.error.data.kind, .payload.error.data.reason,
          .rate_limit.code, .rate_limit.type, .rate_limit.reason,
          .payload.rate_limit.code, .payload.rate_limit.type, .payload.rate_limit.reason
        ]
        | any(.[]?; type == "string" and quota_code))
    ) | select(.)
  ' "$output_file" >/dev/null 2>&1
}

# claude_result_is_error <json_file>
# Claude Code emits auth/api/budget failures as a `--output-format json` result
# object with `is_error:true` (and often `terminal_reason:"api_error"`) while
# still exiting 0. The process exit code is therefore not a success signal; this
# is the authoritative check, used by both the availability probe and mid-run
# classification.
claude_result_is_error() {
  local f="$1"
  jq -e '(.is_error // false) == true or ((.terminal_reason // "") | test("^(api_)?error"))' \
    "$f" >/dev/null 2>&1
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
    local out_file="${STATE_DIR}/.quota-check-$$.out"
    local stderr_file="${STATE_DIR}/.quota-check-$$.stderr"
    # Budget must clear the project's system-prompt cache-creation cost (~$0.08)
    # or the probe always reports budget_exhausted; 0.25 leaves headroom.
    if run_with_timeout "$quota_timeout" \
        claude -p "Reply with exactly: OK" \
          --output-format json --max-turns 1 --max-budget-usd 0.25 --allowedTools "" \
          >"$out_file" 2>"$stderr_file"; then
      # Claude emits auth/api/budget failures as an is_error result object while
      # still exiting 0, so the exit code is not a success signal. Inspect the
      # JSON before declaring the agent available.
      if claude_result_is_error "$out_file"; then
        warn "Claude dry-run reported an error: $(jq -r '.result // .terminal_reason // "unknown"' "$out_file" 2>/dev/null | head -c 200)"
        rm -f "$out_file" "$stderr_file"; return 1
      fi
      rm -f "$out_file" "$stderr_file"; return 0
    fi
    local code=$?; local stderr; stderr=$(cat "$stderr_file" 2>/dev/null || printf ''); rm -f "$out_file" "$stderr_file"
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
  # Codex reports structured failures on its JSONL stream. Capture both streams
  # so a rate-limit event remains distinguishable from an ordinary auth/error.
  local output_file="${STATE_DIR}/.quota-check-$$.jsonl"
  local stderr_file="${output_file}.stderr"
  if run_with_timeout "$quota_timeout" \
      codex exec --json --sandbox workspace-write "Reply with exactly: OK" \
        >"$output_file" 2>"$stderr_file"; then
    if codex_jsonl_has_quota_signal "$output_file"; then
      rm -f "$output_file" "$stderr_file"
      warn "Codex quota exhausted or rate-limited."
      return 1
    fi
    rm -f "$output_file" "$stderr_file"; return 0
  fi
  local code=$? quota_signaled=0 details
  codex_jsonl_has_quota_signal "$output_file" && quota_signaled=1
  details=$(cat "$output_file" 2>/dev/null || printf '')
  [[ -z "$details" ]] && details=$(cat "$stderr_file" 2>/dev/null || printf '')
  rm -f "$output_file" "$stderr_file"
  if [[ "$code" -eq 124 ]]; then
    warn "Codex dry-run timed out after ${quota_timeout}s — proceeding anyway."
    return 0
  elif [[ "$quota_signaled" -eq 1 ]]; then
    warn "Codex quota exhausted or rate-limited."
  else
    warn "Codex dry-run failed (exit ${code}): $(printf '%s' "$details" | head -c 200)"
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
# Extract the latest pause marker's agent, optional session id, and author.
# Sets: PAUSED_AGENT, PAUSED_SESSION_ID (may be empty), PAUSED_MARKER_AUTHOR.
# The author is retained because GitHub comments are untrusted input until a
# Codex resume anchor validates it against the worker's authenticated actor.
# Returns 1 if no marker.
read_pause_marker() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}):"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local marker body
  marker=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" --json comments --jq '.comments' 2>/dev/null \
    | jq -r --arg prefix "$prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts)
            | select(.body | startswith($prefix))
            | select(.body | contains("prauto:quota-paused"))]
      | last // empty
    ') || return 1
  [[ -n "$marker" ]] || return 1
  body=$(printf '%s' "$marker" | jq -r '.body // ""')
  [[ -n "$body" ]] || return 1
  PAUSED_MARKER_AUTHOR=$(printf '%s' "$marker" | jq -r '.author.login // ""')
  PAUSED_AGENT=$(printf '%s' "$body" | sed -n 's/^prauto:agent=\([a-z]*\)$/\1/p' | head -1)
  PAUSED_SESSION_ID=$(printf '%s' "$body" | sed -n 's/^prauto:session=\([^ ]*\)$/\1/p' | head -1)
  [[ -n "$PAUSED_AGENT" ]] || PAUSED_AGENT="claude"
  return 0
}

# codex_pause_marker_is_trusted <issue_number>
# Require all three independently held facts before Codex resume: a comment by
# this worker account, a strict UUID (not a Codex thread name), and a matching
# local native-session anchor for the current ready-label lifecycle.
codex_pause_marker_is_trusted() {
  local issue_number="$1"
  [[ "${PAUSED_AGENT:-}" == "codex" ]] || return 1
  [[ -n "${PRAUTO_GITHUB_ACTOR:-}" ]] || return 1
  [[ "${PAUSED_MARKER_AUTHOR:-}" == "$PRAUTO_GITHUB_ACTOR" ]] || return 1
  is_strict_uuid "${PAUSED_SESSION_ID:-}" || return 1
  codex_native_session_anchor_matches "$issue_number" "${READY_LABEL_TIMESTAMP:-}" "$PAUSED_SESSION_ID"
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

# post_untrusted_resume_restart_comment <issue_number> <agent>
# A forged/stale Codex pause marker cannot be deleted from GitHub. Post a later
# lifecycle marker so has_quota_paused_comment becomes false and the normal
# retry path starts a fresh agent instead of ever resuming the supplied id.
post_untrusted_resume_restart_comment() {
  local issue_number="$1" agent="$2"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Restarting — the previous Codex pause marker did not match a trusted local native-session anchor. Fresh ${agent} session." 2>/dev/null \
    || warn "Failed to post untrusted-resume restart marker on issue #${issue_number}."
  info "Untrusted Codex resume marker bypassed on issue #${issue_number}."
}

# post_resume_failure_restart_comment <issue_number> <agent>
# An ordinary resume failure must also supersede its pause marker; otherwise the
# next heartbeat would retry the same failed resume without advancing retries.
post_resume_failure_restart_comment() {
  local issue_number="$1" agent="$2"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Restarting — the previous ${agent} resume failed without a recognized quota signal. Fresh ${agent} session on the next retry." 2>/dev/null \
    || warn "Failed to post resume-failure restart marker on issue #${issue_number}."
  info "Resume failure converted to ordinary retry on issue #${issue_number}."
}
