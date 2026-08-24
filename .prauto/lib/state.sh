# Job state management for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, git available.

STATE_DIR="${PRAUTO_DIR}/state"
LOCK_FILE="${STATE_DIR}/heartbeat.lock"
SESSIONS_DIR="${STATE_DIR}/sessions"

# Current issue session directory (set by init_issue_session).
CUR_SESSION_DIR=""

# Ensure the state/worktree directories exist.
ensure_state_dirs() {
  mkdir -p "$STATE_DIR" "$SESSIONS_DIR" "${PRAUTO_DIR}/worktrees"
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
