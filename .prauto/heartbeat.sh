#!/usr/bin/env bash
# prauto heartbeat — one wake of the autonomous PR worker.
#
# This is the executor. The loop-master (a Hermes cron job) is now only a
# scheduler + model selector: it probes claude/codex and may pre-set
# PRAUTO_AGENT before invoking this script; when PRAUTO_AGENT is unset or
# `auto`, this script selects the agent itself (select_agent).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRAUTO_DIR="$SCRIPT_DIR"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/helpers.sh
source "$PRAUTO_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Trap: on any exit, remove a live worktree and release the lock. A worker that
# dies must not leave a dirty worktree or a stale lock for the next wake.
# ---------------------------------------------------------------------------
WORKTREE_DIR=""
cleanup() {
  if [[ -n "$WORKTREE_DIR" ]] && [[ -d "$WORKTREE_DIR" ]]; then
    cd "$REPO_DIR"
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git worktree prune 2>/dev/null || true
    info "Worktree ${WORKTREE_DIR} cleaned up."
  fi
  release_lock 2>/dev/null || true
}
trap cleanup EXIT

printf '\n=== prauto heartbeat — %s ===\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------------------------------------------------------------------------
# Step 1: acquire lock
# ---------------------------------------------------------------------------
# shellcheck source=lib/state.sh
source "$PRAUTO_DIR/lib/state.sh"
if ! acquire_lock; then
  exit 0
fi
info "Lock acquired (PID $$)."

# ---------------------------------------------------------------------------
# Step 2: load config + resolve identity
# ---------------------------------------------------------------------------
load_config "$PRAUTO_DIR"
info "Config loaded (worker: ${PRAUTO_WORKER_ID})."

# Export secrets only when non-empty; otherwise unset so CLIs use system auth.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then export ANTHROPIC_API_KEY; else unset ANTHROPIC_API_KEY; fi
if [[ -n "${GH_TOKEN:-}" ]]; then export GH_TOKEN; else unset GH_TOKEN; fi

# Resolve the authenticated GitHub actor ONCE. Every gh write for the rest of
# this run inherits this identity, so comments/labels/assignees are attributed
# to the worker account, not whatever the keyring would fall back to.
PRAUTO_GITHUB_ACTOR=$(gh api user --jq '.login' 2>/dev/null) || {
  error "Failed to resolve GitHub actor from GH_TOKEN / system gh auth. Is gh authenticated?"
}
info "GitHub actor: ${PRAUTO_GITHUB_ACTOR}"
if [[ -n "${PRAUTO_GITHUB_EXPECTED_ACTOR:-}" ]] && [[ "$PRAUTO_GITHUB_ACTOR" != "$PRAUTO_GITHUB_EXPECTED_ACTOR" ]]; then
  error "GitHub actor mismatch: expected ${PRAUTO_GITHUB_EXPECTED_ACTOR}, got ${PRAUTO_GITHUB_ACTOR}. Check GH_TOKEN in config.local.env."
fi

ensure_command "gh"
ensure_command "git"
ensure_command "jq"

# shellcheck source=lib/quota.sh
source "$PRAUTO_DIR/lib/quota.sh"
# shellcheck source=lib/issues.sh
source "$PRAUTO_DIR/lib/issues.sh"
# shellcheck source=lib/agent.sh
source "$PRAUTO_DIR/lib/agent.sh"
# shellcheck source=lib/git-ops.sh
source "$PRAUTO_DIR/lib/git-ops.sh"
# shellcheck source=lib/pr.sh
source "$PRAUTO_DIR/lib/pr.sh"
# shellcheck source=lib/phases.sh
source "$PRAUTO_DIR/lib/phases.sh"

ensure_state_dirs
reset_ephemeral_state
info "Ephemeral state reset."
cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# Step 3: agent selection (pre-flight)
# ---------------------------------------------------------------------------
if ! select_agent; then
  warn "No coding agent available this wake."
  if find_all_claimed_issues; then
    qi=0
    while [[ "$qi" -lt "$ALL_CLAIMED_COUNT" ]]; do
      q_labels=$(printf '%s' "$ALL_CLAIMED_ISSUES" | jq ".[$qi].labels | map(.name)")
      if labels_contain "$q_labels" "$PRAUTO_GITHUB_LABEL_WIP"; then
        q_issue=$(printf '%s' "$ALL_CLAIMED_ISSUES" | jq -r ".[$qi].number")
        get_ready_label_timestamp "$q_issue"
        post_quota_paused_comment "$q_issue" "${PRAUTO_AGENT:-claude}"
      fi
      qi=$((qi + 1))
    done
  fi
  exit 0
fi
info "Agent selected: ${ACTIVE_AGENT}."

# ---------------------------------------------------------------------------
# Step 4: claim new work (if under the open-issue limit)
# ---------------------------------------------------------------------------
CLAIMED_NEW_ISSUE=""
find_all_claimed_issues || true
if [[ "${ALL_CLAIMED_COUNT:-0}" -ge "${PRAUTO_OPEN_ISSUE_LIMIT:-1}" ]]; then
  info "Open issue limit reached (${ALL_CLAIMED_COUNT}/${PRAUTO_OPEN_ISSUE_LIMIT:-1}). Skipping new issue pickup."
else
  if find_eligible_issue; then
    if claim_issue "$FOUND_ISSUE_NUMBER"; then
      CLAIMED_NEW_ISSUE="$FOUND_ISSUE_NUMBER"
      info "Claimed issue #${FOUND_ISSUE_NUMBER}."
    else
      warn "Failed to claim issue #${FOUND_ISSUE_NUMBER}."
    fi
  else
    info "No eligible issues to claim."
  fi
fi

# ---------------------------------------------------------------------------
# Step 5: process all claimed issues (oldest first)
# ---------------------------------------------------------------------------
if [[ -n "$CLAIMED_NEW_ISSUE" ]]; then
  find_all_claimed_issues || true
fi

pending_claimed_count=0
if [[ "${ALL_CLAIMED_COUNT:-0}" -gt 0 ]]; then
  claim_i=0
  while [[ "$claim_i" -lt "$ALL_CLAIMED_COUNT" ]]; do
    CUR_ISSUE_NUMBER=$(printf '%s' "$ALL_CLAIMED_ISSUES" | jq -r ".[$claim_i].number")
    CUR_ISSUE_TITLE=$(printf '%s' "$ALL_CLAIMED_ISSUES" | jq -r ".[$claim_i].title")
    CUR_LABELS=$(printf '%s' "$ALL_CLAIMED_ISSUES" | jq ".[$claim_i].labels | map(.name)")
    CUR_BRANCH="${PRAUTO_BRANCH_PREFIX}I-${CUR_ISSUE_NUMBER}"

    # Terminal states — nothing to do.
    if labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_DONE" || \
       labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_FAILED"; then
      claim_i=$((claim_i + 1)); continue
    fi

    get_ready_label_timestamp "$CUR_ISSUE_NUMBER"

    # ---- prauto:wip — active work item ----
    if labels_contain "$CUR_LABELS" "$PRAUTO_GITHUB_LABEL_WIP"; then
      init_issue_session "$CUR_ISSUE_NUMBER"
      derive_phase_from_github "$CUR_ISSUE_NUMBER" "$CUR_BRANCH"
      info "WIP #${CUR_ISSUE_NUMBER}: phase=${DERIVED_PHASE}"

      # Plan-approval: peek to decide wait vs active (exempt from quota resume —
      # it waits on a human, not on agent quota).
      if [[ "$DERIVED_PHASE" == "plan-approval" ]]; then
        peek_status=0
        check_plan_approval "$CUR_ISSUE_NUMBER" || peek_status=$?
        if [[ "$peek_status" -eq 1 ]]; then
          info "Issue #${CUR_ISSUE_NUMBER}: waiting for plan approval. Skipping."
          pending_claimed_count=$((pending_claimed_count + 1))
          claim_i=$((claim_i + 1)); continue
        fi
        create_branch "$CUR_ISSUE_NUMBER"
        cd "$WORKTREE_DIR"
        handle_phase_plan_approval "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH"
        info "Plan-approval work complete for #${CUR_ISSUE_NUMBER}."
        cleanup_worktree
        claim_i=$((claim_i + 1)); continue
      fi

      # ---- Quota-pause state machine (analysis/implementation/pr phases) ----
      if has_quota_paused_comment "$CUR_ISSUE_NUMBER"; then
        if ! read_pause_marker "$CUR_ISSUE_NUMBER"; then
          warn "Issue #${CUR_ISSUE_NUMBER}: could not read its quota marker. Waiting for a later wake."
          pending_claimed_count=$((pending_claimed_count + 1))
          claim_i=$((claim_i + 1)); continue
        elif [[ "$PAUSED_AGENT" == "codex" ]] && ! codex_pause_marker_is_trusted "$CUR_ISSUE_NUMBER"; then
          # GitHub markers are not authority for a Codex resume. A missing,
          # corrupt, stale, or foreign-author anchor becomes a fresh retry.
          warn "Issue #${CUR_ISSUE_NUMBER}: Codex pause marker is not locally trusted; restarting fresh."
          post_untrusted_resume_restart_comment "$CUR_ISSUE_NUMBER" "$ACTIVE_AGENT"
          # Fall through to normal dispatch below.
        elif has_abandon_override "$CUR_ISSUE_NUMBER"; then
          # Human abandoned the previous session: restart fresh. Re-select the
          # agent (under `auto`, codex takes over if the paused agent is down).
          if ! select_agent; then
            warn "No agent available for the restart of #${CUR_ISSUE_NUMBER}. Will retry next wake."
            pending_claimed_count=$((pending_claimed_count + 1))
            claim_i=$((claim_i + 1)); continue
          fi
          post_restart_comment "$CUR_ISSUE_NUMBER" "$ACTIVE_AGENT"
          # Fall through to the normal dispatch below (a restart is a new attempt).
        elif check_quota "$PAUSED_AGENT"; then
          # Quota reset: resume the SAME session on the SAME agent. No heartbeat
          # (a resume is a continuation, not a new attempt — no retry burn).
          ACTIVE_AGENT="$PAUSED_AGENT"
          create_branch "$CUR_ISSUE_NUMBER"
          cd "$WORKTREE_DIR"
          case "$DERIVED_PHASE" in
            analysis)       handle_phase_analysis "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
            implementation) handle_phase_implementation "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
            *)              warn "Cannot resume phase ${DERIVED_PHASE}. Skipping." ;;
          esac
          cleanup_worktree
          claim_i=$((claim_i + 1)); continue
        else
          # Still quota-paused: wait, no retry burn.
          info "Issue #${CUR_ISSUE_NUMBER}: quota-paused (${PAUSED_AGENT}). Waiting."
          pending_claimed_count=$((pending_claimed_count + 1))
          claim_i=$((claim_i + 1)); continue
        fi
      fi

      # ---- Normal dispatch: retry tracking + heartbeat + phase handler ----
      count_heartbeat_comments "$CUR_ISSUE_NUMBER"
      retry_count=$((HEARTBEAT_COMMENT_COUNT + 1))
      if [[ "$HEARTBEAT_COMMENT_COUNT" -ge "$PRAUTO_MAX_RETRIES_PER_JOB" ]]; then
        warn "Issue #${CUR_ISSUE_NUMBER} exceeded max retries (${HEARTBEAT_COMMENT_COUNT}/${PRAUTO_MAX_RETRIES_PER_JOB})."
        abandon_job_github "$CUR_ISSUE_NUMBER" "$HEARTBEAT_COMMENT_COUNT"
        claim_i=$((claim_i + 1)); continue
      fi

      post_heartbeat_comment "$CUR_ISSUE_NUMBER" "$DERIVED_PHASE" "$retry_count" "$PRAUTO_MAX_RETRIES_PER_JOB"
      info "Dispatching issue #${CUR_ISSUE_NUMBER} (phase: ${DERIVED_PHASE}, attempt: ${retry_count}/${PRAUTO_MAX_RETRIES_PER_JOB})."

      create_branch "$CUR_ISSUE_NUMBER"
      cd "$WORKTREE_DIR"
      case "$DERIVED_PHASE" in
        analysis)       handle_phase_analysis "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
        implementation) handle_phase_implementation "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
        pr)             handle_phase_pr "$CUR_ISSUE_NUMBER" "$CUR_ISSUE_TITLE" "$CUR_BRANCH" ;;
        *)              warn "Unknown phase: ${DERIVED_PHASE}. Abandoning."
                        abandon_job_github "$CUR_ISSUE_NUMBER" "$HEARTBEAT_COMMENT_COUNT" ;;
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
            if squash_and_finalize_pr "$REVIEW_PR_NUMBER" "$REVIEW_PR_BRANCH" "$REVIEW_PR_TITLE" "$REVIEW_PR_BODY" "$CUR_ISSUE_NUMBER"; then
              info "Squash-finalize complete for #${CUR_ISSUE_NUMBER}."
            fi
            cleanup_worktree
            ;;
          feedback_needed)
            info "Addressing reviewer feedback on PR #${REVIEW_PR_NUMBER} for issue #${CUR_ISSUE_NUMBER}..."
            fetch_approved_plan "$CUR_ISSUE_NUMBER"
            checkout_branch_worktree "$REVIEW_PR_BRANCH"
            cd "$WORKTREE_DIR"
            run_pr_review "$CUR_ISSUE_NUMBER" "$REVIEW_PR_BRANCH" "$ACTIONABLE_COMMENTS" "$APPROVED_PLAN_TEXT"
            run_integration_test_fix "$CUR_ISSUE_NUMBER" "$REVIEW_PR_BRANCH"
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
      pending_claimed_count=$((pending_claimed_count + 1))
      info "Issue #${CUR_ISSUE_NUMBER}: PR waiting for review. Skipping."
      claim_i=$((claim_i + 1)); continue
    fi

    claim_i=$((claim_i + 1))
  done

  info "All claimed issues checked. ${pending_claimed_count} pending."
fi

info "Heartbeat complete."
