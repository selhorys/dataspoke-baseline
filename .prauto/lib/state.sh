# Job state management for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, jq available.

STATE_DIR="${PRAUTO_DIR}/state"
LOCK_FILE="${STATE_DIR}/heartbeat.lock"
HISTORY_DIR="${STATE_DIR}/history"
SESSIONS_DIR="${STATE_DIR}/sessions"

# Ensure state directories exist.
ensure_state_dirs() {
  mkdir -p "$STATE_DIR" "$HISTORY_DIR" "$SESSIONS_DIR" "${PRAUTO_DIR}/worktrees"
}

# Acquire PID-based lock. Returns 0 on success, 1 if already locked.
acquire_lock() {
  ensure_state_dirs

  if [[ -f "$LOCK_FILE" ]]; then
    local existing_pid
    existing_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      warn "Another heartbeat is running (PID $existing_pid). Exiting."
      return 1
    fi
    # Stale lock — previous process died
    warn "Removing stale lock file (PID $existing_pid no longer running)."
    rm -f "$LOCK_FILE"
  fi

  echo $$ > "$LOCK_FILE"
  return 0
}

# Release the lock.
release_lock() {
  rm -f "$LOCK_FILE"
}

# Reset ephemeral state at heartbeat startup (GitHub is SSOT for job state).
# Called after lock acquisition to ensure a clean local slate each run.
reset_ephemeral_state() {
  # Remove stale monitoring file (GitHub is SSOT for job state)
  rm -f "${STATE_DIR}/current-job.json"
  # Remove rendered prompt (regenerated each invocation)
  rm -f "${STATE_DIR}/.system-append-rendered.md"
  # Clean orphaned worktrees from crashed previous runs
  if [[ -d "${PRAUTO_DIR}/worktrees" ]]; then
    local wt
    for wt in "${PRAUTO_DIR}/worktrees"/*/; do
      [[ -d "$wt" ]] || continue
      git worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
    done
    git worktree prune 2>/dev/null || true
  fi
}

# Abandon a job after max retries.
# Writes abandon record to history, updates labels, posts comment.
# Usage: abandon_job_github <issue_number> <retry_count>
abandon_job_github() {
  local issue_number="$1"
  local retry_count="$2"

  # Write abandon record to history
  local date_prefix
  date_prefix=$(date +%Y%m%d)
  local history_file="${HISTORY_DIR}/${date_prefix}_I-${issue_number}.json"
  jq -n \
    --argjson issue_number "$issue_number" \
    --argjson retry_count "$retry_count" \
    --arg abandoned_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issue_number: $issue_number, abandoned_at: $abandoned_at, retry_count: $retry_count}' \
    > "$history_file"
  info "Job for issue #${issue_number} abandoned → ${history_file}"

  # Update labels (remove wip + plan-review, add failed)
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --add-label "$PRAUTO_GITHUB_LABEL_FAILED" 2>/dev/null || \
    warn "Failed to update labels on issue #${issue_number}"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || true

  # Post comment (with idempotency check)
  if ! comment_exists "issue" "$issue_number" "Abandoning"; then
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Abandoning after ${retry_count} retries. Manual intervention needed." \
      2>/dev/null || warn "Failed to post abandonment comment on issue #${issue_number}"
  fi
}

# Record job completion to history.
# Usage: complete_job <issue_number>
complete_job() {
  local issue_number="$1"
  local date_prefix
  date_prefix=$(date +%Y%m%d)
  local history_file="${HISTORY_DIR}/${date_prefix}_I-${issue_number}.json"
  jq -n \
    --argjson issue_number "$issue_number" \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issue_number: $issue_number, completed_at: $completed_at}' \
    > "$history_file"
  info "Job for issue #${issue_number} completed → ${history_file}"
}

