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
# shellcheck source=lib/pr.sh
source "$PRAUTO_DIR/lib/pr.sh"
# shellcheck source=lib/phases.sh
source "$PRAUTO_DIR/lib/phases.sh"

# Ensure state dirs exist
ensure_state_dirs

# Reset ephemeral state (GitHub is SSOT — start each heartbeat from a clean local slate)
reset_ephemeral_state
info "Ephemeral state reset."

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
  warn "Token quota exhausted or auth failed."
  # Notify on the WIP issue (if any) so humans can see why work stopped
  if find_wip_issue; then
    post_quota_paused_comment "$WIP_ISSUE_NUMBER"
  fi
  exit 0
fi
info "Token quota available."

# ---------------------------------------------------------------------------
# Step 5: Find WIP issue on GitHub (GitHub-as-SSOT — replaces local resume + orphan recovery)
# ---------------------------------------------------------------------------
if find_wip_issue; then
  info "Found WIP issue #${WIP_ISSUE_NUMBER} on GitHub. Deriving phase..."

  derive_phase_from_github "$WIP_ISSUE_NUMBER" "$WIP_BRANCH"
  info "Derived phase: ${DERIVED_PHASE}"

  # For plan-approval phase, skip retry tracking and heartbeat comment (just check approval)
  if [[ "$DERIVED_PHASE" == "plan-approval" ]]; then
    # If the previous heartbeat posted a quota-paused comment, post a resumed notice
    if has_quota_paused_comment "$WIP_ISSUE_NUMBER"; then
      post_quota_resumed_comment "$WIP_ISSUE_NUMBER"
    fi

    # Create worktree and route to plan-approval handler
    create_branch "$WIP_ISSUE_NUMBER"
    cd "$WORKTREE_DIR"
    handle_phase_plan_approval "$WIP_ISSUE_NUMBER" "$WIP_ISSUE_TITLE" "$WIP_BRANCH"
    info "Plan-approval check complete. Exiting."
    exit 0
  fi

  # Count heartbeat comments for retry tracking
  count_heartbeat_comments "$WIP_ISSUE_NUMBER"
  local retry_count=$((HEARTBEAT_COMMENT_COUNT + 1))

  # Check max retries
  if [[ "$HEARTBEAT_COMMENT_COUNT" -ge "$PRAUTO_MAX_RETRIES_PER_JOB" ]]; then
    warn "Issue #${WIP_ISSUE_NUMBER} exceeded max retries (${HEARTBEAT_COMMENT_COUNT}/${PRAUTO_MAX_RETRIES_PER_JOB})."
    abandon_job_github "$WIP_ISSUE_NUMBER" "$HEARTBEAT_COMMENT_COUNT"
    exit 0
  fi

  # Post heartbeat comment (retry marker on GitHub)
  post_heartbeat_comment "$WIP_ISSUE_NUMBER" "$DERIVED_PHASE" "$retry_count" "$PRAUTO_MAX_RETRIES_PER_JOB"

  # If the previous heartbeat posted a quota-paused comment, post a resumed notice
  if has_quota_paused_comment "$WIP_ISSUE_NUMBER"; then
    post_quota_resumed_comment "$WIP_ISSUE_NUMBER"
  fi

  info "Resuming issue #${WIP_ISSUE_NUMBER} (phase: ${DERIVED_PHASE}, attempt: ${retry_count}/${PRAUTO_MAX_RETRIES_PER_JOB})."

  # Create worktree for the issue's branch
  create_branch "$WIP_ISSUE_NUMBER"
  cd "$WORKTREE_DIR"

  # Route to phase handler
  case "$DERIVED_PHASE" in
    analysis)        handle_phase_analysis "$WIP_ISSUE_NUMBER" "$WIP_ISSUE_TITLE" "$WIP_BRANCH" ;;
    implementation)  handle_phase_implementation "$WIP_ISSUE_NUMBER" "$WIP_ISSUE_TITLE" "$WIP_BRANCH" ;;
    pr-review)       handle_phase_pr_review "$WIP_ISSUE_NUMBER" "$WIP_ISSUE_TITLE" "$WIP_BRANCH" ;;
    pr)              handle_phase_pr "$WIP_ISSUE_NUMBER" "$WIP_ISSUE_TITLE" "$WIP_BRANCH" ;;
    *)               warn "Unknown phase: ${DERIVED_PHASE}. Abandoning."; abandon_job_github "$WIP_ISSUE_NUMBER" "$HEARTBEAT_COMMENT_COUNT" ;;
  esac

  info "WIP issue processing complete. Exiting."
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

  # Create a worktree for the PR branch
  checkout_branch_worktree "$ACTIONABLE_PR_BRANCH"
  cd "$WORKTREE_DIR"

  # Run PR review phase
  run_pr_review "$ACTIONABLE_PR_ISSUE" "$ACTIONABLE_PR_BRANCH" "$ACTIONABLE_COMMENTS"

  # Push and update PR
  push_branch "$ACTIONABLE_PR_BRANCH"
  create_or_update_pr "$ACTIONABLE_PR_ISSUE" "" "$ACTIONABLE_PR_BRANCH"
  post_feedback_addressed_comment "$ACTIONABLE_PR_NUMBER"

  # Complete job
  complete_job "$ACTIONABLE_PR_ISSUE"
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

# ---------------------------------------------------------------------------
# Step 10: Phase 1 — Analysis
# ---------------------------------------------------------------------------
info "Starting analysis phase for issue #${FOUND_ISSUE_NUMBER}..."
if ! run_analysis "$FOUND_ISSUE_NUMBER" "$FOUND_ISSUE_TITLE" "$FOUND_ISSUE_BODY"; then
  warn "Analysis failed for issue #${FOUND_ISSUE_NUMBER}. Will retry next heartbeat."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 10.5: Post plan & check approval gate
# ---------------------------------------------------------------------------
CHANGE_SIZE=$(extract_change_size "$FOUND_ISSUE_BODY")
info "Change size: ${CHANGE_SIZE}"
post_plan_comment "$FOUND_ISSUE_NUMBER" "$ANALYSIS_OUTPUT" "$CHANGE_SIZE"

if [[ "$CHANGE_SIZE" != "minor" ]]; then
  info "Plan posted for ${CHANGE_SIZE} change. Waiting for approval. Exiting."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 11: Phase 2 — Implementation
# ---------------------------------------------------------------------------
info "Starting implementation phase for issue #${FOUND_ISSUE_NUMBER}..."
run_implementation "$FOUND_ISSUE_NUMBER" "$BRANCH_NAME" "$ANALYSIS_OUTPUT"

# ---------------------------------------------------------------------------
# Step 12-13: Create/update PR, update labels, complete job
# ---------------------------------------------------------------------------
finalize_issue_pr "$BRANCH_NAME" "$FOUND_ISSUE_NUMBER" "$FOUND_ISSUE_TITLE"

# ---------------------------------------------------------------------------
# Steps 14-15: Restore secrets and release lock (handled by trap)
# ---------------------------------------------------------------------------
info "Heartbeat complete."
