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
    [[ -n "${SECRETS_TEMP_DIR:-}" ]] && rmdir "$SECRETS_TEMP_DIR" 2>/dev/null || true
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
SECRETS_TEMP_DIR="${PRAUTO_DIR}/state/.secrets-$$"
mkdir -p "$SECRETS_TEMP_DIR"
SECRETS_TEMP_FILE="${SECRETS_TEMP_DIR}/config.local.env"
cp "$PRAUTO_DIR/config.local.env" "$SECRETS_TEMP_FILE"
info "Secrets backed up to ${SECRETS_TEMP_FILE}."

# ---------------------------------------------------------------------------
# Step 4: Check token quota
# ---------------------------------------------------------------------------
if ! check_quota; then
  warn "Token quota exhausted or auth failed."
  # Notify on all WIP issues so humans can see why work stopped
  if find_all_claimed_issues; then
    qi=0
    while [[ "$qi" -lt "$ALL_CLAIMED_COUNT" ]]; do
      q_labels=$(echo "$ALL_CLAIMED_ISSUES" | jq ".[$qi].labels | map(.name)")
      if labels_contain "$q_labels" "$PRAUTO_GITHUB_LABEL_WIP"; then
        q_issue=$(echo "$ALL_CLAIMED_ISSUES" | jq -r ".[$qi].number")
        post_quota_paused_comment "$q_issue"
      fi
      qi=$((qi + 1))
    done
  fi
  exit 0
fi
info "Token quota available."

# ---------------------------------------------------------------------------
# Step 5: Claim new issue (if under limit)
# Uses find_all_claimed_issues to both count and list claimed issues.
# If under PRAUTO_OPEN_ISSUE_LIMIT, finds and claims the oldest eligible issue.
# ---------------------------------------------------------------------------
CLAIMED_NEW_ISSUE=""
find_all_claimed_issues || true
if [[ "${ALL_CLAIMED_COUNT:-0}" -ge "${PRAUTO_OPEN_ISSUE_LIMIT}" ]]; then
  info "Open issue limit reached (${ALL_CLAIMED_COUNT}/${PRAUTO_OPEN_ISSUE_LIMIT}). Skipping new issue pickup."
else
  if find_eligible_issue; then
    if claim_issue "$FOUND_ISSUE_NUMBER"; then
      info "Claimed issue #${FOUND_ISSUE_NUMBER}."
      CLAIMED_NEW_ISSUE="$FOUND_ISSUE_NUMBER"
    else
      warn "Failed to claim issue #${FOUND_ISSUE_NUMBER}."
    fi
  else
    info "No eligible issues to claim."
  fi
fi

# ---------------------------------------------------------------------------
# Step 6: Process all claimed issues (oldest first)
# Re-fetches from GitHub if a new issue was claimed (to include it).
# Each iteration is a self-contained state machine for one issue.
# Issues needing active work are processed; terminal or waiting issues are skipped.
# ---------------------------------------------------------------------------
if [[ -n "$CLAIMED_NEW_ISSUE" ]]; then
  find_all_claimed_issues || true
fi
pending_claimed_count=0
if [[ "${ALL_CLAIMED_COUNT:-0}" -gt 0 ]]; then
  claim_i=0
  while [[ "$claim_i" -lt "$ALL_CLAIMED_COUNT" ]]; do
    CUR_ISSUE_NUMBER=$(echo "$ALL_CLAIMED_ISSUES" | jq -r ".[$claim_i].number")
    CUR_ISSUE_TITLE=$(echo "$ALL_CLAIMED_ISSUES" | jq -r ".[$claim_i].title")
    CUR_LABELS=$(echo "$ALL_CLAIMED_ISSUES" | jq ".[$claim_i].labels | map(.name)")
    CUR_BRANCH="${PRAUTO_BRANCH_PREFIX}I-${CUR_ISSUE_NUMBER}"

    # ---- Terminal states: nothing to do ----
    if labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_DONE" || \
       labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_FAILED"; then
      claim_i=$((claim_i + 1)); continue
    fi

    # Fetch the ready-label timestamp once per issue (anchor for comment filtering)
    get_ready_label_timestamp "$CUR_ISSUE_NUMBER"

    # ---- prauto:wip — active work item ----
    if labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_WIP"; then
      init_issue_session "$CUR_ISSUE_NUMBER"
      derive_phase_from_github "$CUR_ISSUE_NUMBER" "$CUR_BRANCH"
      info "WIP #${CUR_ISSUE_NUMBER}: phase=${DERIVED_PHASE}"

      # Plan-approval: peek to decide wait vs active work
      if [[ "$DERIVED_PHASE" == "plan-approval" ]]; then
        COUNTER_PROPOSAL=""
        peek_status=0
        check_plan_approval "$CUR_ISSUE_NUMBER" || peek_status=$?

        if [[ "$peek_status" -eq 1 ]]; then
          info "Issue #${CUR_ISSUE_NUMBER}: waiting for plan approval. Skipping."
          if has_quota_paused_comment "$CUR_ISSUE_NUMBER"; then
            post_quota_resumed_comment "$CUR_ISSUE_NUMBER"
          fi
          pending_claimed_count=$((pending_claimed_count + 1))
          claim_i=$((claim_i + 1)); continue
        fi

        # Actionable (approved, counter-proposal, or missing plan)
        if has_quota_paused_comment "$CUR_ISSUE_NUMBER"; then
          post_quota_resumed_comment "$CUR_ISSUE_NUMBER"
        fi
        create_branch "$CUR_ISSUE_NUMBER"
        cd "$WORKTREE_DIR"
        handle_phase_plan_approval "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH"
        info "Plan-approval work complete for #${CUR_ISSUE_NUMBER}."
        cleanup_worktree
        claim_i=$((claim_i + 1)); continue
      fi

      # Non plan-approval WIP: retry tracking + phase handling
      count_heartbeat_comments "$CUR_ISSUE_NUMBER"
      retry_count=$((HEARTBEAT_COMMENT_COUNT + 1))

      if [[ "$HEARTBEAT_COMMENT_COUNT" -ge "$PRAUTO_MAX_RETRIES_PER_JOB" ]]; then
        warn "Issue #${CUR_ISSUE_NUMBER} exceeded max retries (${HEARTBEAT_COMMENT_COUNT}/${PRAUTO_MAX_RETRIES_PER_JOB})."
        abandon_job_github "$CUR_ISSUE_NUMBER" "$HEARTBEAT_COMMENT_COUNT"
        claim_i=$((claim_i + 1)); continue
      fi

      post_heartbeat_comment "$CUR_ISSUE_NUMBER" "$DERIVED_PHASE" "$retry_count" "$PRAUTO_MAX_RETRIES_PER_JOB"

      if has_quota_paused_comment "$CUR_ISSUE_NUMBER"; then
        post_quota_resumed_comment "$CUR_ISSUE_NUMBER"
      fi

      info "Resuming issue #${CUR_ISSUE_NUMBER} (phase: ${DERIVED_PHASE}, attempt: ${retry_count}/${PRAUTO_MAX_RETRIES_PER_JOB})."

      create_branch "$CUR_ISSUE_NUMBER"
      cd "$WORKTREE_DIR"

      case "$DERIVED_PHASE" in
        analysis)        handle_phase_analysis "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
        implementation)  handle_phase_implementation "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
        pr)              handle_phase_pr "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
        *)               warn "Unknown phase: ${DERIVED_PHASE}. Abandoning."; abandon_job_github "$CUR_ISSUE_NUMBER" "$HEARTBEAT_COMMENT_COUNT" ;;
      esac

      info "WIP issue #${CUR_ISSUE_NUMBER} processing complete."
      cleanup_worktree
      claim_i=$((claim_i + 1)); continue
    fi

    # ---- prauto:review — PR in code review ----
    if labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_REVIEW"; then
      init_issue_session "$CUR_ISSUE_NUMBER"
      if check_review_pr "$CUR_ISSUE_NUMBER"; then
        case "$REVIEW_PR_ACTION" in
          squash_ready)
            info "Squash-finalizing PR #${REVIEW_PR_NUMBER} for issue #${CUR_ISSUE_NUMBER}..."
            checkout_branch_worktree "$REVIEW_PR_BRANCH"
            cd "$WORKTREE_DIR"
            if squash_and_finalize_pr \
                "$REVIEW_PR_NUMBER" "$REVIEW_PR_BRANCH" \
                "$REVIEW_PR_TITLE" "$REVIEW_PR_BODY" "$CUR_ISSUE_NUMBER"; then
              info "Squash-finalize complete for #${CUR_ISSUE_NUMBER}."
            else
              warn "Squash-finalize failed for PR #${REVIEW_PR_NUMBER}."
            fi
            cleanup_worktree
            ;;
          feedback_needed)
            info "Addressing reviewer feedback on PR #${REVIEW_PR_NUMBER} for issue #${CUR_ISSUE_NUMBER}..."
            checkout_branch_worktree "$REVIEW_PR_BRANCH"
            cd "$WORKTREE_DIR"
            run_pr_review "$CUR_ISSUE_NUMBER" "$REVIEW_PR_BRANCH" "$ACTIONABLE_COMMENTS"
            push_branch "$REVIEW_PR_BRANCH"
            create_or_update_pr "$CUR_ISSUE_NUMBER" "" "$REVIEW_PR_BRANCH"
            run_and_post_test_results "$REVIEW_PR_BRANCH"
            post_review_response_comment "$REVIEW_PR_NUMBER" "$REVIEW_RESPONSE"
            post_feedback_addressed_comment "$REVIEW_PR_NUMBER"
            complete_job "$CUR_ISSUE_NUMBER"
            info "PR review complete for #${CUR_ISSUE_NUMBER}."
            cleanup_worktree
            ;;
        esac
        claim_i=$((claim_i + 1)); continue
      fi
      # Waiting for review
      pending_claimed_count=$((pending_claimed_count + 1))
      info "Issue #${CUR_ISSUE_NUMBER}: PR waiting for review. Skipping."
      claim_i=$((claim_i + 1)); continue
    fi

    # ---- Unknown prauto label combination ----
    claim_i=$((claim_i + 1))
  done

  info "All claimed issues checked. ${pending_claimed_count} pending."
fi

# ---------------------------------------------------------------------------
# Step 7: Restore secrets and release lock (handled by trap)
# ---------------------------------------------------------------------------
info "Heartbeat complete."
