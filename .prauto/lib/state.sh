# Job state management for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, jq available.

STATE_DIR="${PRAUTO_DIR}/state"
LOCK_FILE="${STATE_DIR}/heartbeat.lock"
JOB_FILE="${STATE_DIR}/current-job.json"
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

# Write monitoring state to current-job.json (monitoring-only artifact).
# This file is NOT used for routing — it exists purely for external monitoring tools.
# Usage: write_monitor_state <issue_number> <issue_title> <branch> <source> <phase>
write_monitor_state() {
  local issue_number="$1"
  local issue_title="$2"
  local branch="$3"
  local source="$4"
  local phase="$5"

  ensure_state_dirs

  jq -n \
    --argjson issue_number "$issue_number" \
    --arg issue_title "$issue_title" \
    --arg branch "$branch" \
    --arg source "$source" \
    --arg phase "$phase" \
    --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      issue_number: $issue_number,
      issue_title: $issue_title,
      branch: $branch,
      source: $source,
      phase: $phase,
      timestamp: $timestamp
    }' > "$JOB_FILE"
}

# Abandon a job after max retries (GitHub-as-SSOT version).
# Takes parameters directly instead of reading from JOB_FILE.
# Moves monitoring file to history if present, updates labels, posts comment.
# Usage: abandon_job_github <issue_number> <retry_count>
abandon_job_github() {
  local issue_number="$1"
  local retry_count="$2"

  # Step 1: Move monitoring state file to history (if present)
  if [[ -f "$JOB_FILE" ]]; then
    local date_prefix
    date_prefix=$(date +%Y%m%d)
    local history_file="${HISTORY_DIR}/${date_prefix}_I-${issue_number}.json"
    mv "$JOB_FILE" "$history_file"
    info "Job for issue #${issue_number} abandoned → ${history_file}"
  fi

  # Step 2: Update labels (remove wip + plan-review, add failed)
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --add-label "$PRAUTO_GITHUB_LABEL_FAILED" 2>/dev/null || \
    warn "Failed to update labels on issue #${issue_number}"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || true

  # Step 3: Post comment (with idempotency check)
  if ! comment_exists "issue" "$issue_number" "Abandoning"; then
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Abandoning after ${retry_count} retries. Manual intervention needed." \
      2>/dev/null || warn "Failed to post abandonment comment on issue #${issue_number}"
  fi
}

# Update specific fields in the current job.
# Usage: update_job_field <field> <value>
update_job_field() {
  local field="$1"
  local value="$2"

  if [[ ! -f "$JOB_FILE" ]]; then
    error "No active job to update"
  fi

  local tmp
  tmp=$(mktemp)
  jq --arg field "$field" --arg value "$value" '.[$field] = $value' "$JOB_FILE" > "$tmp"
  mv "$tmp" "$JOB_FILE"
}

# Move current job to history.
complete_job() {
  if [[ ! -f "$JOB_FILE" ]]; then
    warn "No active job to complete."
    return 0
  fi

  local issue_number
  issue_number=$(jq -r '.issue_number' "$JOB_FILE")
  local date_prefix
  date_prefix=$(date +%Y%m%d)
  local history_file="${HISTORY_DIR}/${date_prefix}_I-${issue_number}.json"

  mv "$JOB_FILE" "$history_file"
  info "Job for issue #${issue_number} completed → ${history_file}"
}

