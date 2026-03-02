# Phase handlers for prauto heartbeat.
# Source this file — do not execute directly.
# Requires: helpers.sh, state.sh, quota.sh, issues.sh, claude.sh, git-ops.sh, pr.sh
#           all sourced, config loaded, JOB_* globals set by load_job().

# Shared helper: push, create/update PR, swap labels, complete job.
# Usage: finalize_issue_pr <branch> <issue_number> <issue_title>
finalize_issue_pr() {
  local branch="$1" issue_number="$2" issue_title="$3"
  push_branch "$branch"
  create_or_update_pr "$issue_number" "$issue_title" "$branch"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
  complete_job
}

# Cross-check: advance local phase if GitHub shows later state.
# Operates on JOB_* globals (mutates JOB_PHASE if stale).
cross_check_phase() {
  # If PR exists for this branch, phase should be "pr"
  if [[ "$JOB_PHASE" != "pr" && "$JOB_PHASE" != "pr-review" ]]; then
    local existing_pr
    existing_pr=$(gh pr list -R "$PRAUTO_GITHUB_REPO" --head "$JOB_BRANCH" \
      --json number --jq '.[0].number // empty' 2>/dev/null)
    if [[ -n "$existing_pr" ]]; then
      warn "Cross-check: PR #${existing_pr} exists but phase='${JOB_PHASE}'. Advancing to 'pr'."
      JOB_PHASE="pr"; update_job_field "phase" "pr"; return
    fi
  fi
  # If phase is "analysis" but plan comment exists, advance to plan-approval
  if [[ "$JOB_PHASE" == "analysis" ]]; then
    local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
    local plan_exists
    plan_exists=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
      --json comments \
      --jq "[.comments[] | select(.body | startswith(\"${plan_prefix}\"))] | length" \
      2>/dev/null) || plan_exists=0
    if [[ "$plan_exists" -gt 0 ]]; then
      warn "Cross-check: plan exists but phase='analysis'. Advancing to 'plan-approval'."
      JOB_PHASE="plan-approval"; update_job_field "phase" "plan-approval"; return
    fi
  fi
}

# Phase: analysis — re-run analysis, post plan, auto-proceed for minor changes.
handle_phase_analysis() {
  # Re-run analysis from scratch (cheap)
  if ! run_analysis "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" ""; then
    warn "Analysis failed for issue #${JOB_ISSUE_NUMBER}. Will retry next heartbeat."
    exit 0
  fi
  # Fetch issue body for change-size detection
  local issue_body_raw
  issue_body_raw=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --json body --jq '.body // ""' 2>/dev/null || echo "")
  local change_size
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
  finalize_issue_pr "$JOB_BRANCH" "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE"
}

# Phase: plan-approval — check approval, handle counter-proposal or missing plan.
handle_phase_plan_approval() {
  COUNTER_PROPOSAL=""
  local approval_status=0
  check_plan_approval "$JOB_ISSUE_NUMBER" || approval_status=$?
  if [[ "$approval_status" -eq 0 ]]; then
    # Approved — proceed to implementation
    info "Plan approved. Starting implementation..."
    # Load the last analysis output
    local analysis_file="${SESSIONS_DIR}/analysis-I-${JOB_ISSUE_NUMBER}.txt"
    local saved_analysis=""
    if [[ -f "$analysis_file" ]]; then
      saved_analysis=$(cat "$analysis_file")
    fi
    update_job_field "phase" "implementation"
    update_job_field "session_id" ""
    run_implementation "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "$saved_analysis"
    update_job_field "phase" "pr"
    update_job_field "session_id" "$IMPL_SESSION_ID"
    finalize_issue_pr "$JOB_BRANCH" "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE"
  elif [[ "$approval_status" -eq 2 ]]; then
    # Counter-proposal — re-run analysis with feedback
    info "Counter-proposal received. Re-running analysis..."
    local issue_body_raw
    issue_body_raw=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || echo "")
    if ! run_analysis "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$issue_body_raw" "$COUNTER_PROPOSAL"; then
      warn "Re-analysis failed for issue #${JOB_ISSUE_NUMBER}. Will retry next heartbeat."
      exit 0
    fi
    local change_size
    change_size=$(extract_change_size "$issue_body_raw")
    # Derive plan revision from GitHub comment count (SSOT)
    get_plan_revision_from_github "$JOB_ISSUE_NUMBER"
    post_plan_comment "$JOB_ISSUE_NUMBER" "$ANALYSIS_OUTPUT" "$change_size" "$GITHUB_PLAN_REVISION"
    update_job_field "plan_revision" "$GITHUB_PLAN_REVISION"  # cache locally
    # Stay in plan-approval phase, exit
    info "Revised plan (rev ${GITHUB_PLAN_REVISION}) posted. Waiting for approval. Exiting."
    exit 0
  elif [[ "$approval_status" -eq 3 ]]; then
    # Plan comment missing — re-run analysis from GitHub state
    info "Plan comment missing on issue #${JOB_ISSUE_NUMBER}. Re-running analysis..."
    local issue_body_raw
    issue_body_raw=$(gh issue view "$JOB_ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || echo "")
    if ! run_analysis "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE" "$issue_body_raw"; then
      warn "Re-analysis failed for issue #${JOB_ISSUE_NUMBER}. Will retry next heartbeat."
      exit 0
    fi
    local change_size
    change_size=$(extract_change_size "$issue_body_raw")
    post_plan_comment "$JOB_ISSUE_NUMBER" "$ANALYSIS_OUTPUT" "$change_size"
    if [[ "$change_size" != "minor" ]]; then
      info "Plan re-posted for ${change_size} change. Waiting for approval. Exiting."
      exit 0
    fi
    # Minor → proceed to implementation (same as approval path)
    update_job_field "phase" "implementation"
    update_job_field "session_id" ""
    run_implementation "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "$ANALYSIS_OUTPUT"
    update_job_field "phase" "pr"
    update_job_field "session_id" "$IMPL_SESSION_ID"
    finalize_issue_pr "$JOB_BRANCH" "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE"
  else
    # No response yet — just wait (don't bump retries)
    info "Still waiting for plan approval on issue #${JOB_ISSUE_NUMBER}."
    exit 0
  fi
}

# Phase: implementation — resume implementation, finalize PR.
handle_phase_implementation() {
  run_implementation "$JOB_ISSUE_NUMBER" "$JOB_BRANCH" "" "$JOB_SESSION_ID"
  update_job_field "phase" "pr"
  update_job_field "session_id" "$IMPL_SESSION_ID"
  finalize_issue_pr "$JOB_BRANCH" "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE"
}

# Phase: pr-review — resume review, push, post feedback marker.
handle_phase_pr_review() {
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
}

# Phase: pr — just push + create PR + labels.
handle_phase_pr() {
  finalize_issue_pr "$JOB_BRANCH" "$JOB_ISSUE_NUMBER" "$JOB_ISSUE_TITLE"
}
