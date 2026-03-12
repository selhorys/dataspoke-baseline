# Phase handlers for prauto heartbeat.
# Source this file — do not execute directly.
# Requires: helpers.sh, state.sh, quota.sh, issues.sh, claude.sh, git-ops.sh, pr.sh
#           all sourced, config loaded.
# All handlers accept (issue_number, issue_title, branch) parameters.

# Shared helper: push, create/update PR, run tests, post results, swap labels, complete job.
# Usage: finalize_issue_pr <branch> <issue_number> <issue_title>
finalize_issue_pr() {
  local branch="$1" issue_number="$2" issue_title="$3"
  push_branch "$branch"
  create_or_update_pr "$issue_number" "$issue_title" "$branch"
  run_and_post_test_results "$branch"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || true
  complete_job "$issue_number"
}

# Run available test suites and post results as PR comments.
# Unit tests run unconditionally if the directory exists.
# Integration tests follow the dev-env lock protocol (best-effort).
# Usage: run_and_post_test_results <branch>
run_and_post_test_results() {
  local branch="$1"

  get_pr_number_for_branch "$branch"
  if [[ -z "$BRANCH_PR_NUMBER" ]]; then
    warn "No PR found for branch ${branch}. Skipping test result posting."
    return 0
  fi

  # --- Set up .venv via uv sync ---
  if [[ -f "pyproject.toml" ]]; then
    info "Setting up .venv (uv sync)..."
    uv sync 2>&1 || warn "uv sync failed — tests may not run correctly."
  fi

  # --- Unit tests ---
  if [[ -d "tests/unit" ]]; then
    info "Running unit tests..."
    local unit_output unit_exit=0
    unit_output=$(uv run pytest tests/unit/ --tb=short 2>&1) || unit_exit=$?
    post_test_results_comment "$BRANCH_PR_NUMBER" "Unit" "$unit_exit" "$unit_output"
    info "Unit test results posted on PR #${BRANCH_PR_NUMBER} (exit: ${unit_exit})."
  else
    info "No tests/unit/ directory. Skipping unit tests."
  fi

  # --- Integration tests (requires dev-env) ---
  if [[ -d "tests/integration" ]]; then
    run_integration_tests_with_protocol "$BRANCH_PR_NUMBER"
  else
    info "No tests/integration/ directory. Skipping integration tests."
  fi
}

# Run integration tests with the dev-env lock protocol.
# Acquires lock, resets dummy data, runs tests, resets again, releases lock.
# Skips gracefully if dev-env is not reachable or lock cannot be acquired.
# Usage: run_integration_tests_with_protocol <pr_number>
run_integration_tests_with_protocol() {
  local pr_number="$1"
  local lock_owner="prauto-${PRAUTO_WORKER_ID}"
  local lock_url="http://localhost:9221/lock"

  # Check if lock endpoint is reachable
  if ! curl -s --connect-timeout 2 "${lock_url}/status" >/dev/null 2>&1; then
    info "Dev-env lock endpoint not reachable. Skipping integration tests."
    return 0
  fi

  # Acquire lock
  local lock_code
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${lock_url}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\", \"message\": \"prauto integration tests for PR #${pr_number}\"}")

  if [[ "$lock_code" != "200" ]]; then
    info "Could not acquire dev-env lock (HTTP ${lock_code}). Skipping integration tests."
    return 0
  fi
  info "Dev-env lock acquired for integration tests."

  # Run integration tests (conftest.py handles dummy-data resets via Python utilities)
  info "Running integration tests..."
  local integ_output integ_exit=0
  integ_output=$(DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1 uv run pytest tests/integration/ --tb=short 2>&1) || integ_exit=$?

  # Release lock
  curl -s -X POST "${lock_url}/release" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
  info "Dev-env lock released."

  # Post results
  post_test_results_comment "$pr_number" "Integration" "$integ_exit" "$integ_output"
  info "Integration test results posted on PR #${pr_number} (exit: ${integ_exit})."
}

# Fetch the approved plan text from GitHub issue comments.
# Returns the body of the latest plan comment posted by this worker,
# scoped to the current lifecycle (after the last prauto:ready label event).
# Usage: fetch_approved_plan <issue_number>
# Requires: READY_LABEL_TIMESTAMP set (via get_ready_label_timestamp)
# Sets: APPROVED_PLAN_TEXT
fetch_approved_plan() {
  local issue_number="$1"
  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"

  APPROVED_PLAN_TEXT=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq '.comments' 2>/dev/null \
    | jq -r --arg prefix "$plan_prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts) | select(.body | startswith($prefix))] | last | .body // ""
    ') || APPROVED_PLAN_TEXT=""

  # Strip the prauto header and metadata, keep the plan content
  if [[ -n "$APPROVED_PLAN_TEXT" ]]; then
    # Extract everything after "## Implementation Plan" header
    local plan_body
    plan_body=$(echo "$APPROVED_PLAN_TEXT" | sed -n '/^## Implementation Plan$/,$ p' | tail -n +2)
    # Strip trailing footer (everything after the LAST ---, which separates
    # the analysis output from the approval prompt).  The analysis output may
    # contain its own --- separators, so we must not cut at the first one.
    plan_body=$(echo "$plan_body" | awk '
      { lines[NR] = $0 }
      /^---$/ { last_sep = NR }
      END {
        end = (last_sep > 0) ? last_sep - 1 : NR
        for (i = 1; i <= end; i++) print lines[i]
      }')
    if [[ -n "$plan_body" ]]; then
      APPROVED_PLAN_TEXT="$plan_body"
    fi
  fi
}

# Run integration tests in a fix loop: test → Claude fix → re-test (up to N retries).
# Follows the dev-env lock protocol. Skips gracefully if dev-env is not reachable.
# Usage: run_integration_test_fix <issue_number> <branch>
run_integration_test_fix() {
  local issue_number="$1"
  local branch="$2"

  # Skip if no integration tests exist
  if [[ ! -d "tests/integration" ]]; then
    info "No tests/integration/ directory. Skipping integration test fix loop."
    return 0
  fi

  local lock_owner="prauto-${PRAUTO_WORKER_ID}"
  local lock_url="http://localhost:9221/lock"
  local max_retries="${PRAUTO_INTEGRATION_FIX_MAX_RETRIES:-2}"

  # Check if lock endpoint is reachable
  if ! curl -s --connect-timeout 2 "${lock_url}/status" >/dev/null 2>&1; then
    info "Dev-env lock endpoint not reachable. Skipping integration test fix loop."
    return 0
  fi

  # Acquire lock
  local lock_code
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${lock_url}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\", \"message\": \"prauto integration fix for issue #${issue_number}\"}")

  if [[ "$lock_code" != "200" ]]; then
    info "Could not acquire dev-env lock (HTTP ${lock_code}). Skipping integration test fix loop."
    return 0
  fi
  info "Dev-env lock acquired for integration test fix loop."

  # Set up .venv if needed
  if [[ -f "pyproject.toml" ]]; then
    uv sync 2>&1 || warn "uv sync failed — integration tests may not run correctly."
  fi

  local attempt integ_output integ_exit
  for (( attempt = 1; attempt <= max_retries; attempt++ )); do
    info "Integration test fix loop: attempt ${attempt}/${max_retries}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — integration test fix loop: attempt ${attempt}/${max_retries}" \
      2>/dev/null || warn "Failed to post integration fix comment on issue #${issue_number}."

    # Run integration tests (conftest.py handles dummy-data resets via Python utilities)
    integ_exit=0
    integ_output=$(DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1 uv run pytest tests/integration/ --tb=short 2>&1) || integ_exit=$?

    if [[ "$integ_exit" -eq 0 ]]; then
      info "Integration tests passed on attempt ${attempt}."
      break
    fi

    info "Integration tests failed (exit ${integ_exit}) on attempt ${attempt}/${max_retries}."

    if [[ "$attempt" -lt "$max_retries" ]]; then
      # Invoke Claude to fix integration test failures
      info "Invoking Claude to fix integration test failures..."
      run_integration_fix_session "$issue_number" "$branch" "$integ_output"
    else
      info "Max integration fix retries reached. Proceeding with current state."
    fi
  done

  # Release lock
  curl -s -X POST "${lock_url}/release" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
  info "Dev-env lock released after integration test fix loop."
}

# Combined helper: implement → integration test fix loop → finalize PR.
# Usage: implement_and_finalize <issue_number> <branch> <plan> <issue_title>
implement_and_finalize() {
  local issue_number="$1" branch="$2" plan="$3" issue_title="$4"
  # Post implementation start comment (not idempotent — each attempt is a new marker)
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — implementation starting" \
    2>/dev/null || warn "Failed to post implementation start comment on issue #${issue_number}."
  run_implementation "$issue_number" "$branch" "$plan"
  run_integration_test_fix "$issue_number" "$branch"
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}

# Phase: analysis — run analysis, post plan, auto-proceed for minor changes.
# Usage: handle_phase_analysis <issue_number> <issue_title> <branch>
handle_phase_analysis() {
  local issue_number="$1" issue_title="$2" branch="$3"

  # Fetch issue body for analysis prompt and change-size detection
  local issue_body_raw
  issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json body --jq '.body // ""' 2>/dev/null || echo "")
  # Re-run analysis from scratch (cheap)
  if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw"; then
    warn "Analysis failed for issue #${issue_number}. Will retry next heartbeat."
    return 0
  fi
  local change_size
  change_size=$(extract_change_size "$issue_body_raw")
  post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size"
  if [[ "$change_size" != "minor" ]]; then
    info "Plan posted for ${change_size} change. Waiting for approval."
    return 0
  fi
  # Fall through to implementation + integration fix + PR
  implement_and_finalize "$issue_number" "$branch" "$ANALYSIS_OUTPUT" "$issue_title"
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
    implement_and_finalize "$issue_number" "$branch" "$APPROVED_PLAN_TEXT" "$issue_title"
  elif [[ "$approval_status" -eq 2 ]]; then
    # Counter-proposal — respond to feedback, then revise plan
    info "Counter-proposal received. Revising plan..."
    fetch_approved_plan "$issue_number"
    # Generate and post response to feedback before re-analysis
    generate_feedback_response "$issue_number" "$issue_title" "$COUNTER_PROPOSAL" "$APPROVED_PLAN_TEXT"
    post_feedback_response_comment "$issue_number" "$FEEDBACK_RESPONSE_TEXT"
    # Fetch issue body for re-analysis (analysis needs issue body as context)
    local issue_body_raw
    issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || echo "")
    if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw" "$COUNTER_PROPOSAL" "$APPROVED_PLAN_TEXT"; then
      warn "Re-analysis failed for issue #${issue_number}. Will retry next heartbeat."
      return 0
    fi
    local change_size
    change_size=$(extract_change_size "$issue_body_raw")
    # Derive plan revision from GitHub comment count (SSOT)
    get_plan_revision_from_github "$issue_number"
    post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size" "$GITHUB_PLAN_REVISION"
    # Stay in plan-approval phase
    info "Revised plan (rev ${GITHUB_PLAN_REVISION}) posted. Waiting for approval."
    return 0
  elif [[ "$approval_status" -eq 3 ]]; then
    # Plan comment missing — re-run analysis from GitHub state
    info "Plan comment missing on issue #${issue_number}. Re-running analysis..."
    # Fetch issue body for re-analysis (analysis needs issue body as context)
    local issue_body_raw
    issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || echo "")
    if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw"; then
      warn "Re-analysis failed for issue #${issue_number}. Will retry next heartbeat."
      return 0
    fi
    local change_size
    change_size=$(extract_change_size "$issue_body_raw")
    post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size"
    if [[ "$change_size" != "minor" ]]; then
      info "Plan re-posted for ${change_size} change. Waiting for approval."
      return 0
    fi
    # Minor → proceed to implementation (same as approval path)
    implement_and_finalize "$issue_number" "$branch" "$ANALYSIS_OUTPUT" "$issue_title"
  else
    # No response yet — just wait (don't bump retries)
    info "Still waiting for plan approval on issue #${issue_number}."
    return 0
  fi
}

# Phase: implementation — start fresh implementation, finalize PR.
# Usage: handle_phase_implementation <issue_number> <issue_title> <branch>
handle_phase_implementation() {
  local issue_number="$1" issue_title="$2" branch="$3"

  # Fetch the approved plan from GitHub for context (issue body is not needed here)
  fetch_approved_plan "$issue_number"
  implement_and_finalize "$issue_number" "$branch" "$APPROVED_PLAN_TEXT" "$issue_title"
}

# Phase: pr — just push + create PR + labels.
# Usage: handle_phase_pr <issue_number> <issue_title> <branch>
handle_phase_pr() {
  local issue_number="$1" issue_title="$2" branch="$3"
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}
