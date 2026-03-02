#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRAUTO_DIR="$SCRIPT_DIR"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Source libraries
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$PRAUTO_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Trap: ensure lock is released on unexpected exit
# ---------------------------------------------------------------------------
SECRETS_TEMP_FILE=""
WORKTREE_DIR=""
cleanup() {
  # Remove worktree if one was created (cd away first — can't remove cwd)
  if [[ -n "$WORKTREE_DIR" ]] && [[ -d "$WORKTREE_DIR" ]]; then
    cd "$REPO_DIR"
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git worktree prune 2>/dev/null || true
    info "Worktree ${WORKTREE_DIR} cleaned up."
  fi
  # Clean up secrets backup (original stays in place, protected by --disallowedTools denylist)
  if [[ -n "$SECRETS_TEMP_FILE" ]] && [[ -f "$SECRETS_TEMP_FILE" ]]; then
    rm -f "$SECRETS_TEMP_FILE"
    info "Secrets backup cleaned up."
  fi
  # Release lock
  if [[ -f "${PRAUTO_DIR}/state/heartbeat.lock" ]]; then
    local lock_pid
    lock_pid=$(cat "${PRAUTO_DIR}/state/heartbeat.lock" 2>/dev/null || echo "")
    if [[ "$lock_pid" == "$$" ]]; then
      rm -f "${PRAUTO_DIR}/state/heartbeat.lock"
      info "Lock released."
    fi
  fi
}
trap cleanup EXIT

echo ""
echo "=== prauto heartbeat — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""

# ---------------------------------------------------------------------------
# Step 1: Acquire lock
# ---------------------------------------------------------------------------
# shellcheck source=lib/state.sh
source "$PRAUTO_DIR/lib/state.sh"

if ! acquire_lock; then
  exit 0
fi
info "Lock acquired (PID $$)."

# ---------------------------------------------------------------------------
# Step 2: Load config
# ---------------------------------------------------------------------------
load_config "$PRAUTO_DIR"
info "Config loaded (worker: ${PRAUTO_WORKER_ID})."

# Export secrets only when non-empty; otherwise unset so CLIs use system auth.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  export ANTHROPIC_API_KEY
else
  unset ANTHROPIC_API_KEY
  info "ANTHROPIC_API_KEY not set — claude will use system credentials."
fi
if [[ -n "${GH_TOKEN:-}" ]]; then
  export GH_TOKEN
else
  unset GH_TOKEN
  info "GH_TOKEN not set — gh will use system credentials."
fi

# Resolve the GitHub login of the authenticated worker (single source of truth from GH_TOKEN).
PRAUTO_GITHUB_ACTOR=$(gh api user --jq '.login' 2>/dev/null) || {
  error "Failed to resolve GitHub actor from GH_TOKEN / system gh auth. Is gh authenticated?"
}
info "GitHub actor: ${PRAUTO_GITHUB_ACTOR}"

# Check required tools
ensure_command "claude"
ensure_command "gh"
ensure_command "git"
ensure_command "jq"

# ---------------------------------------------------------------------------
# Source remaining libraries (need config loaded first)
# ---------------------------------------------------------------------------
# shellcheck source=lib/quota.sh
source "$PRAUTO_DIR/lib/quota.sh"
# shellcheck source=lib/issues.sh
source "$PRAUTO_DIR/lib/issues.sh"
# shellcheck source=lib/claude.sh
source "$PRAUTO_DIR/lib/claude.sh"
# shellcheck source=lib/git-ops.sh
source "$PRAUTO_DIR/lib/git-ops.sh"

# Ensure state dirs exist
ensure_state_dirs

# Change to repo root for all git operations
cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# Step 3: Secure secrets (move config.local.env out of repo tree)
# ---------------------------------------------------------------------------
SECRETS_TEMP_FILE="/tmp/.prauto-secrets-$$"
cp "$PRAUTO_DIR/config.local.env" "$SECRETS_TEMP_FILE"
info "Secrets backed up to ${SECRETS_TEMP_FILE}."

# ---------------------------------------------------------------------------
# Step 4: Check token quota
# ---------------------------------------------------------------------------
if ! check_quota; then
  warn "Token quota exhausted or auth failed. Exiting."
  exit 0
fi
info "Token quota available."

# ---------------------------------------------------------------------------
# Step 5: Resume interrupted job
# ---------------------------------------------------------------------------
if has_active_job; then
  info "Found active job. Attempting resume..."
  load_job

  # Verify issue assignee matches this worker (prauto:wip ownership check)
  issue_assignee=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --json assignees --jq '.assignees[].login' 2>/dev/null || echo "")
  if ! echo "$issue_assignee" | grep -q "^${PRAUTO_GITHUB_ACTOR}$"; then
    warn "Issue #${JOB_ISSUE_NUMBER} assignee (${issue_assignee}) does not match this worker (${PRAUTO_GITHUB_ACTOR}). Skipping."
    exit 0
  fi

  # For plan-approval phase, don't count retries (waiting is not a failure)
  if [[ "$JOB_PHASE" != "plan-approval" ]]; then
    # Check max retries
    if [[ "$JOB_RETRIES" -ge "$PRAUTO_MAX_RETRIES_PER_JOB" ]]; then
      warn "Job for issue #${JOB_ISSUE_NUMBER} exceeded max retries (${JOB_RETRIES}/${PRAUTO_MAX_RETRIES_PER_JOB})."
      abandon_job
      exit 0
    fi

    # Increment retries and update heartbeat timestamp
    bump_heartbeat
  fi
  info "Resuming job for issue #${JOB_ISSUE_NUMBER} (phase: ${JOB_PHASE}, retry: ${JOB_RETRIES})."

  # Create a worktree for the job's branch
  create_branch "$JOB_ISSUE_NUMBER"
  cd "$WORKTREE_DIR"

  case "$JOB_PHASE" in
    analysis)
      # Re-run analysis from scratch (cheap)
      run_analysis "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" ""
      # Fetch issue body for change-size detection
      issue_body_raw=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
        --json body --jq '.body // ""' 2>/dev/null || echo "")
      change_size=$(extract_change_size "$issue_body_raw")
      post_plan_comment "$JOB_ISSUE_NUMBER" "$ANALYSIS_OUTPUT" "$change_size"
      if [[ "$change_size" != "minor" ]]; then
        update_job_field "phase" "plan-approval"
        info "Plan posted for ${change_size} change. Waiting for approval. Exiting."
        exit 0
      fi
      update_job_field "phase" "implementation"
      update_job_field "session_id" ""
      # Fall through to implementation
      run_implementation "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "$ANALYSIS_OUTPUT"
      update_job_field "phase" "pr"
      update_job_field "session_id" "$IMPL_SESSION_ID"
      # Fall through to PR
      push_branch "$JOB_BRANCH"
      create_or_update_pr "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$JOB_BRANCH"
      # Update labels
      gh issue edit "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
        --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
        --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
      complete_job
      ;;
    plan-approval)
      COUNTER_PROPOSAL=""
      approval_status=0
      check_plan_approval "$JOB_ISSUE_NUMBER" || approval_status=$?
      if [[ "$approval_status" -eq 0 ]]; then
        # Approved — proceed to implementation
        info "Plan approved. Starting implementation..."
        # Load the last analysis output
        analysis_file="${SESSIONS_DIR}/analysis-I-${JOB_ISSUE_NUMBER}.txt"
        saved_analysis=""
        if [[ -f "$analysis_file" ]]; then
          saved_analysis=$(cat "$analysis_file")
        fi
        update_job_field "phase" "implementation"
        update_job_field "session_id" ""
        run_implementation "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "$saved_analysis"
        update_job_field "phase" "pr"
        update_job_field "session_id" "$IMPL_SESSION_ID"
        push_branch "$JOB_BRANCH"
        create_or_update_pr "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$JOB_BRANCH"
        gh issue edit "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
          --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
          --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
        complete_job
      elif [[ "$approval_status" -eq 2 ]]; then
        # Counter-proposal — re-run analysis with feedback
        info "Counter-proposal received. Re-running analysis..."
        issue_body_raw=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
          --json body --jq '.body // ""' 2>/dev/null || echo "")
        run_analysis "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$issue_body_raw" "$COUNTER_PROPOSAL"
        change_size=$(extract_change_size "$issue_body_raw")
        # Increment plan revision
        current_rev=$(jq -r '.plan_revision // 1' "$JOB_FILE")
        next_rev=$(( current_rev + 1 ))
        update_job_field "plan_revision" "$next_rev"
        post_plan_comment "$JOB_ISSUE_NUMBER" "$ANALYSIS_OUTPUT" "$change_size" "$next_rev"
        # Stay in plan-approval phase, exit
        info "Revised plan (rev ${next_rev}) posted. Waiting for approval. Exiting."
        exit 0
      else
        # No response yet — just wait (don't bump retries)
        info "Still waiting for plan approval on issue #${JOB_ISSUE_NUMBER}."
        exit 0
      fi
      ;;
    implementation)
      run_implementation "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "" "$JOB_SESSION_ID"
      update_job_field "phase" "pr"
      update_job_field "session_id" "$IMPL_SESSION_ID"
      push_branch "$JOB_BRANCH"
      create_or_update_pr "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$JOB_BRANCH"
      gh issue edit "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
        --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
        --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
      complete_job
      ;;
    pr-review)
      run_pr_review "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "" "$JOB_SESSION_ID"
      update_job_field "phase" "pr"
      update_job_field "session_id" "$REVIEW_SESSION_ID"
      push_branch "$JOB_BRANCH"
      create_or_update_pr "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$JOB_BRANCH"
      # Post feedback-addressed marker (derive PR number from branch)
      local review_pr_number
      review_pr_number=$(gh pr list -R "$PRAUTO_GITHUB_REPO" --head "$JOB_BRANCH" --json number --jq '.[0].number // empty' 2>/dev/null)
      if [[ -n "$review_pr_number" ]]; then
        post_feedback_addressed_comment "$review_pr_number"
      fi
      complete_job
      ;;
    pr)
      push_branch "$JOB_BRANCH"
      create_or_update_pr "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$JOB_BRANCH"
      gh issue edit "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
        --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
        --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
      complete_job
      ;;
    *)
      warn "Unknown phase: ${JOB_PHASE}. Abandoning job."
      abandon_job
      ;;
  esac

  info "Resume complete. Exiting."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 5.5: Squash and finalize approved PRs (do NOT merge)
# ---------------------------------------------------------------------------
if find_mergeable_prs; then
  info "Squash-finalizing approved PR #${MERGEABLE_PR_NUMBER} (${MERGEABLE_PR_BRANCH})..."
  checkout_branch_worktree "$MERGEABLE_PR_BRANCH"
  cd "$WORKTREE_DIR"
  if squash_and_finalize_pr \
      "$MERGEABLE_PR_NUMBER" \
      "$MERGEABLE_PR_BRANCH" \
      "$MERGEABLE_PR_TITLE" \
      "$MERGEABLE_PR_BODY" \
      "$MERGEABLE_PR_ISSUE"; then
    info "Squash-finalize complete. Exiting."
  else
    warn "Squash-finalize failed for PR #${MERGEABLE_PR_NUMBER}. Exiting."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 6: Check open PRs for reviewer comments
# ---------------------------------------------------------------------------
if find_actionable_prs; then
  info "Addressing reviewer feedback on PR #${ACTIONABLE_PR_NUMBER}..."

  # Create job state for PR review
  save_job "$ACTIONABLE_PR_ISSUE" "" "$ACTIONABLE_PR_BRANCH" "pr-review" "pr-review"

  # Create a worktree for the PR branch
  checkout_branch_worktree "$ACTIONABLE_PR_BRANCH"
  cd "$WORKTREE_DIR"

  # Run PR review phase
  run_pr_review "$ACTIONABLE_PR_ISSUE" "$ACTIONABLE_PR_BRANCH" "$ACTIONABLE_COMMENTS"
  update_job_field "phase" "pr"
  update_job_field "session_id" "$REVIEW_SESSION_ID"

  # Push and update PR
  push_branch "$ACTIONABLE_PR_BRANCH"
  create_or_update_pr "$ACTIONABLE_PR_ISSUE" "" "$ACTIONABLE_PR_BRANCH"
  post_feedback_addressed_comment "$ACTIONABLE_PR_NUMBER"

  # Complete job
  complete_job
  info "PR review complete. Exiting."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 7: Find eligible issue
# ---------------------------------------------------------------------------
if ! find_eligible_issue; then
  info "No work to do. Exiting."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 8: Claim issue
# ---------------------------------------------------------------------------
if ! claim_issue "$FOUND_ISSUE_NUMBER"; then
  warn "Failed to claim issue #${FOUND_ISSUE_NUMBER}. Exiting."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 9: Create branch
# ---------------------------------------------------------------------------
create_branch "$FOUND_ISSUE_NUMBER"
cd "$WORKTREE_DIR"

# Save job state
save_job "$FOUND_ISSUE_NUMBER" "$FOUND_ISSUE_TITLE" "$BRANCH_NAME" "issue" "analysis"

# ---------------------------------------------------------------------------
# Step 10: Phase 1 — Analysis
# ---------------------------------------------------------------------------
info "Starting analysis phase for issue #${FOUND_ISSUE_NUMBER}..."
run_analysis "$FOUND_ISSUE_NUMBER" "$FOUND_ISSUE_TITLE" "$FOUND_ISSUE_BODY"

# ---------------------------------------------------------------------------
# Step 10.5: Post plan & check approval gate
# ---------------------------------------------------------------------------
CHANGE_SIZE=$(extract_change_size "$FOUND_ISSUE_BODY")
info "Change size: ${CHANGE_SIZE}"
post_plan_comment "$FOUND_ISSUE_NUMBER" "$ANALYSIS_OUTPUT" "$CHANGE_SIZE"

if [[ "$CHANGE_SIZE" != "minor" ]]; then
  update_job_field "phase" "plan-approval"
  info "Plan posted for ${CHANGE_SIZE} change. Waiting for approval. Exiting."
  exit 0
fi

update_job_field "phase" "implementation"

# ---------------------------------------------------------------------------
# Step 11: Phase 2 — Implementation
# ---------------------------------------------------------------------------
info "Starting implementation phase for issue #${FOUND_ISSUE_NUMBER}..."
run_implementation "$FOUND_ISSUE_NUMBER" "$BRANCH_NAME" "$ANALYSIS_OUTPUT"
update_job_field "phase" "pr"
update_job_field "session_id" "$IMPL_SESSION_ID"

# ---------------------------------------------------------------------------
# Step 12: Create/update PR
# ---------------------------------------------------------------------------
push_branch "$BRANCH_NAME"
create_or_update_pr "$FOUND_ISSUE_NUMBER" "$FOUND_ISSUE_TITLE" "$BRANCH_NAME"

# ---------------------------------------------------------------------------
# Step 13: Complete job
# ---------------------------------------------------------------------------
# Update labels: remove wip, add review
gh issue edit "$FOUND_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
  --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
  --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true

complete_job

# ---------------------------------------------------------------------------
# Steps 14-15: Restore secrets and release lock (handled by trap)
# ---------------------------------------------------------------------------
info "Heartbeat complete."
