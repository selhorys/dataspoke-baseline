# Job state management for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, git available.

STATE_DIR="${PRAUTO_DIR}/state"
LOCK_FILE="${STATE_DIR}/heartbeat.lock"
SESSIONS_DIR="${STATE_DIR}/sessions"
# Persistent, local-only anchors for agent-native sessions. Unlike SESSIONS_DIR,
# this directory survives reset_ephemeral_state so a later heartbeat can verify
# a GitHub pause marker before asking Codex to resume it.
NATIVE_SESSIONS_DIR="${STATE_DIR}/native-sessions"

# Current issue session directory (set by init_issue_session).
CUR_SESSION_DIR=""

# Ensure the state/worktree directories exist.
ensure_state_dirs() {
  mkdir -p "$STATE_DIR" "$SESSIONS_DIR" "$NATIVE_SESSIONS_DIR" "${PRAUTO_DIR}/worktrees"
  chmod 700 "$NATIVE_SESSIONS_DIR" 2>/dev/null || true
}

# is_strict_uuid <value>
# Codex accepts either a UUID or a thread name. PRauto persists and resumes only
# canonical UUIDs so a GitHub comment cannot turn a session field into a name or
# an option-like argument.
is_strict_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

# native_session_anchor_file <issue_number>
native_session_anchor_file() {
  local issue_number="$1"
  [[ "$issue_number" =~ ^[0-9]+$ ]] || return 1
  printf '%s/issue-%s.json' "$NATIVE_SESSIONS_DIR" "$issue_number"
}

# record_codex_native_session <issue> <ready_timestamp> <thread_id>
# Persist the exact local proof only after Codex emitted thread.started. The
# ready timestamp binds a reused issue number to its current GitHub lifecycle.
record_codex_native_session() {
  local issue_number="$1" ready_timestamp="$2" thread_id="$3"
  [[ -n "$ready_timestamp" ]] || return 1
  is_strict_uuid "$thread_id" || return 1
  local anchor_file tmp_file
  anchor_file=$(native_session_anchor_file "$issue_number") || return 1
  mkdir -p "$NATIVE_SESSIONS_DIR"
  chmod 700 "$NATIVE_SESSIONS_DIR" 2>/dev/null || true
  tmp_file=$(mktemp "${anchor_file}.tmp.XXXXXX") || return 1
  if ! jq -n \
      --arg issue_number "$issue_number" \
      --arg ready_timestamp "$ready_timestamp" \
      --arg agent "codex" \
      --arg thread_id "$thread_id" \
      '{issue_number: $issue_number, ready_timestamp: $ready_timestamp, agent: $agent, thread_id: $thread_id}' \
      > "$tmp_file"; then
    rm -f "$tmp_file"
    return 1
  fi
  chmod 600 "$tmp_file" 2>/dev/null || true
  mv -f "$tmp_file" "$anchor_file"
}

# codex_native_session_anchor_matches <issue> <ready_timestamp> <thread_id>
# Validate a local anchor exactly. GitHub remains phase SSOT; this proves only
# that the session id in its marker was created by this executor in this issue's
# current lifecycle.
codex_native_session_anchor_matches() {
  local issue_number="$1" ready_timestamp="$2" thread_id="$3"
  [[ -n "$ready_timestamp" ]] || return 1
  is_strict_uuid "$thread_id" || return 1
  local anchor_file
  anchor_file=$(native_session_anchor_file "$issue_number") || return 1
  [[ -f "$anchor_file" ]] || return 1
  jq -e \
    --arg issue_number "$issue_number" \
    --arg ready_timestamp "$ready_timestamp" \
    --arg thread_id "$thread_id" '
      .issue_number == $issue_number
      and .ready_timestamp == $ready_timestamp
      and .agent == "codex"
      and .thread_id == $thread_id
    ' "$anchor_file" >/dev/null 2>&1
}

# Initialize a per-issue session directory.
# Creates .prauto/state/sessions/issue-<n>/<ts>-<uuid8>/ and sets CUR_SESSION_DIR.
# Usage: init_issue_session <issue_number>
init_issue_session() {
  local issue_number="$1"
  local ts uuid
  ts=$(date -u '+%Y%m%d-%H%M%S')
  uuid=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s-$$)
  uuid=$(printf '%s' "$uuid" | tr '[:upper:]' '[:lower:]')
  local session_id="${ts}-${uuid:0:8}"
  CUR_SESSION_DIR="${SESSIONS_DIR}/issue-${issue_number}/${session_id}"
  mkdir -p "$CUR_SESSION_DIR"
  info "Session dir: ${CUR_SESSION_DIR}"
}

# Acquire the PID lock. Returns 0 on success, 1 if another heartbeat holds it.
#
# The lock is the concurrency gate for the whole wake: a second heartbeat must
# not run a worker against a worktree another one is mid-flight on. The lock
# holds the holder's PID; `kill -0` distinguishes a live holder from a stale
# lock left by a crashed run (which is removed and re-acquired).
acquire_lock() {
  ensure_state_dirs

  if [[ -f "$LOCK_FILE" ]]; then
    local existing_pid
    existing_pid=$(cat "$LOCK_FILE" 2>/dev/null || printf '')
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      warn "Another heartbeat is running (PID $existing_pid). Exiting."
      return 1
    fi
    warn "Removing stale lock file (PID $existing_pid no longer running)."
    rm -f "$LOCK_FILE"
  fi

  printf '%s' "$$" > "$LOCK_FILE"
  return 0
}

# Release the lock. Safe to call when it is already absent.
release_lock() {
  if [[ -f "$LOCK_FILE" ]]; then
    local lock_pid
    lock_pid=$(cat "$LOCK_FILE" 2>/dev/null || printf '')
    [[ "$lock_pid" == "$$" ]] && rm -f "$LOCK_FILE"
  fi
}

# Reset ephemeral state at wake start. GitHub is the single source of truth, so
# each wake begins from a clean local slate; any work a prior (crashed) run left
# uncommitted in a worktree is invisible to resume and must not leak into the
# next run's fresh checkout. This sweeps orphaned worktrees so a dead worker
# cannot leave a dirty tree that the next tick would re-edit.
reset_ephemeral_state() {
  # Remove the rendered system prompt (regenerated each invocation).
  rm -f "${STATE_DIR}/.system-append-rendered.md"

  if [[ -d "${PRAUTO_DIR}/worktrees" ]]; then
    local wt
    for wt in "${PRAUTO_DIR}/worktrees"/*/; do
      [[ -d "$wt" ]] || continue
      warn "Removing orphaned worktree: $wt"
      git -C "$REPO_DIR" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
    done
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
  fi
}

# Abandon a job after max retries or a workflow escalation.
# Writes an abandon record, swaps labels (wip/plan-review -> failed), posts an
# idempotent "Abandoning" comment.
# Usage: abandon_job_github <issue_number> <retry_count> [reason]
abandon_job_github() {
  local issue_number="$1" retry_count="$2" reason="${3:-}"

  local history_file
  if [[ -n "$CUR_SESSION_DIR" ]] && [[ -d "$CUR_SESSION_DIR" ]]; then
    history_file="${CUR_SESSION_DIR}/abandon.json"
  else
    history_file="${SESSIONS_DIR}/$(date +%Y%m%d)_abandon_I-${issue_number}.json"
  fi
  jq -n \
    --argjson issue_number "$issue_number" \
    --argjson retry_count "$retry_count" \
    --arg abandoned_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg reason "$reason" \
    '{issue_number: $issue_number, abandoned_at: $abandoned_at, retry_count: $retry_count, reason: $reason}' \
    > "$history_file"
  info "Job for issue #${issue_number} abandoned -> ${history_file}"

  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --add-label "$PRAUTO_GITHUB_LABEL_FAILED" 2>/dev/null \
    || warn "Failed to update labels on issue #${issue_number}"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || true

  if ! comment_exists "issue" "$issue_number" "Abandoning"; then
    local body="prauto(${PRAUTO_WORKER_ID}): Abandoning after ${retry_count} retries. Manual intervention needed."
    [[ -n "$reason" ]] && body="${body}
${reason}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" --body "$body" 2>/dev/null \
      || warn "Failed to post abandonment comment on issue #${issue_number}"
  fi
}

# Record job completion to history.
# Usage: complete_job <issue_number>
complete_job() {
  local issue_number="$1"
  local history_file
  if [[ -n "$CUR_SESSION_DIR" ]] && [[ -d "$CUR_SESSION_DIR" ]]; then
    history_file="${CUR_SESSION_DIR}/complete.json"
  else
    history_file="${SESSIONS_DIR}/$(date +%Y%m%d)_complete_I-${issue_number}.json"
  fi
  jq -n \
    --argjson issue_number "$issue_number" \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issue_number: $issue_number, completed_at: $completed_at}' \
    > "$history_file"
  info "Job for issue #${issue_number} completed -> ${history_file}"
}
