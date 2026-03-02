# Phase handlers for prauto heartbeat.
# Source this file — do not execute directly.
# Requires: helpers.sh, state.sh, quota.sh, issues.sh, claude.sh, git-ops.sh, pr.sh
#           all sourced, config loaded.
# All handlers accept (issue_number, issue_title, branch) parameters.

# Shared helper: push, create/update PR, swap labels, complete job.
# Usage: finalize_issue_pr <branch> <issue_number> <issue_title>
finalize_issue_pr() {
  local branch="$1" issue_number="$2" issue_title="$3"
  push_branch "$branch"
  create_or_update_pr "$issue_number" "$issue_title" "$branch"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
  complete_job "$issue_number"
}

# Fetch the approved plan text from GitHub issue comments.
# Returns the body of the latest plan comment posted by this worker.
# Usage: fetch_approved_plan <issue_number>
# Sets: APPROVED_PLAN_TEXT
fetch_approved_plan() {
  local issue_number="$1"
  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"

  APPROVED_PLAN_TEXT=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq "[.comments[] | select(.body | startswith(\"${plan_prefix}\"))] | last | .body // \"\"" \
    2>/dev/null) || APPROVED_PLAN_TEXT=""

  # Strip the prauto header and metadata, keep the plan content
  if [[ -n "$APPROVED_PLAN_TEXT" ]]; then
    # Extract everything after "## Implementation Plan" header
    local plan_body
    plan_body=$(echo "$APPROVED_PLAN_TEXT" | sed -n '/^## Implementation Plan$/,$ p' | tail -n +2)
    # Strip trailing footer (everything after the last ---)
    plan_body=$(echo "$plan_body" | sed '/^---$/,$ d')
    if [[ -n "$plan_body" ]]; then
      APPROVED_PLAN_TEXT="$plan_body"
    fi
  fi
}

# Phase: analysis — run analysis, post plan, auto-proceed for minor changes.
# Usage: handle_phase_analysis <issue_number> <issue_title> <branch>
handle_phase_analysis() {
  local issue_number="$1" issue_title="$2" branch="$3"

  # Re-run analysis from scratch (cheap)
  if ! run_analysis "$issue_number" "$issue_title" ""; then
    warn "Analysis failed for issue #${issue_number}. Will retry next heartbeat."
    exit 0
  fi
  # Fetch issue body for change-size detection
  local issue_body_raw
  issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json body --jq '.body // ""' 2>/dev/null || echo "")
  local change_size
  change_size=$(extract_change_size "$issue_body_raw")
  post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size"
  if [[ "$change_size" != "minor" ]]; then
    info "Plan posted for ${change_size} change. Waiting for approval. Exiting."
    exit 0
  fi
  # Fall through to implementation
  run_implementation "$issue_number" "$branch" "$ANALYSIS_OUTPUT"
  # Fall through to PR
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}

# Phase: plan-approval — check approval, handle counter-proposal or missing plan.
# Usage: handle_phase_plan_approval <issue_number> <issue_title> <branch>
handle_phase_plan_approval() {
  local issue_number="$1" issue_title="$2" branch="$3"

  COUNTER_PROPOSAL=""
  local approval_status=0
  check_plan_approval "$issue_number" || approval_status=$?
  if [[ "$approval_status" -eq 0 ]]; then
    # Approved — remove plan-review label, proceed to implementation
    info "Plan approved. Starting implementation..."
    gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || true
    # Fetch the plan from GitHub (not local session file)
    fetch_approved_plan "$issue_number"
    run_implementation "$issue_number" "$branch" "$APPROVED_PLAN_TEXT"
    finalize_issue_pr "$branch" "$issue_number" "$issue_title"
  elif [[ "$approval_status" -eq 2 ]]; then
    # Counter-proposal — respond to feedback, then revise plan
    info "Counter-proposal received. Revising plan..."
    local issue_body_raw
    issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || echo "")
    fetch_approved_plan "$issue_number"
    # Generate and post response to feedback before re-analysis
    generate_feedback_response "$issue_number" "$issue_title" "$COUNTER_PROPOSAL" "$APPROVED_PLAN_TEXT"
    post_feedback_response_comment "$issue_number" "$FEEDBACK_RESPONSE_TEXT"
    if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw" "$COUNTER_PROPOSAL" "$APPROVED_PLAN_TEXT"; then
      warn "Re-analysis failed for issue #${issue_number}. Will retry next heartbeat."
      exit 0
    fi
    local change_size
    change_size=$(extract_change_size "$issue_body_raw")
    # Derive plan revision from GitHub comment count (SSOT)
    get_plan_revision_from_github "$issue_number"
    post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size" "$GITHUB_PLAN_REVISION"
    # Stay in plan-approval phase, exit
    info "Revised plan (rev ${GITHUB_PLAN_REVISION}) posted. Waiting for approval. Exiting."
    exit 0
  elif [[ "$approval_status" -eq 3 ]]; then
    # Plan comment missing — re-run analysis from GitHub state
    info "Plan comment missing on issue #${issue_number}. Re-running analysis..."
    local issue_body_raw
    issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || echo "")
    if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw"; then
      warn "Re-analysis failed for issue #${issue_number}. Will retry next heartbeat."
      exit 0
    fi
    local change_size
    change_size=$(extract_change_size "$issue_body_raw")
    post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size"
    if [[ "$change_size" != "minor" ]]; then
      info "Plan re-posted for ${change_size} change. Waiting for approval. Exiting."
      exit 0
    fi
    # Minor → proceed to implementation (same as approval path)
    run_implementation "$issue_number" "$branch" "$ANALYSIS_OUTPUT"
    finalize_issue_pr "$branch" "$issue_number" "$issue_title"
  else
    # No response yet — just wait (don't bump retries)
    info "Still waiting for plan approval on issue #${issue_number}."
    exit 0
  fi
}

# Phase: implementation — start fresh implementation, finalize PR.
# Usage: handle_phase_implementation <issue_number> <issue_title> <branch>
handle_phase_implementation() {
  local issue_number="$1" issue_title="$2" branch="$3"

  # Fetch the plan from GitHub for context
  fetch_approved_plan "$issue_number"
  run_implementation "$issue_number" "$branch" "$APPROVED_PLAN_TEXT"
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}

# Phase: pr-review — address reviewer feedback, push, post feedback marker.
# Usage: handle_phase_pr_review <issue_number> <issue_title> <branch>
handle_phase_pr_review() {
  local issue_number="$1" issue_title="$2" branch="$3"

  # Fetch reviewer comments from PR
  local review_pr_number
  review_pr_number=$(gh pr list -R "$PRAUTO_GITHUB_REPO" --head "$branch" \
    --json number --jq '.[0].number // empty' 2>/dev/null)

  local reviewer_comments=""
  if [[ -n "$review_pr_number" ]]; then
    local pr_review_comments pr_issue_comments
    pr_review_comments=$(gh api "repos/${PRAUTO_GITHUB_REPO}/pulls/${review_pr_number}/comments" \
      --jq '[.[] | {body: .body, user: .user.login}]' 2>/dev/null || echo "[]")
    pr_issue_comments=$(gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${review_pr_number}/comments" \
      --jq '[.[] | {body: .body, user: .user.login}]' 2>/dev/null || echo "[]")
    reviewer_comments=$(jq -s 'add' <<< "${pr_review_comments}${pr_issue_comments}" 2>/dev/null | \
      jq -r --arg worker "prauto(${PRAUTO_WORKER_ID})" '
        [.[] | select(.body | startswith($worker) | not)]
        | map("Comment by \(.user):\n\(.body)")
        | join("\n\n---\n\n")
      ')
  fi

  run_pr_review "$issue_number" "$branch" "$reviewer_comments"
  push_branch "$branch"
  create_or_update_pr "$issue_number" "$issue_title" "$branch"
  # Post review response and feedback-addressed marker
  if [[ -n "$review_pr_number" ]]; then
    post_review_response_comment "$review_pr_number" "$REVIEW_RESPONSE"
    post_feedback_addressed_comment "$review_pr_number"
  fi
  complete_job "$issue_number"
}

# Phase: pr — just push + create PR + labels.
# Usage: handle_phase_pr <issue_number> <issue_title> <branch>
handle_phase_pr() {
  local issue_number="$1" issue_title="$2" branch="$3"
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}
