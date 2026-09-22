# shellcheck shell=bash
# Phase orchestration sits on top of the cluster-lifecycle and regression
# layers and sources them here, so every caller gets a complete set from one
# source line. Both carry include guards, so loading them again is a no-op.
_PRAUTO_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=dev-env.sh
source "${_PRAUTO_LIB_DIR}/dev-env.sh"
# shellcheck source=regression.sh
source "${_PRAUTO_LIB_DIR}/regression.sh"
# Phase handlers for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh, state.sh, quota.sh, issues.sh, agent.sh, git-ops.sh, pr.sh
#           all sourced, config loaded.
# All handlers accept (issue_number, issue_title, branch).

# The heartbeat owns the lifecycle of a cluster it provisions. These globals
# are read by heartbeat.sh's EXIT trap so even a later test/agent failure gets
# a best-effort teardown, while a pre-existing healthy cluster is left alone.
DEV_ENV_PROVISIONED=false
DEV_ENV_PROVISIONED_ENV_FILE=""
DEV_ENV_TEARDOWN_ATTEMPTED=false
# Durable marker for a provisioned-but-not-yet-torn-down cluster. provision_dev_env
# writes it; teardown_provisioned_dev_env deletes it only after uninstall.sh actually
# succeeds. A crash (SIGKILL/OOM, skipping the EXIT trap) or a failed uninstall.sh
# leaves it behind for a later heartbeat's recover_orphaned_dev_env() to act on —
# the in-memory globals above are lost the moment the process dies, this is not.
DEV_ENV_STATE_FILE="${PRAUTO_DIR}/state/dev-env-provisioned.json"
# Executor-observed passing cluster stages in this heartbeat, one
# "<stage><TAB><commit sha>" line each. In-process only: never read from a
# file, the worktree, or agent output. It is the earlier-same-heartbeat-pass
# basis for the post-PR environmental-flake exception.
HEARTBEAT_STAGE_PASSES=""
# Commit of the branch API / frontend artifact this heartbeat last deployed
# successfully from a clean tree; empty when unknown, failed, or superseded. A
# pass is evidence only for the artifact it actually exercised.
DEPLOYED_API_SHA=""
DEPLOYED_FRONTEND_SHA=""

# Process-group id of the install.sh run provision_dev_env most recently
# backgrounded. Set immediately after it is backgrounded and cleared just
# after wait_with_group_backstop returns. heartbeat.sh's EXIT trap reads it:
# install.sh's job runs in its
# OWN process group (created by `set -m`), so if the heartbeat process itself
# is killed while still blocked inside that wait, control jumps straight to
# the trap without ever reaching the post-wait clear — the group would
# otherwise be silently orphaned and keep running (and, with it, helm and any
# credential-helper grandchild) after the heartbeat that launched it is gone.
PROVISION_PGID=""
PROVISION_LEADER_PID=""
# A gated wrapper uses FD 9 only until the parent verifies its private PGID.
# heartbeat.sh closes that FD on INT/TERM/EXIT so an unverified wrapper exits
# before it can exec a provisioning or health-check command.
CONTAINMENT_GATE_FD_OPEN=false
CONTAINMENT_GATE_WRAPPER_PID=""

# checkpoint_branch <issue_number> <branch>
# Persist committed progress before a worker worktree is removed, then expose
# the branch and commit links on GitHub. Every operation is best-effort so a
# transient GitHub/SSH failure does not turn a resumable worker pause into a
# terminal heartbeat failure.
checkpoint_branch() {
  local issue_number="$1" branch="$2"
  if ! push_checkpoint_branch "$branch"; then
    return 0
  fi
  link_branch_to_issue "$issue_number" "$branch" || true
  publish_commit_checkpoints "$issue_number" "$branch" || true
}

# branch_is_code_affecting <branch>
# The exemption list is deliberately narrow.  A path not listed here, an empty
# diff, or a git failure is code-affecting: workers never get to self-exempt.
branch_is_code_affecting() {
  local branch="$1" paths path
  paths=$(git diff --name-only "origin/${PRAUTO_BASE_BRANCH}...${branch}" 2>/dev/null) || return 0
  [[ -n "$paths" ]] || return 0
  while IFS= read -r path; do
    case "$path" in
      *.md|docs/*|spec/*|scaffold/*|.agents/skills/*|.claude/agents/*|.codex/agents/*|.prauto/prompts/*|.prauto/README.md|.prauto/config*.env.example|plugins/*)
        ;;
      *) return 0 ;;
    esac
  done <<< "$paths"
  return 1
}

regression_set_wip() {
  local issue_number="$1" branch="${2:-}"
  if ! gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_REVIEW" \
    --add-label "$PRAUTO_GITHUB_LABEL_WIP" 2>/dev/null; then
    warn "Could not establish prauto:wip on issue #${issue_number}."
    return 1
  fi
  if [[ -n "$branch" ]] && ! set_pr_wip_label "$branch"; then
    warn "Could not establish prauto:wip on the PR for ${branch}."
    return 1
  fi
  return 0
}

# Tracks whether this dispatch already handed its attempt back. Dispatch-scoped,
# NOT wake-scoped: heartbeat.sh resets it per claimed issue beside
# RETRY_COUNT_CONSUMED, because one outage blocks every issue in a wake and each
# must be able to refund its own dispatch. The reasons themselves go to durable
# per-issue state, not to a shell variable: the abandonment that reads them runs
# in a LATER heartbeat process.
REGRESSION_BLOCK_REFUNDED=false

regression_blocked() {
  local issue_number="$1" reason="$2" branch="${3:-}"
  record_blocked_reason "$issue_number" "$reason"
  # An attempt the infrastructure never let start is not an attempt at the issue.
  # Hand it back through the existing refund path, which keeps its own
  # PRAUTO_MAX_REFUNDS_PER_JOB cap — a persistent outage still abandons, only a
  # transient one stops eating the budget. Once per dispatch: this dispatch
  # consumed one retry, so it can return at most one, however many stages block.
  if [[ "$REGRESSION_BLOCK_REFUNDED" != true ]]; then
    REGRESSION_BLOCK_REFUNDED=true
    refund_retry_count "$issue_number" \
      || warn "Could not refund the retry count for #${issue_number}; this attempt stays counted."
  fi
  regression_set_wip "$issue_number" "$branch" || return 1
  prauto_issue_comment "$issue_number" \
    "Regression blocked by infrastructure/setup: ${reason}. The PR remains in prauto:wip and will retry on a later heartbeat."
}

regression_ready() {
  local issue_number="$1" branch="${2:-}"
  # Move the PR first; if the issue transition then fails, restore the PR to
  # WIP. Completion is never allowed with only one side marked ready.
  if [[ -n "$branch" ]] && ! set_pr_review_label "$branch"; then
    warn "Could not establish prauto:review on the PR for ${branch}."
    return 1
  fi
  if ! gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null; then
    warn "Could not establish prauto:review on issue #${issue_number}."
    [[ -n "$branch" ]] && set_pr_wip_label "$branch" || true
    return 1
  fi
  # Best-effort only: never lets a reviewer-request failure flip a ready PR
  # back to unready. set_pr_review_label (above) already populated BRANCH_PR_NUMBER.
  if [[ -n "${PRAUTO_REVIEWER:-}" ]] && [[ -n "$branch" ]] && [[ -n "${BRANCH_PR_NUMBER:-}" ]]; then
    gh pr edit "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
      --add-reviewer "$PRAUTO_REVIEWER" 2>/dev/null \
      || warn "Could not request reviewer ${PRAUTO_REVIEWER} on PR #${BRANCH_PR_NUMBER}."
  fi
  return 0
}

# finalize_issue_pr <branch> <issue_number> <issue_title>
# The PR is intentionally created before the full regression.  It remains WIP
# until the exact pushed head passes the centralized readiness gate.
finalize_issue_pr() {
  local branch="$1" issue_number="$2" issue_title="$3"
  push_branch "$branch"
  link_branch_to_issue "$issue_number" "$branch" || true
  publish_commit_checkpoints "$issue_number" "$branch" || true
  create_or_update_pr "$issue_number" "$issue_title" "$branch"
  if ! regression_set_wip "$issue_number" "$branch"; then
    prauto_issue_comment "$issue_number" \
      "Regression blocked: GitHub label/API state could not be established. Retrying on a later heartbeat."
    return 0
  fi
  if run_post_pr_regression "$issue_number" "$branch"; then
    if regression_ready "$issue_number" "$branch"; then
      complete_job "$issue_number"
    else
      prauto_issue_comment "$issue_number" \
        "Regression passed but GitHub readiness labels could not be updated; retrying on a later heartbeat."
    fi
  fi
}

# tail_chars <text> <max_chars> — keep the last N characters (failure summaries
# print last).
tail_chars() {
  local text="$1" max_chars="$2"
  if [[ ${#text} -le $max_chars ]]; then printf '%s' "$text"; return 0; fi
  printf '(truncated — last %s characters)\n%s' "$max_chars" "${text: -max_chars}"
}

# fetch_approved_plan <issue_number>
# Fetch the latest plan comment's plan body (scoped to lifecycle). Sets
# APPROVED_PLAN_TEXT.
fetch_approved_plan() {
  local issue_number="$1"
  local plan_prefix="$(prauto_comment_prefix)Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"

  APPROVED_PLAN_TEXT=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq -r --arg prefix "$plan_prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts) | select(.body | startswith($prefix))] | last | .body // ""
    ') || APPROVED_PLAN_TEXT=""

  if [[ -n "$APPROVED_PLAN_TEXT" ]]; then
    local plan_body
    plan_body=$(printf '%s' "$APPROVED_PLAN_TEXT" | sed -n '/^## Implementation Plan$/,$ p' | tail -n +2)
    plan_body=$(printf '%s' "$plan_body" | awk '
      { lines[NR] = $0 }
      /^---$/ { last_sep = NR }
      END { end = (last_sep > 0) ? last_sep - 1 : NR; for (i = 1; i <= end; i++) print lines[i] }')
    [[ -n "$plan_body" ]] && APPROVED_PLAN_TEXT="$plan_body"
  fi
}

# run_integration_test_fix <issue_number> <branch>
# Integration test fix loop: deploy branch API, run groups, fix via worker, up to
# N retries, under the dev-env lock. Skips gracefully when the cluster is absent.
run_integration_test_fix() {
  local issue_number="$1" branch="$2"
  [[ ! -d "tests/integration" ]] && { info "No tests/integration/. Skipping integration fix loop."; return 0; }

  local lock_owner="prauto-${PRAUTO_WORKER_ID}"
  local max_retries="${PRAUTO_INTEGRATION_FIX_MAX_RETRIES:-2}"

  if ! resolve_dev_env; then info "Dev-env file not found. Skipping integration fix loop."; return 0; fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then info "Dev-env unhealthy. Skipping integration fix loop."; return 0; fi
  # Same primitives as acquire_required_dev_lock: the /health probe with -f (the
  # service answers 404 to anything else, which a bare `curl -s` reads as
  # reachable), and dev_lock_acquire, so this second holder of the SAME cluster
  # lock cannot run under weaker rules than the required one. The only
  # difference is what a failure means here — skip, not block.
  if ! dev_lock_endpoint_reachable; then
    warn "Dev-env lock endpoint not reachable (${DEV_LOCK_HEALTH_URL}). Skipping integration fix loop."
    return 0
  fi

  local lock_code
  lock_code=$(dev_lock_acquire "$lock_owner" "prauto integration fix for issue #${issue_number}")
  if [[ "$lock_code" == 409 ]]; then
    # Reclaim this worker's own leaked lock on proof of the stored token — never
    # on a name match. See acquire_required_dev_lock for why.
    local stale_token release_code
    stale_token=$(dev_lock_load_token "$lock_owner")
    if [[ -n "$stale_token" ]]; then
      release_code=$(dev_lock_release_request "$lock_owner" "$stale_token")
      dev_lock_clear_token
      if [[ "$release_code" == 200 ]]; then
        warn "Reclaimed a stale dev-env lock left by this worker (owner=${lock_owner})."
        lock_code=$(dev_lock_acquire "$lock_owner" "prauto integration fix for issue #${issue_number}")
      fi
    fi
  fi
  if [[ "$lock_code" != "200" ]]; then info "Could not acquire dev-env lock. Skipping."; return 0; fi
  # Register the lock with the shared owner global so release_required_dev_lock —
  # including the heartbeat EXIT trap's call — can release it if this loop dies.
  REQUIRED_LOCK_OWNER="$lock_owner"
  dev_lock_adopt_token "$lock_owner" || true
  # Fail closed, exactly as acquire_required_dev_lock does: this stage is one of
  # the two longest cluster runs, and a lease it cannot extend expires partway
  # through while the executor still believes it holds the cluster. Proceeding on
  # a warning would reintroduce the concurrent-run hazard the lease prevents.
  if ! dev_lock_start_renewer "$lock_owner"; then
    release_required_dev_lock
    warn "Dev-env lock cannot be renewed (no acquisition token). Skipping integration fix loop."
    return 0
  fi
  info "Dev-env lock acquired for integration test fix loop."

  # A plain `if`: as an `&& ... ||` chain this warned "uv sync failed" whenever
  # pyproject.toml was merely absent, and let uv's output escape to the harness's
  # own stdout instead of being captured like every other invocation.
  if [[ -f "pyproject.toml" ]]; then
    local uv_sync_output
    uv_sync_output=$(uv sync 2>&1) || warn "uv sync failed: ${uv_sync_output}"
  fi

  if diff_touches src/api/ src/backend/ src/shared/; then
    if ! deploy_branch_api "$DEV_ENV_FILE"; then
      warn "Branch API deploy failed. Skipping the integration test fix loop."
      release_required_dev_lock
      return 0
    fi
  fi

  local max_flake_reruns="${PRAUTO_INTEGRATION_FLAKE_RERUNS:-2}"
  if [[ ! "$max_flake_reruns" =~ ^[0-9]+$ ]]; then
    [[ -n "${PRAUTO_INTEGRATION_FLAKE_RERUNS:-}" ]] && \
      warn "Invalid PRAUTO_INTEGRATION_FLAKE_RERUNS='${PRAUTO_INTEGRATION_FLAKE_RERUNS}'; using the default (2)."
    max_flake_reruns=2
  fi

  # attempt only advances when a worker is (or would be) dispatched; a flake-only
  # rerun (below) repeats the groups without spending a fix attempt. flake_reruns
  # is a separate, smaller budget for exactly that case. is_flake_rerun lets the
  # single top-of-loop heartbeat comment note a rerun instead of a second,
  # duplicate comment. need_pre_probe is set after a worker session (whether or
  # not it redeployed) so the NEXT iteration re-checks cluster health before
  # trusting it — the fix session's own wall-clock time can make the last
  # confirmed-healthy check stale by the time that iteration actually runs.
  local attempt=1 quota_paused=false
  local flake_reruns=0
  local is_flake_rerun=false
  local need_pre_probe=false
  while [[ "$attempt" -le "$max_retries" ]]; do
    if [[ "$need_pre_probe" == true ]]; then
      need_pre_probe=false
      if ! dev_env_probe_healthy "$DEV_ENV_FILE"; then
        warn "Dev-env unhealthy ahead of attempt ${attempt} (stale since the last fix session/redeploy). Ending the integration test fix loop without dispatching a worker."
        release_required_dev_lock
        return 0
      fi
    fi

    # Exactly one comment per loop iteration: a flake rerun folds its
    # (fixed-string, numbers-only) note into this same comment instead of
    # posting a second one — see the flake-classification block below, which
    # only sets is_flake_rerun and logs locally.
    local attempt_comment="Heartbeat — integration test fix loop: attempt ${attempt}/${max_retries}"
    [[ "$is_flake_rerun" == true ]] && attempt_comment="${attempt_comment}, flake rerun ${flake_reruns}/${max_flake_reruns}"
    is_flake_rerun=false
    prauto_issue_comment "$issue_number" "$attempt_comment"

    info "Integration test fix loop: attempt ${attempt}/${max_retries}"
    local tested_head
    tested_head=$(executor_test_head)
    run_integration_groups "$DEV_ENV_FILE"
    # Executor-observed passes only, and only as evidence for the artifact that
    # ran: the clean tested commit must be the deployed branch API and HEAD must
    # not have moved during the run. A missing group directory is a skip.
    if [[ -n "$tested_head" && "$tested_head" == "${DEPLOYED_API_SHA:-}" && "$(current_head)" == "$tested_head" ]]; then
      [[ -d tests/integration/spot && "$INTEG_SPOT_EXIT" -eq 0 ]] && record_heartbeat_stage_pass "Integration (spot)" "$tested_head"
      [[ -d tests/integration/api_wired && "$INTEG_API_WIRED_EXIT" -eq 0 ]] && record_heartbeat_stage_pass "Integration (api-wired)" "$tested_head"
    fi
    [[ "$INTEG_EXIT" -eq 0 ]] && { info "Integration tests passed on attempt ${attempt}."; break; }

    info "Integration tests failed (spot: ${INTEG_SPOT_EXIT}, api-wired: ${INTEG_API_WIRED_EXIT})."

    # A session-start health-gate abort (require_server failed before any test
    # ran) is an infrastructure condition, never a branch failure or a flake.
    # Checked explicitly, before flake classification, so it gets its own
    # correctly-labeled outcome (spec/AI_PRAUTO.md's cluster-abort handling)
    # rather than merely falling through: it extracts no per-test failures, so
    # stage_failures_are_transport_flakes below would reject it as non-flake
    # too, but silently landing in the ordinary worker-dispatch path would
    # spend a fix attempt at a cluster that cannot run tests at all.
    local health_abort=""
    [[ "$INTEG_SPOT_EXIT" -ne 0 ]] && is_cluster_health_abort "$INTEG_SPOT_OUTPUT" && health_abort="Integration (spot)"
    if [[ "$INTEG_API_WIRED_EXIT" -ne 0 ]] && is_cluster_health_abort "$INTEG_API_WIRED_OUTPUT"; then
      health_abort="${health_abort:+${health_abort}, }Integration (api-wired)"
    fi
    if [[ -n "$health_abort" ]]; then
      warn "Integration harness health gate aborted before any test ran (${health_abort}). Ending the integration test fix loop without dispatching a worker (infrastructure)."
      release_required_dev_lock
      return 0
    fi

    # Environmental transport-flake convergence (spec/AI_PRAUTO.md's flake
    # exception, conditions 1/2/3/5 — no fix-path condition 4 applies here,
    # since no fix is being classified): every failed group's own failures must
    # be allowlisted transport signatures with no disqualifier, and the
    # post-stage health probe (never provisions) must pass. When both hold, the
    # executor reruns the groups itself on the next iteration instead of
    # dispatching a worker. This never reads worker attestation as pass
    # evidence — only Stage 5's targeted retry does that.
    local flake_only=true
    [[ "$INTEG_SPOT_EXIT" -ne 0 ]] && { stage_failures_are_transport_flakes "Integration (spot)" "$INTEG_SPOT_OUTPUT" || flake_only=false; }
    [[ "$INTEG_API_WIRED_EXIT" -ne 0 ]] && { stage_failures_are_transport_flakes "Integration (api-wired)" "$INTEG_API_WIRED_OUTPUT" || flake_only=false; }
    if [[ "$flake_only" == true ]] && dev_env_probe_healthy "$DEV_ENV_FILE"; then
      local flake_stage_names="" flake_categories=""
      if [[ "$INTEG_SPOT_EXIT" -ne 0 ]]; then
        flake_stage_names="Integration (spot)"
        flake_categories="Integration (spot): $(stage_transport_flake_categories "Integration (spot)" "$INTEG_SPOT_OUTPUT")"
      fi
      if [[ "$INTEG_API_WIRED_EXIT" -ne 0 ]]; then
        flake_stage_names="${flake_stage_names:+${flake_stage_names}, }Integration (api-wired)"
        flake_categories="${flake_categories:+${flake_categories}; }Integration (api-wired): $(stage_transport_flake_categories "Integration (api-wired)" "$INTEG_API_WIRED_OUTPUT")"
      fi
      # Logged only, not posted: the next iteration's single top-of-loop
      # comment above already announces the rerun with fixed strings and
      # numeric counters, so the free-form stage/category text stays local
      # instead of duplicating into a second public comment.
      if [[ "$flake_reruns" -lt "$max_flake_reruns" ]]; then
        flake_reruns=$((flake_reruns + 1))
        info "Environmental transport flake in ${flake_stage_names} (${flake_categories}) — rerunning without a fix session (flake rerun ${flake_reruns}/${max_flake_reruns})."
        is_flake_rerun=true
        continue
      fi
      info "Flake-only integration failures in ${flake_stage_names} exhausted the flake rerun budget (${max_flake_reruns}). Proceeding without dispatching a worker."
      break
    fi

    if [[ "$attempt" -lt "$max_retries" ]]; then
      local failed_stages=""
      [[ "$INTEG_SPOT_EXIT" -ne 0 ]] && failed_stages="Integration (spot)"
      [[ "$INTEG_API_WIRED_EXIT" -ne 0 ]] && failed_stages="${failed_stages:+${failed_stages}, }Integration (api-wired)"
      local pre_fix_head post_fix_head
      pre_fix_head=$(current_head)
      run_integration_fix_session "$issue_number" "$branch" "$failed_stages" "$INTEG_OUTPUT" pre-pr
      post_fix_head=$(current_head)
      [[ "$post_fix_head" == "$pre_fix_head" ]] && info "fix session produced no commit"
      checkpoint_branch "$issue_number" "$branch"
      # No PR exists yet at this point in the pipeline, so the next wake still
      # derives phase "implementation" and the quota-resume dispatch in
      # heartbeat.sh can resume this exact session — safe to post the marker.
      if [[ "$AGENT_STATUS" == "quota" ]]; then
        warn "Issue #${issue_number}: integration fix worker died on a quota/session limit (${ACTIVE_AGENT}). Pausing."
        post_quota_paused_comment "$issue_number" "$ACTIVE_AGENT" "$AGENT_SESSION_ID"
        quota_paused=true
        break
      fi
      # A commit that touches src/api/, src/backend/, or src/shared/ must be
      # redeployed before the rerun below, or the rerun would exercise the
      # unchanged pre-fix image while DEPLOYED_API_SHA no longer matches the
      # commit it actually tests, and the rerun would never be able to record
      # a pass. deploy_branch_api builds from the worktree's current
      # (already-committed) source directly, so it needs no push and runs
      # regardless of whether checkpoint_branch's best-effort push succeeded.
      if [[ "$post_fix_head" != "$pre_fix_head" ]] && diff_touches src/api/ src/backend/ src/shared/; then
        if ! deploy_branch_api "$DEV_ENV_FILE"; then
          warn "Branch API redeploy after the fix session failed. Skipping the rest of the integration test fix loop."
          release_required_dev_lock
          return 0
        fi
      fi
      need_pre_probe=true
    else
      info "Max integration fix retries reached. Proceeding with current state."
    fi
    attempt=$((attempt + 1))
  done

  release_required_dev_lock
  info "Dev-env lock released after integration test fix loop."
  [[ "$quota_paused" == "true" ]] && return 1
  return 0
}

# report_e2e_results <issue_number> <branch> <exit_code> <output>
report_e2e_results() {
  local issue_number="$1" branch="$2" exit_code="$3" output="$4"
  get_pr_number_for_branch "$branch"
  if [[ -n "$BRANCH_PR_NUMBER" ]]; then
    post_test_results_comment "$BRANCH_PR_NUMBER" "E2E" "$exit_code" "$output"
    return 0
  fi
  local status_label="Passed"
  [[ "$exit_code" -ne 0 ]] && status_label="Failed (exit code ${exit_code})"
  prauto_issue_comment "$issue_number" "Heartbeat — E2E test results: ${status_label}"
}

# run_e2e_test_fix <issue_number> <branch>
run_e2e_test_fix() {
  local issue_number="$1" branch="$2"
  [[ ! -d "tests/e2e" ]] && { info "No tests/e2e/. Skipping E2E stage."; return 0; }
  command -v pnpm >/dev/null 2>&1 || { info "pnpm not available. Skipping E2E stage."; return 0; }
  if ! diff_touches src/frontend/ tests/e2e/ src/api/; then
    info "Diff touches no UI/E2E/API paths. Skipping E2E stage."
    return 0
  fi

  local lock_owner="prauto-${PRAUTO_WORKER_ID}"
  local max_retries="${PRAUTO_E2E_FIX_MAX_RETRIES:-3}"
  local max_flake_reruns="${PRAUTO_E2E_FLAKE_RERUNS:-2}"
  if ! resolve_dev_env; then info "Dev-env file not found. Skipping E2E stage."; return 0; fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then info "Dev-env unhealthy. Skipping E2E stage."; return 0; fi
  # Same primitives as acquire_required_dev_lock: the /health probe with -f (the
  # service answers 404 to anything else, which a bare `curl -s` reads as
  # reachable), and dev_lock_acquire, so this second holder of the SAME cluster
  # lock cannot run under weaker rules than the required one. The only
  # difference is what a failure means here — skip, not block.
  if ! dev_lock_endpoint_reachable; then
    warn "Dev-env lock endpoint not reachable (${DEV_LOCK_HEALTH_URL}). Skipping E2E stage."
    return 0
  fi

  local lock_code
  lock_code=$(dev_lock_acquire "$lock_owner" "prauto E2E for issue #${issue_number}")
  if [[ "$lock_code" == 409 ]]; then
    # Reclaim this worker's own leaked lock on proof of the stored token — never
    # on a name match. See acquire_required_dev_lock for why.
    local stale_token release_code
    stale_token=$(dev_lock_load_token "$lock_owner")
    if [[ -n "$stale_token" ]]; then
      release_code=$(dev_lock_release_request "$lock_owner" "$stale_token")
      dev_lock_clear_token
      if [[ "$release_code" == 200 ]]; then
        warn "Reclaimed a stale dev-env lock left by this worker (owner=${lock_owner})."
        lock_code=$(dev_lock_acquire "$lock_owner" "prauto E2E for issue #${issue_number}")
      fi
    fi
  fi
  if [[ "$lock_code" != "200" ]]; then info "Could not acquire dev-env lock. Skipping E2E."; return 0; fi
  # Register the lock with the shared owner global so release_required_dev_lock —
  # including the heartbeat EXIT trap's call — can release it if this loop dies.
  REQUIRED_LOCK_OWNER="$lock_owner"
  dev_lock_adopt_token "$lock_owner" || true
  # Fail closed, exactly as acquire_required_dev_lock does: this stage is one of
  # the two longest cluster runs, and a lease it cannot extend expires partway
  # through while the executor still believes it holds the cluster. Proceeding on
  # a warning would reintroduce the concurrent-run hazard the lease prevents.
  if ! dev_lock_start_renewer "$lock_owner"; then
    release_required_dev_lock
    warn "Dev-env lock cannot be renewed (no acquisition token). Skipping E2E stage."
    return 0
  fi
  info "Dev-env lock acquired for E2E stage."

  # attempt only advances when a real (non-flake) run happens; a flake-only
  # rerun (below) repeats the suite without spending a fix attempt, mirroring
  # run_integration_test_fix's flake_reruns/is_flake_rerun split — see that
  # function's comment for why a plain `for` loop cannot express this.
  local attempt=1 e2e_output e2e_exit=0 deployed=false quota_paused=false
  local flake_reruns=0 is_flake_rerun=false
  while [[ "$attempt" -le "$max_retries" ]]; do
    local attempt_comment="Heartbeat — E2E stage: attempt ${attempt}/${max_retries}"
    [[ "$is_flake_rerun" == true ]] && attempt_comment="${attempt_comment}, flake rerun ${flake_reruns}/${max_flake_reruns}"
    is_flake_rerun=false
    info "E2E stage: attempt ${attempt}/${max_retries}"
    prauto_issue_comment "$issue_number" "$attempt_comment"

    if ! deploy_branch_frontend "$DEV_ENV_FILE"; then warn "Skipping E2E — frontend deploy failed."; break; fi
    if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1; then warn "Skipping E2E — pnpm install failed."; break; fi
    if ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
      warn "Skipping E2E — could not install Playwright Chromium."
      break
    fi
    deployed=true

    info "Running E2E tests..."
    local tested_head
    tested_head=$(executor_test_head)
    e2e_exit=0
    e2e_output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" \
      pnpm -C tests/e2e test 2>&1) || e2e_exit=$?
    if [[ "$e2e_exit" -eq 0 ]]; then
      # Evidence only when both exercised artifacts (frontend and the API it
      # calls) were deployed from the clean tested commit and HEAD is unchanged.
      if [[ -n "$tested_head" && "$tested_head" == "${DEPLOYED_FRONTEND_SHA:-}" && \
            "$tested_head" == "${DEPLOYED_API_SHA:-}" && "$(current_head)" == "$tested_head" ]]; then
        record_heartbeat_stage_pass "E2E" "$tested_head"
      fi
      info "E2E tests passed on attempt ${attempt}."
      break
    fi

    info "E2E tests failed (exit ${e2e_exit})."

    # Environmental transport-flake convergence, same allowlist/exception as
    # run_integration_test_fix (stage_failures_are_transport_flakes/
    # stage_transport_flake_categories already handle "E2E" as a playwright
    # stage). A Playwright run crosses the same GKE ingress far more times per
    # test than one integration call, so it is at least as exposed to
    # transient laptop<->cluster drops; without this check a flake here would
    # burn a fix-worker dispatch (a full rebuild + redeploy) on infrastructure
    # noise instead of source code — the same pattern that used to make the
    # integration loop hang before it got this exception.
    if stage_failures_are_transport_flakes "E2E" "$e2e_output" && dev_env_probe_healthy "$DEV_ENV_FILE"; then
      if [[ "$flake_reruns" -lt "$max_flake_reruns" ]]; then
        flake_reruns=$((flake_reruns + 1))
        info "Environmental transport flake in E2E ($(stage_transport_flake_categories "E2E" "$e2e_output")) — rerunning without a fix session (flake rerun ${flake_reruns}/${max_flake_reruns})."
        is_flake_rerun=true
        continue
      fi
      info "Flake-only E2E failures exhausted the flake rerun budget (${max_flake_reruns}). Proceeding without dispatching a worker."
      break
    fi

    if [[ "$attempt" -lt "$max_retries" ]]; then
      run_e2e_fix_session "$issue_number" "$branch" "$(tail_chars "$e2e_output" 28000)"
      checkpoint_branch "$issue_number" "$branch"
      # No PR exists yet at this point in the pipeline, so the next wake still
      # derives phase "implementation" and the quota-resume dispatch in
      # heartbeat.sh can resume this exact session — safe to post the marker.
      if [[ "$AGENT_STATUS" == "quota" ]]; then
        warn "Issue #${issue_number}: E2E fix worker died on a quota/session limit (${ACTIVE_AGENT}). Pausing."
        post_quota_paused_comment "$issue_number" "$ACTIVE_AGENT" "$AGENT_SESSION_ID"
        quota_paused=true
        break
      fi
    else
      info "Max E2E fix retries reached."
    fi
    attempt=$((attempt + 1))
  done

  release_required_dev_lock
  info "Dev-env lock released after E2E stage."

  [[ "$deployed" == "true" ]] && report_e2e_results "$issue_number" "$branch" "$e2e_exit" "$e2e_output"
  [[ "$quota_paused" == "true" ]] && return 1
  return 0
}

# implementation_escalated <impl_output>
# Returns 0 when the implementation session reported ESCALATED. Match only the
# LAST `PRAUTO_WORKFLOW_OUTCOME:` line (the template echoes the sentinel verbatim
# inside a fenced block, which a whole-output grep would false-trigger).
implementation_escalated() {
  local impl_output="$1" last_sentinel
  last_sentinel=$(printf '%s' "$impl_output" | grep -E '^PRAUTO_WORKFLOW_OUTCOME:' | tail -1) || true
  [[ "$last_sentinel" =~ ^PRAUTO_WORKFLOW_OUTCOME:[[:space:]]*ESCALATED ]]
}

# implementation_complete <impl_result>
# A successful implementation must explicitly end with COMPLETE. Exit code 0
# alone is insufficient: an agent can return empty, partial, or otherwise
# malformed text after making no usable workflow progress.
#
# Takes the agent's own result (AGENT_RESULT via IMPL_RESULT), never the merged
# AGENT_OUTPUT: that one has the session's stderr appended for diagnostics, and
# any stderr line would sit after the sentinel and fail a genuinely complete run.
implementation_complete() {
  local impl_result="$1" last_line
  last_line=$(printf '%s' "$impl_result" | sed '/^[[:space:]]*$/d' | tail -1) || true
  [[ "$last_line" == "PRAUTO_WORKFLOW_OUTCOME: COMPLETE" ]]
}

# implementation_truncated_by_agent_cli <elapsed_secs>
# True when the session ran long enough that the agent CLI must have terminated it
# on its own background-task wait ceiling, rather than the worker finishing badly.
# The CLI exits 0 in that case, so exit status alone cannot tell the two apart, and
# the workflow may have been making real progress — earlier stages commit as they
# go. That is a harness limitation, not a worker failure, and it must not consume
# the job's retry budget.
#
# Takes the executor's own wall-clock measurement (AGENT_ELAPSED_SECS via
# IMPL_ELAPSED_SECS) and nothing from the session's output. The worker runs
# unreviewed branch code as this executor's OS user, so anything it writes — its
# report, and the stderr sidecar under the state tree just as much — is
# worker-influenceable. Elapsed time is the one account of the session the worker
# cannot author. Its own stalling is still bounded: PRAUTO_MAX_REFUNDS_PER_JOB caps
# how many refunds a lifecycle can collect, and stalling to the ceiling costs it a
# full ceiling's wall-clock per attempt.
#
# A ceiling of 0 means wait indefinitely, so no truncation is possible.
implementation_truncated_by_agent_cli() {
  local elapsed_secs="$1"
  local ceiling_ms="${PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS:-14400000}"
  [[ "$elapsed_secs" =~ ^[0-9]+$ ]] || return 1
  [[ "$ceiling_ms" =~ ^[0-9]+$ ]] || return 1
  [[ "$ceiling_ms" -gt 0 ]] || return 1
  (( elapsed_secs >= ceiling_ms / 1000 ))
}

# abandon_workflow_escalation <issue_number> <impl_output>
# A wf-minimal ESCALATE halts the run at the escalating stage group, leaving a
# partial uncommitted implementation that must not reach tests or a PR.
abandon_workflow_escalation() {
  local issue_number="$1" impl_output="$2"

  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    --add-label "$PRAUTO_GITHUB_LABEL_FAILED" 2>/dev/null || true

  local details
  details=$(printf '%s' "$impl_output" | grep -vE '^PRAUTO_WORKFLOW_OUTCOME:' || true)
  details=$(scrub_secrets "$details")
  details=$(tail_chars "$details" 12000)

  prauto_issue_comment "$issue_number" \
    "Abandoning — implementation workflow escalated. Either a per-stage reviewer's findings persisted after a fix pass, or the worker could not run the required workflow loop (for example the Workflow tool was absent from its session's tool list). The report below says which. Manual intervention needed.

${details}" \
    "Failed to post workflow-escalation comment on issue #${issue_number}."
  info "Job for issue #${issue_number} abandoned (workflow escalation)."
}

# implement_and_finalize <issue_number> <branch> <plan> <issue_title>
# Dispatch the implementation (fresh or quota-resume), handle quota-death and
# ESCALATE, then integration fix → E2E → finalize PR.
#
# Quota-pause/resume: a worker that dies on a rate/session-limit exit posts a
# pause marker carrying the session id. The NEXT wake enters here with the pause
# marker present and resumes the SAME session on the SAME agent; a resume never
# posts a heartbeat (it is not a new attempt, so it does not advance the retry
# counter). Only `abandon previous session` (handled in heartbeat.sh) restarts.
implement_and_finalize() {
  local issue_number="$1" branch="$2" plan="$3" issue_title="$4"

  if has_quota_paused_comment "$issue_number"; then
    # RESUME path: continue the same session on the same agent. A resume is a
    # continuation, not a new attempt, so no heartbeat is posted (the retry
    # counter does not advance). "Resumed" is posted when the resume begins —
    # quota is confirmed reset, so announce it before the (potentially long)
    # resume run; if the resume dies on quota again, the fresh pause marker
    # that follows supersedes it.
    read_pause_marker "$issue_number"
    ACTIVE_AGENT="$PAUSED_AGENT"
    post_quota_resumed_comment "$issue_number"
    resume_agent \
      "Continue your implementation from where you left off. Complete the workflow, commit (do NOT push), and end with exactly one of these lines: PRAUTO_WORKFLOW_OUTCOME: COMPLETE or PRAUTO_WORKFLOW_OUTCOME: ESCALATED" \
      "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION:-400}" \
      "$PAUSED_SESSION_ID" "${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}" "$DENY_TOOLS" \
      "${IMPLEMENTATION_CLAUDE_ENV[@]}"
    IMPL_RESULT="$AGENT_RESULT"
    IMPL_STDERR="$AGENT_STDERR"
    IMPL_ELAPSED_SECS="$AGENT_ELAPSED_SECS"
    IMPL_OUTPUT="$AGENT_OUTPUT"
    if [[ "$AGENT_STATUS" == "quota" ]]; then
      warn "Issue #${issue_number}: resumed ${ACTIVE_AGENT} died on quota again. Re-pausing."
      checkpoint_branch "$issue_number" "$branch"
      post_quota_paused_comment "$issue_number" "$ACTIVE_AGENT" "$PAUSED_SESSION_ID"
      return 0
    fi
    if [[ "$AGENT_STATUS" != "ok" ]]; then
      warn "Issue #${issue_number}: resumed ${ACTIVE_AGENT} failed. Will retry through the normal path."
      checkpoint_branch "$issue_number" "$branch"
      post_resume_failure_restart_comment "$issue_number" "$ACTIVE_AGENT"
      return 0
    fi
  else
    # FRESH dispatch. The caller (heartbeat.sh's normal dispatch or
    # handle_phase_plan_approval) already posted the phase heartbeat — do not
    # double-post here.
    run_implementation "$issue_number" "$branch" "$plan"

    # Mid-run quota death: pause (with session id), do not finalize, do not burn
    # a retry. The next wake resumes this same session once quota resets.
    if [[ "$AGENT_STATUS" == "quota" ]]; then
      warn "Issue #${issue_number}: worker died on a quota/session limit (${ACTIVE_AGENT}). Pausing."
      checkpoint_branch "$issue_number" "$branch"
      post_quota_paused_comment "$issue_number" "$ACTIVE_AGENT" "$AGENT_SESSION_ID"
      return 0
    fi

    # A non-ok, non-quota result (auth expired, api_error, timeout, empty/wrong
    # answer) must not flow into finalize: there is no committed work to push.
    # Retry on a later wake; the heartbeat-comment retry counter governs abandon.
    if [[ "$AGENT_STATUS" != "ok" ]]; then
      warn "Issue #${issue_number}: implementation failed (${ACTIVE_AGENT}, status=${AGENT_STATUS}). Will retry next heartbeat."
      checkpoint_branch "$issue_number" "$branch"
      return 0
    fi
  fi

  if implementation_escalated "$IMPL_RESULT"; then
    warn "Implementation workflow escalated for issue #${issue_number}. Abandoning."
    checkpoint_branch "$issue_number" "$branch"
    prauto_issue_comment "$issue_number" "Heartbeat — workflow escalated"
    abandon_workflow_escalation "$issue_number" "$IMPL_OUTPUT"
    return 0
  fi

  if ! implementation_complete "$IMPL_RESULT"; then
    if implementation_truncated_by_agent_cli "$IMPL_ELAPSED_SECS"; then
      warn "Issue #${issue_number}: the agent CLI terminated the session on its background-task wait ceiling before the workflow reported an outcome. Refunding this attempt; will retry next heartbeat."
      checkpoint_branch "$issue_number" "$branch"
      refund_retry_count "$issue_number" \
        || warn "Could not refund the retry count for #${issue_number}; this attempt stays counted."
      return 0
    fi
    warn "Issue #${issue_number}: implementation returned no valid COMPLETE outcome. Will retry next heartbeat."
    checkpoint_branch "$issue_number" "$branch"
    return 0
  fi

  # Every generator owns its commit now. A clean worktree is the parent-side
  # invariant that catches a failed or over-broad stage commit before pushing.
  local dirty_worktree
  dirty_worktree=$(git status --porcelain --untracked-files=all 2>/dev/null || true)
  if [[ -n "$dirty_worktree" ]]; then
    warn "Issue #${issue_number}: implementation completed with uncommitted changes. Will retry next heartbeat."
    checkpoint_branch "$issue_number" "$branch"
    return 0
  fi

  # Selected pre-PR verification is an iteration aid; the post-PR gate below
  # remains the only readiness authority and always reruns the complete suite.
  run_pre_pr_selected_verification "$issue_number" "$branch" || return 0
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}

# handle_phase_analysis <issue_number> <issue_title> <branch>
handle_phase_analysis() {
  local issue_number="$1" issue_title="$2" branch="$3"
  local issue_body_raw
  issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json body --jq '.body // ""' 2>/dev/null || printf '')

  local plan_file="${CUR_SESSION_DIR}/plan.md"
  if has_quota_paused_comment "$issue_number"; then
    # Resume a paused analysis session (same agent), then capture its plan.md.
    read_pause_marker "$issue_number"
    ACTIVE_AGENT="$PAUSED_AGENT"
    post_quota_resumed_comment "$issue_number"
    resume_agent \
      "Continue your analysis and write the complete plan (including the metadata block) to the plan_file path named in your instructions." \
      "$ANALYSIS_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS:-100}" \
      "$PAUSED_SESSION_ID" "${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"
    ANALYSIS_SESSION_ID="$PAUSED_SESSION_ID"
    if [[ "$AGENT_STATUS" == "quota" ]]; then
      warn "Issue #${issue_number}: resumed analysis died on quota again. Re-pausing."
      post_quota_paused_comment "$issue_number" "$ACTIVE_AGENT" "$PAUSED_SESSION_ID"
      return 0
    fi
    if [[ "$AGENT_STATUS" != "ok" ]]; then
      warn "Issue #${issue_number}: resumed analysis failed. Will retry through the normal path."
      post_resume_failure_restart_comment "$issue_number" "$ACTIVE_AGENT"
      return 0
    fi
    if [[ -f "$plan_file" ]] && [[ -s "$plan_file" ]]; then
      ANALYSIS_OUTPUT=$(cat "$plan_file")
    else
      ANALYSIS_OUTPUT="$AGENT_RESULT"
    fi
  elif ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw"; then
    # Quota death mid-analysis: pause with session id, do not burn a retry.
    if [[ "$AGENT_STATUS" == "quota" ]]; then
      post_quota_paused_comment "$issue_number" "$ACTIVE_AGENT" "$AGENT_SESSION_ID"
    else
      warn "Analysis failed for issue #${issue_number}. Will retry next heartbeat."
    fi
    return 0
  fi

  local change_size
  change_size=$(resolve_change_size "$issue_body_raw" "$ANALYSIS_OUTPUT")
  post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size"
  if [[ "$change_size" != "minor" ]]; then
    info "Plan posted for ${change_size} change. Waiting for approval."
    return 0
  fi
  implement_and_finalize "$issue_number" "$branch" "$ANALYSIS_OUTPUT" "$issue_title"
}

# handle_phase_plan_approval <issue_number> <issue_title> <branch>
handle_phase_plan_approval() {
  local issue_number="$1" issue_title="$2" branch="$3"
  local approval_status=0
  COUNTER_PROPOSAL=""
  check_plan_approval "$issue_number" || approval_status=$?

  if [[ "$approval_status" -eq 0 ]]; then
    info "Plan approved. Starting implementation..."
    gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || true
    prauto_issue_comment "$issue_number" "Heartbeat — implementation starting"
    fetch_approved_plan "$issue_number"
    implement_and_finalize "$issue_number" "$branch" "$APPROVED_PLAN_TEXT" "$issue_title"
  elif [[ "$approval_status" -eq 2 ]]; then
    info "Counter-proposal received. Revising plan..."
    fetch_approved_plan "$issue_number"
    generate_feedback_response "$issue_number" "$issue_title" "$COUNTER_PROPOSAL" "$APPROVED_PLAN_TEXT"
    post_feedback_response_comment "$issue_number" "$FEEDBACK_RESPONSE_TEXT"
    local issue_body_raw
    issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || printf '')
    if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw" "$COUNTER_PROPOSAL" "$APPROVED_PLAN_TEXT"; then
      warn "Re-analysis failed for issue #${issue_number}. Will retry next heartbeat."
      return 0
    fi
    local change_size
    change_size=$(resolve_change_size "$issue_body_raw" "$ANALYSIS_OUTPUT")
    get_plan_revision_from_github "$issue_number"
    post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size" "$GITHUB_PLAN_REVISION"
    info "Revised plan (rev ${GITHUB_PLAN_REVISION}) posted. Waiting for approval."
  elif [[ "$approval_status" -eq 3 ]]; then
    info "Plan comment missing on issue #${issue_number}. Re-running analysis..."
    local issue_body_raw
    issue_body_raw=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --json body --jq '.body // ""' 2>/dev/null || printf '')
    if ! run_analysis "$issue_number" "$issue_title" "$issue_body_raw"; then
      warn "Re-analysis failed for issue #${issue_number}. Will retry next heartbeat."
      return 0
    fi
    local change_size
    change_size=$(resolve_change_size "$issue_body_raw" "$ANALYSIS_OUTPUT")
    post_plan_comment "$issue_number" "$ANALYSIS_OUTPUT" "$change_size"
    # A plain `if`, not a `[[ ]] && call` tail: as the function's last command that
    # compound returns 1 for a non-minor plan, and heartbeat.sh calls this function
    # bare under `set -e`, which would end the whole wake — skipping the worktree
    # cleanup and every remaining claimed issue — on an ordinary non-minor plan.
    if [[ "$change_size" == "minor" ]]; then
      implement_and_finalize "$issue_number" "$branch" "$ANALYSIS_OUTPUT" "$issue_title"
    fi
  else
    info "Still waiting for plan approval on issue #${issue_number}."
  fi
}

# handle_phase_implementation <issue_number> <issue_title> <branch>
handle_phase_implementation() {
  local issue_number="$1" issue_title="$2" branch="$3"
  fetch_approved_plan "$issue_number"
  implement_and_finalize "$issue_number" "$branch" "$APPROVED_PLAN_TEXT" "$issue_title"
}

# handle_phase_pr <issue_number> <issue_title> <branch>
handle_phase_pr() {
  local issue_number="$1" issue_title="$2" branch="$3"
  finalize_issue_pr "$branch" "$issue_number" "$issue_title"
}
