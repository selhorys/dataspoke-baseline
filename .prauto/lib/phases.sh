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

regression_blocked() {
  local issue_number="$1" reason="$2" branch="${3:-}"
  regression_set_wip "$issue_number" "$branch" || return 1
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Regression blocked by infrastructure/setup: ${reason}. The PR remains in prauto:wip and will retry on a later heartbeat." \
    2>/dev/null || true
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
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Regression blocked: GitHub label/API state could not be established. Retrying on a later heartbeat." 2>/dev/null || true
    return 0
  fi
  if run_post_pr_regression "$issue_number" "$branch"; then
    if regression_ready "$issue_number" "$branch"; then
      complete_job "$issue_number"
    else
      gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
        --body "prauto(${PRAUTO_WORKER_ID}): Regression passed but GitHub readiness labels could not be updated; retrying on a later heartbeat." 2>/dev/null || true
    fi
  fi
}

# env_file_value <file> <key>
# Read a single value from an env file without sourcing it (sourcing would
# execute the file and export every key). Prints the value, quotes stripped.
env_file_value() {
  local file="$1" key="$2" line=""
  line=$(grep -E "^${key}=" "$file" 2>/dev/null | tail -1 || true)
  [[ -z "$line" ]] && return 0
  local value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

# resolve_dev_env
# Resolve the dev-env file and lock endpoint, anchored to $REPO_DIR (the checkout),
# NEVER the worktree — a branch must not redirect deploys. Sets DEV_ENV_FILE,
# DEV_LOCK_URL. Returns 0 if the env file is present, 1 otherwise.
resolve_dev_env() {
  DEV_ENV_FILE=""; DEV_LOCK_URL=""
  [[ -z "${REPO_DIR:-}" ]] && return 1

  local configured="${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}" candidate
  if [[ "$configured" == /* ]]; then candidate="$configured"; else candidate="${REPO_DIR}/${configured}"; fi
  [[ ! -f "$candidate" ]] && return 1
  DEV_ENV_FILE="$candidate"

  local lock_base
  lock_base=$(env_file_value "$DEV_ENV_FILE" "DATASPOKE_DEV_LOCK_URL")
  DEV_LOCK_URL="${lock_base:-http://localhost:9221}/lock"
  return 0
}

# diff_touches <path> [path...]
# Returns 0 when the branch diff against the base touches any of the given paths.
diff_touches() {
  local changed
  changed=$(git diff --name-only "origin/${PRAUTO_BASE_BRANCH}...HEAD" -- "$@" 2>/dev/null || true)
  [[ -n "$changed" ]]
}

# with_dev_env <env_file> <command> [args...]
# Run a command with the dev-env file exported, scoped to a subshell (`set -a`
# because the file carries no export prefixes; the subshell keeps its credentials
# out of the heartbeat and out of later agent sessions).
with_dev_env() {
  local env_file="$1"; shift
  (
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    "$@"
  )
}

# provision_dev_env <env_file>
# Provision the worker's dev cluster with a full dev-profile install, from the
# repo checkout only (never the worktree). Returns 0 on a completed install, 1
# when provisioning could not complete.
provision_dev_env() {
  local env_file="$1"
  [[ -z "${REPO_DIR:-}" ]] && { warn "REPO_DIR is not set. Cannot provision."; return 1; }
  local install_script="${REPO_DIR}/helm-charts/bin/install.sh"
  [[ -f "$install_script" ]] || { warn "install.sh not found. Cannot provision."; return 1; }

  info "Provisioning the dev cluster (install.sh --profile dev)..."
  local provision_output provision_exit=0
  provision_output=$(bash "$install_script" --profile dev --env-file "$env_file" 2>&1) || provision_exit=$?
  if [[ "$provision_exit" -ne 0 ]]; then
    warn "Cluster provisioning failed (exit ${provision_exit}):"
    warn "$provision_output"
    return 1
  fi
  DEV_ENV_PROVISIONED=true
  DEV_ENV_PROVISIONED_ENV_FILE="$env_file"
  write_dev_env_state_marker "$env_file"
  # install.sh rewrites the env file in place (e.g. a fresh LB IP for
  # DATASPOKE_DEV_LOCK_URL); re-resolve so DEV_ENV_FILE/DEV_LOCK_URL reflect it.
  if ! resolve_dev_env; then
    warn "Could not re-resolve the dev-env file after provisioning."
    return 1
  fi
  info "Cluster provisioning completed."
  return 0
}

# write_dev_env_state_marker <env_file>
# Persist proof of a provisioned-but-not-yet-torn-down cluster to disk, atomically
# (mktemp + chmod + mv, matching record_codex_native_session in state.sh). This is
# what survives a crash that skips the in-memory globals and the EXIT trap.
write_dev_env_state_marker() {
  local env_file="$1" tmp_file
  mkdir -p "$(dirname "$DEV_ENV_STATE_FILE")" 2>/dev/null || true
  tmp_file=$(mktemp "${DEV_ENV_STATE_FILE}.tmp.XXXXXX") || return 1
  if ! jq -n --arg env_file "$env_file" --arg provisioned_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{env_file: $env_file, provisioned_at: $provisioned_at}' > "$tmp_file"; then
    rm -f "$tmp_file"
    return 1
  fi
  chmod 600 "$tmp_file" 2>/dev/null || true
  mv -f "$tmp_file" "$DEV_ENV_STATE_FILE"
}

# teardown_provisioned_dev_env
# Remove a dev profile that this heartbeat (or a recovered earlier one, via
# recover_orphaned_dev_env) provisioned. Full deletion includes PVCs and
# namespaces so a temporary test cluster does not keep incurring cost. This is
# intentionally best-effort because it runs from the EXIT trap — but it only
# clears the durable marker on an actual success, so a failure keeps the
# evidence around for the next heartbeat to retry instead of giving up forever.
teardown_provisioned_dev_env() {
  [[ "${DEV_ENV_PROVISIONED:-false}" == true ]] || return 0
  [[ "${DEV_ENV_TEARDOWN_ATTEMPTED:-false}" == true ]] && return 0
  DEV_ENV_TEARDOWN_ATTEMPTED=true

  local env_file="${DEV_ENV_PROVISIONED_ENV_FILE:-}"
  local uninstall_script="${REPO_DIR:-}/helm-charts/bin/uninstall.sh"
  if [[ -z "$env_file" || ! -f "$uninstall_script" ]]; then
    warn "Cannot tear down the provisioned dev cluster: uninstall.sh or its env file is missing."
    return 0
  fi

  info "Tearing down the dev cluster provisioned by this heartbeat..."
  local teardown_output teardown_exit=0
  teardown_output=$(bash "$uninstall_script" \
    --profile dev --env-file "$env_file" --no-question --delete-all 2>&1) || teardown_exit=$?
  if [[ "$teardown_exit" -ne 0 ]]; then
    warn "Dev cluster teardown failed (exit ${teardown_exit}):"
    warn "$teardown_output"
    warn "Leaving the durable marker in place for a later heartbeat to retry."
    return 0
  fi
  rm -f "$DEV_ENV_STATE_FILE"
  info "Provisioned dev cluster torn down."
}

# recover_orphaned_dev_env
# Self-healing for a teardown a previous heartbeat never completed — the process
# was killed before its EXIT trap ran, or uninstall.sh itself failed. Called once
# at the start of every heartbeat, before this wake claims any new work. Loads
# the durable marker (if any) into the same globals provision_dev_env would have
# set, then reuses teardown_provisioned_dev_env's own retry/backoff logic rather
# than duplicating the uninstall.sh invocation.
recover_orphaned_dev_env() {
  [[ -f "$DEV_ENV_STATE_FILE" ]] || return 0
  local env_file
  env_file=$(jq -r '.env_file // empty' "$DEV_ENV_STATE_FILE" 2>/dev/null)
  if [[ -z "$env_file" ]]; then
    warn "Dev-env state marker is unreadable; removing it without a teardown attempt."
    rm -f "$DEV_ENV_STATE_FILE"
    return 0
  fi
  warn "Found a dev-env state marker from an earlier heartbeat (env_file=${env_file}). Retrying its teardown now."
  DEV_ENV_PROVISIONED=true
  DEV_ENV_PROVISIONED_ENV_FILE="$env_file"
  DEV_ENV_TEARDOWN_ATTEMPTED=false
  teardown_provisioned_dev_env
}

# run_health_check <script> <env_file>
# Run health-check.sh with a wall-clock backstop. Sets HEALTH_CHECK_OUTPUT and
# returns the exit code (1 if the backstop fired). Runs in a private TMPDIR
# because health-check.sh writes a kubeconfig copy; a SIGKILL'd run must not
# leave it behind. exit 2 = setup fault (never a cluster verdict).
HEALTH_CHECK_OUTPUT=""
run_health_check() {
  local script="$1" env_file="$2"
  local timeout="${PRAUTO_HEALTH_CHECK_TIMEOUT_SECS:-300}"
  local tmpdir out pid deadline rc=0

  HEALTH_CHECK_OUTPUT=""
  tmpdir="$(mktemp -d)" || { HEALTH_CHECK_OUTPUT="Could not create a temp dir."; return 2; }
  out="${tmpdir}/output"

  set -m
  TMPDIR="$tmpdir" bash "$script" --env-file "$env_file" --keep-lock </dev/null >"$out" 2>&1 &
  pid=$!
  set +m

  deadline=$(( $(date +%s) + timeout ))
  while kill -0 "$pid" 2>/dev/null; do
    if (( $(date +%s) >= deadline )); then
      kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      sleep 2
      kill -0 "$pid" 2>/dev/null && { kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true; }
      wait "$pid" 2>/dev/null || true
      HEALTH_CHECK_OUTPUT="$(cat "$out" 2>/dev/null || true)
[health-check did not finish within ${timeout}s and was stopped]"
      rm -rf "$tmpdir"
      return 1
    fi
    sleep 1
  done

  wait "$pid" || rc=$?
  HEALTH_CHECK_OUTPUT="$(cat "$out" 2>/dev/null || true)"
  rm -rf "$tmpdir"
  return "$rc"
}

# dev_env_healthy <env_file>
# Pre-flight gate for cluster stages. An unhealthy dev env is evidence about the
# cluster, not the branch, so callers skip their stage instead of failing the
# issue. Provisions on exit-1 (red) when enabled; skips on exit-2 (setup fault).
# Returns 0 when healthy, 1 when the stage should be skipped.
dev_env_healthy() {
  local env_file="$1"
  [[ -z "${REPO_DIR:-}" ]] && { warn "REPO_DIR not set; proceeding without the pre-flight gate."; return 0; }
  local script="${REPO_DIR}/helm-charts/bin/health-check.sh"
  [[ -f "$script" ]] || { warn "health-check.sh not found; proceeding without the pre-flight gate."; return 0; }

  info "Running dev-env health check pre-flight..."
  local health_output health_exit=0
  run_health_check "$script" "$env_file" || health_exit=$?
  health_output="$HEALTH_CHECK_OUTPUT"
  [[ "$health_exit" -eq 0 ]] && { info "Dev-env health check passed."; return 0; }

  if [[ "$health_exit" -eq 2 ]]; then
    warn "Dev-env health check could not run (exit 2 — a setup fault, not a cluster verdict):"
    warn "$health_output"
    info "Skipping the cluster stage without provisioning."
    return 1
  fi

  warn "Dev-env health check failed (exit ${health_exit}):"
  warn "$health_output"

  [[ "${PRAUTO_CLUSTER_PROVISION_ENABLED:-true}" != "true" ]] && { info "Provisioning disabled. Skipping the cluster stage."; return 1; }
  if ! provision_dev_env "$env_file"; then
    warn "Cluster provisioning failed. Skipping the cluster stage."
    return 1
  fi

  info "Re-running dev-env health check after provisioning..."
  health_exit=0
  run_health_check "$script" "$env_file" || health_exit=$?
  health_output="$HEALTH_CHECK_OUTPUT"
  [[ "$health_exit" -eq 0 ]] || { warn "Dev-env still unhealthy after provisioning (exit ${health_exit})."; return 1; }
  info "Dev-env health check passed after provisioning."
  return 0
}

# tail_chars <text> <max_chars> — keep the last N characters (failure summaries
# print last).
tail_chars() {
  local text="$1" max_chars="$2"
  if [[ ${#text} -le $max_chars ]]; then printf '%s' "$text"; return 0; fi
  printf '(truncated — last %s characters)\n%s' "$max_chars" "${text: -max_chars}"
}

# run_integration_groups <env_file>
# Run the pytest integration groups separately, spot then api-wired (TESTING.md
# mandates the split — mixing groups flakes on Airflow contention).
run_integration_groups() {
  local env_file="$1"
  INTEG_SPOT_EXIT=0; INTEG_SPOT_OUTPUT="tests/integration/spot/ not present — skipped."
  INTEG_API_WIRED_EXIT=0; INTEG_API_WIRED_OUTPUT="tests/integration/api_wired/ not present — skipped."

  if [[ -d "tests/integration/spot" ]]; then
    info "Running spot integration tests..."
    INTEG_SPOT_OUTPUT=$(ENV_FILE="$env_file" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$env_file" \
      uv run pytest tests/integration/spot/ --tb=short 2>&1) || INTEG_SPOT_EXIT=$?
  fi
  if [[ -d "tests/integration/api_wired" ]]; then
    info "Running api-wired integration tests..."
    INTEG_API_WIRED_OUTPUT=$(ENV_FILE="$env_file" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$env_file" \
      uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || INTEG_API_WIRED_EXIT=$?
  fi

  INTEG_EXIT=0; INTEG_OUTPUT=""
  if [[ "$INTEG_SPOT_EXIT" -ne 0 ]]; then
    INTEG_EXIT=1
    INTEG_OUTPUT="=== Integration (spot) — exit ${INTEG_SPOT_EXIT} ===
$(tail_chars "$INTEG_SPOT_OUTPUT" 14000)"
  fi
  if [[ "$INTEG_API_WIRED_EXIT" -ne 0 ]]; then
    INTEG_EXIT=1
    INTEG_OUTPUT="${INTEG_OUTPUT}

=== Integration (api-wired) — exit ${INTEG_API_WIRED_EXIT} ===
$(tail_chars "$INTEG_API_WIRED_OUTPUT" 14000)"
  fi
}

# run_integration_tests_with_protocol <pr_number>
run_integration_tests_with_protocol() {
  local pr_number="$1"
  local lock_owner="prauto-${PRAUTO_WORKER_ID}"

  if ! resolve_dev_env; then
    info "Dev-env file not found. Skipping integration tests."
    return 0
  fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then info "Dev-env unhealthy. Skipping integration tests."; return 0; fi
  local lock_url="$DEV_LOCK_URL"
  if ! curl -s --connect-timeout 2 "${lock_url}/status" >/dev/null 2>&1; then
    warn "Dev-env lock endpoint not reachable (${lock_url}/status). Skipping integration tests."
    return 0
  fi

  local lock_code
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${lock_url}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\", \"message\": \"prauto integration tests for PR #${pr_number}\"}")
  if [[ "$lock_code" != "200" ]]; then
    info "Could not acquire dev-env lock (HTTP ${lock_code}). Skipping integration tests."
    return 0
  fi
  info "Dev-env lock acquired for integration tests."

  run_integration_groups "$DEV_ENV_FILE"

  curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
  info "Dev-env lock released."

  post_test_results_comment "$pr_number" "Integration (spot)" "$INTEG_SPOT_EXIT" "$INTEG_SPOT_OUTPUT"
  post_test_results_comment "$pr_number" "Integration (api-wired)" "$INTEG_API_WIRED_EXIT" "$INTEG_API_WIRED_OUTPUT"
  info "Integration test results posted on PR #${pr_number}."
}

# post_result <branch> <stage> <exit> <output>
post_result() {
  local branch="$1" stage="$2" exit_code="$3" output="$4"
  # Full post-PR regression deliberately emits one concise status comment per
  # pushed head.  Keep the per-stage output available to the local worker
  # process, but do not expose it in a series of public PR comments.
  if [[ "${POST_PR_REGRESSION_SUMMARY_MODE:-false}" == true ]]; then
    if [[ "$exit_code" -ne 0 ]]; then
      case "|${POST_PR_FAILED_STAGES:-}|" in
        *"|${stage}|"*) ;;
        *) POST_PR_FAILED_STAGES="${POST_PR_FAILED_STAGES:+${POST_PR_FAILED_STAGES}, }${stage}" ;;
      esac
    fi
    return 0
  fi
  get_pr_number_for_branch "$branch"
  [[ -n "$BRANCH_PR_NUMBER" ]] && post_test_results_comment "$BRANCH_PR_NUMBER" "$stage" "$exit_code" "$output"
}

# post_post_pr_regression_comment <branch> <body>
# Regression status belongs to the PR conversation, not its linked issue.
# The body is deliberately caller-supplied summary text; never include command
# output here, because test output can contain credentials or excessive detail.
post_post_pr_regression_comment() {
  local branch="$1" body="$2"
  get_pr_number_for_branch "$branch"
  if [[ -z "${BRANCH_PR_NUMBER:-}" ]]; then
    warn "No PR found for branch ${branch}; could not post regression status."
    return 1
  fi
  if ! gh pr comment "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): ${body}" 2>/dev/null; then
    warn "Failed to post regression status on PR #${BRANCH_PR_NUMBER}."
    return 1
  fi
  return 0
}

# run_static_and_unit_regression <branch>
# Sets LOCAL_REGRESSION_EXIT.  Commands are checks only; none mutates the diff.
run_static_and_unit_regression() {
  local branch="$1" rc=0 output exit_code=0
  LOCAL_REGRESSION_EXIT=0
  [[ -f pyproject.toml ]] || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="pyproject.toml is missing"; return 0; }
  output=$(uv sync 2>&1) || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="uv sync failed"; return 0; }

  output=$(uv run ruff check src/ tests/ 2>&1) || exit_code=$?
  post_result "$branch" "Static (ruff)" "$exit_code" "$output"
  [[ "$exit_code" -eq 0 ]] || rc=1
  exit_code=0; output=$(uv run mypy src/ 2>&1) || exit_code=$?
  post_result "$branch" "Static (mypy)" "$exit_code" "$output"
  [[ "$exit_code" -eq 0 ]] || rc=1

  if diff_touches src/frontend/; then
    exit_code=0; output=$(pnpm -C src/frontend exec tsc --noEmit 2>&1) || exit_code=$?
    post_result "$branch" "Static (frontend typecheck)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
    exit_code=0; output=$(pnpm -C src/frontend exec eslint src/ 2>&1) || exit_code=$?
    post_result "$branch" "Static (frontend eslint)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  fi
  if diff_touches tests/e2e/; then
    exit_code=0; output=$(pnpm -C tests/e2e typecheck 2>&1) || exit_code=$?
    post_result "$branch" "Static (E2E typecheck)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  fi

  [[ -d tests/unit ]] || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="tests/unit is missing"; return 0; }
  exit_code=0; output=$(uv run pytest tests/unit/ --tb=short 2>&1) || exit_code=$?
  post_result "$branch" "Unit (Python)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  if diff_touches src/frontend/; then
    exit_code=0; output=$(pnpm -C src/frontend test 2>&1) || exit_code=$?
    post_result "$branch" "Unit (frontend)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  fi
  LOCAL_REGRESSION_EXIT="$rc"
}

# acquire_required_dev_lock <issue> <purpose>
# Sets REQUIRED_LOCK_OWNER.  Unlike historical pre-PR helpers, every failure is
# a blocked result: a required regression must never become a passing skip.
acquire_required_dev_lock() {
  local issue_number="$1" purpose="$2" lock_code
  REQUIRED_LOCK_OWNER="prauto-${PRAUTO_WORKER_ID}"
  if ! resolve_dev_env; then regression_blocked "$issue_number" "dev env file is unavailable" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then regression_blocked "$issue_number" "dev cluster health/provisioning failed" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  if ! curl -s --connect-timeout 2 "${DEV_LOCK_URL}/status" >/dev/null 2>&1; then
    regression_blocked "$issue_number" "dev-env lock endpoint is unreachable" "${CURRENT_REGRESSION_BRANCH:-}"; return 1
  fi
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${DEV_LOCK_URL}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${REQUIRED_LOCK_OWNER}\", \"message\": \"prauto ${purpose} for issue #${issue_number}\"}")
  if [[ "$lock_code" != 200 ]]; then regression_blocked "$issue_number" "dev-env lock acquisition returned HTTP ${lock_code}" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  return 0
}

release_required_dev_lock() {
  [[ -n "${DEV_LOCK_URL:-}" && -n "${REQUIRED_LOCK_OWNER:-}" ]] || return 0
  curl -s -X POST "${DEV_LOCK_URL}/release" -H "Content-Type: application/json" \
    -d "{\"owner\": \"${REQUIRED_LOCK_OWNER}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
}

# run_full_cluster_regression <issue> <branch>
# Acquires separately for integration and E2E, enforcing API deploy -> spot ->
# api-wired -> frontend deploy -> E2E ordering. Sets CLUSTER_REGRESSION_EXIT: 0 pass, 1 branch
# failure, 2 infrastructure/setup block.
run_full_cluster_regression() {
  local issue_number="$1" branch="$2" output exit_code=0
  CURRENT_REGRESSION_BRANCH="$branch"
  CLUSTER_REGRESSION_EXIT=0
  [[ -d tests/integration/spot && -d tests/integration/api_wired && -d tests/e2e ]] || {
    CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "required integration or E2E test directory is missing" "$branch"; return 0; }
  if ! acquire_required_dev_lock "$issue_number" "full regression"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi

  if ! deploy_branch_api "$DEV_ENV_FILE"; then
    release_required_dev_lock
    if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then
      CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "API deploy could not reach or operate the development environment" "$branch"
    else
      CLUSTER_REGRESSION_EXIT=1; post_result "$branch" "Deploy (API)" 1 "Branch API build/deploy failed."
    fi
    return 0
  fi
  exit_code=0; output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/spot/ --tb=short 2>&1) || exit_code=$?
  post_result "$branch" "Integration (spot)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  exit_code=0; output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || exit_code=$?
  post_result "$branch" "Integration (api-wired)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  # E2E reset-seeds independently and frontend deployment rolls the API, so it
  # must acquire after the integration group has fully released its lock.
  release_required_dev_lock
  if ! acquire_required_dev_lock "$issue_number" "full regression E2E"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
  if ! deploy_branch_frontend "$DEV_ENV_FILE"; then
    release_required_dev_lock
    if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then
      CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "frontend deploy could not reach or operate the development environment" "$branch"
    else
      CLUSTER_REGRESSION_EXIT=1; post_result "$branch" "Deploy (frontend)" 1 "Branch frontend build/deploy failed."
    fi
    return 0
  fi
  if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1 || ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
    release_required_dev_lock; CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "E2E runner/browser setup failed" "$branch"; return 0
  fi
  exit_code=0; output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" pnpm -C tests/e2e test 2>&1) || exit_code=$?
  post_result "$branch" "E2E" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  release_required_dev_lock
}

# run_post_pr_regression <issue> <branch>
# Repeats the complete regression after every worker fix, and does not let a
# setup/cluster condition invoke a code-fix worker. Returns success only when
# the final pushed PR head is ready for review.
run_post_pr_regression() {
  local issue_number="$1" branch="$2" attempt max_retries
  if ! branch_is_code_affecting "$branch"; then
    info "Diff is confined to the explicit non-code exclusion set; full regression is not required."
    return 0
  fi
  [[ -n "$issue_number" ]] || { warn "No issue number for regression gate."; return 1; }
  max_retries="${PRAUTO_REGRESSION_FIX_MAX_RETRIES:-2}"
  POST_PR_REGRESSION_SUMMARY_MODE=true
  for (( attempt=0; attempt<=max_retries; attempt++ )); do
    POST_PR_FAILED_STAGES=""
    run_static_and_unit_regression "$branch"
    if [[ "$LOCAL_REGRESSION_EXIT" -eq 2 ]]; then
      regression_blocked "$issue_number" "$LOCAL_REGRESSION_REASON" "$branch"
      POST_PR_REGRESSION_SUMMARY_MODE=false
      return 1
    fi
    run_full_cluster_regression "$issue_number" "$branch"
    if [[ "$CLUSTER_REGRESSION_EXIT" -eq 2 ]]; then
      POST_PR_REGRESSION_SUMMARY_MODE=false
      return 1
    fi
    if [[ "$LOCAL_REGRESSION_EXIT" -eq 0 && "$CLUSTER_REGRESSION_EXIT" -eq 0 ]]; then
      if ! post_post_pr_regression_comment "$branch" "Full post-PR regression passed for the current pushed PR head."; then
        regression_blocked "$issue_number" "required regression success notice could not be posted" "$branch"
        POST_PR_REGRESSION_SUMMARY_MODE=false
        return 1
      fi
      POST_PR_REGRESSION_SUMMARY_MODE=false
      return 0
    fi
    if [[ "$attempt" -ge "$max_retries" ]]; then
      regression_set_wip "$issue_number" "$branch"
      post_post_pr_regression_comment "$branch" "Post-PR regression is still failing after ${max_retries} fix attempt(s); the PR remains in prauto:wip."
      POST_PR_REGRESSION_SUMMARY_MODE=false
      return 1
    fi
    # This is intentionally posted before the worker is launched, so PR
    # readers know why the branch may change while it remains in WIP.
    if ! post_post_pr_regression_comment "$branch" "Post-PR regression failed: ${POST_PR_FAILED_STAGES:-an unclassified branch-attributable stage}. Generator/reviewer agents will fix this before the full regression reruns."; then
      regression_blocked "$issue_number" "required regression failure notice could not be posted" "$branch"
      POST_PR_REGRESSION_SUMMARY_MODE=false
      return 1
    fi
    info "Full regression failed; invoking the worker fix workflow before rerunning every layer."
    run_integration_fix_session "$issue_number" "$branch" "Full post-PR regression failed in: ${POST_PR_FAILED_STAGES:-an unclassified branch-attributable stage}. Diagnose and fix the branch-attributable failure, then preserve the generator/reviewer contract."
    checkpoint_branch "$issue_number" "$branch"
    # A PR already exists by this point, so derive_phase_from_github will always
    # report "pr" on the next wake — a phase the quota-resume dispatch in
    # heartbeat.sh does not know how to resume. Posting the normal resumable
    # pause marker here would strand the issue forever (paused, unresumable).
    # Defer instead: no marker, no burned fix attempt, plain retry next wake.
    if [[ "$AGENT_STATUS" == "quota" ]]; then
      warn "Issue #${issue_number}: post-PR regression fix worker died on a quota/session limit (${ACTIVE_AGENT}). Deferring without burning a fix attempt."
      post_post_pr_regression_comment "$branch" "Post-PR regression fix paused: ${ACTIVE_AGENT} quota is exhausted. This is an infrastructure condition, not a code failure — retrying automatically on a later heartbeat." || true
      POST_PR_REGRESSION_SUMMARY_MODE=false
      return 1
    fi
    push_branch "$branch"
    create_or_update_pr "$issue_number" "" "$branch"
  done
  POST_PR_REGRESSION_SUMMARY_MODE=false
  return 1
}

# run_pre_pr_selected_verification <issue> <branch>
# The current selector is intentionally conservative: it selects the full
# affected layer while per-test impact metadata is unavailable.  It never runs
# an unrelated cluster layer, and it never turns uncertainty into an omission.
run_pre_pr_selected_verification() {
  local issue_number="$1" branch="$2"
  if ! branch_is_code_affecting "$branch"; then
    info "Pre-PR verification: explicit non-code diff; no code-test layer selected."
    return 0
  fi
  info "Pre-PR verification: selected full static/unit suites (conservative impact fallback)."
  run_static_and_unit_regression "$branch"
  if [[ "$LOCAL_REGRESSION_EXIT" -eq 2 ]]; then regression_blocked "$issue_number" "$LOCAL_REGRESSION_REASON" "$branch"; return 1; fi
  if [[ "$LOCAL_REGRESSION_EXIT" -ne 0 ]]; then
    info "Pre-PR static/unit failure will be handled by the mandatory post-PR fix gate."
  fi
  if diff_touches src/api/ src/backend/ src/shared/ tests/integration/; then
    info "Pre-PR verification: selected spot and api-wired suites (conservative affected-layer fallback)."
    # A non-zero return here means the fix worker died on quota mid-loop (a
    # pause marker is already posted) — stop before PR creation so the next
    # wake resumes the same session instead of finalizing an unverified branch.
    run_integration_test_fix "$issue_number" "$branch" || return 1
  fi
  if diff_touches src/frontend/ src/api/ tests/e2e/; then
    info "Pre-PR verification: selected E2E suite (conservative affected-layer fallback)."
    run_e2e_test_fix "$issue_number" "$branch" || return 1
  fi
}

# fetch_approved_plan <issue_number>
# Fetch the latest plan comment's plan body (scoped to lifecycle). Sets
# APPROVED_PLAN_TEXT.
fetch_approved_plan() {
  local issue_number="$1"
  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
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
  local lock_url="$DEV_LOCK_URL"
  if ! curl -s --connect-timeout 2 "${lock_url}/status" >/dev/null 2>&1; then
    warn "Dev-env lock endpoint not reachable (${lock_url}/status). Skipping integration fix loop."
    return 0
  fi

  local lock_code
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${lock_url}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\", \"message\": \"prauto integration fix for issue #${issue_number}\"}")
  if [[ "$lock_code" != "200" ]]; then info "Could not acquire dev-env lock. Skipping."; return 0; fi
  info "Dev-env lock acquired for integration test fix loop."

  [[ -f "pyproject.toml" ]] && uv sync 2>&1 || warn "uv sync failed."

  if diff_touches src/api/ src/backend/ src/shared/; then
    if ! deploy_branch_api "$DEV_ENV_FILE"; then
      warn "Branch API deploy failed. Skipping the integration test fix loop."
      curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
        -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || true
      return 0
    fi
  fi

  local attempt quota_paused=false
  for (( attempt = 1; attempt <= max_retries; attempt++ )); do
    info "Integration test fix loop: attempt ${attempt}/${max_retries}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — integration test fix loop: attempt ${attempt}/${max_retries}" \
      2>/dev/null || true
    run_integration_groups "$DEV_ENV_FILE"
    [[ "$INTEG_EXIT" -eq 0 ]] && { info "Integration tests passed on attempt ${attempt}."; break; }

    info "Integration tests failed (spot: ${INTEG_SPOT_EXIT}, api-wired: ${INTEG_API_WIRED_EXIT})."
    if [[ "$attempt" -lt "$max_retries" ]]; then
      run_integration_fix_session "$issue_number" "$branch" "$INTEG_OUTPUT"
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
    else
      info "Max integration fix retries reached. Proceeding with current state."
    fi
  done

  curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
  info "Dev-env lock released after integration test fix loop."
  [[ "$quota_paused" == "true" ]] && return 1
  return 0
}

# deploy_branch_api <env_file>
# Deploy the branch's API (--components api) from the worktree, targeting the
# $REPO_DIR-anchored env file. ORDERING: must precede deploy_branch_frontend.
classify_deploy_failure() {
  local deploy_output="$1"
  # Fail closed: only a small set of affirmative source-build/chart-validation
  # diagnostics is branch-attributable. Registry, Helm, resource, rollout, or
  # unknown failures are infrastructure until a human can diagnose them.
  DEPLOY_FAILURE_KIND="infrastructure"
  # Helm's own schema-validation wording puts "schema" before "chart" (e.g.
  # "values don't meet the specifications of the schema(s) in the following
  # chart(s):"), so both orders are matched.
  if printf '%s' "$deploy_output" | grep -Eqi 'failed to solve.*(Dockerfile|COPY|RUN)|Dockerfile.*(error|failed)|(^|[^[:alpha:]])(tsc|typescript|eslint|mypy|ruff)[^[:alpha:]].*(error|failed)|chart.*(schema|validation)|(schema|validation).*chart|values.*(invalid|required|must be)|template.*(executing|error).*\.yaml'; then
    DEPLOY_FAILURE_KIND="branch"
  fi
}

deploy_branch_api() {
  local env_file="$1"
  DEPLOY_FAILURE_KIND="infrastructure"
  local install_script="${WORKTREE_DIR:-}/helm-charts/bin/install.sh"
  [[ -z "${WORKTREE_DIR:-}" ]] || [[ ! -f "$install_script" ]] && { warn "Branch install.sh not found. Cannot deploy API."; return 1; }
  local tool
  for tool in kubectl helm docker; do
    command -v "$tool" >/dev/null 2>&1 || { warn "${tool} not available. Cannot deploy API."; return 1; }
  done

  info "Building and deploying the branch API..."
  local deploy_output deploy_exit=0
  deploy_output=$(bash "$install_script" --profile dev --components api --env-file "$env_file" 2>&1) || deploy_exit=$?
  if [[ "$deploy_exit" -ne 0 ]]; then classify_deploy_failure "$deploy_output"; warn "API deploy failed (exit ${deploy_exit}, ${DEPLOY_FAILURE_KIND}):"; warn "$deploy_output"; return 1; fi
  info "Branch API deployed and rolled."
  return 0
}

# deploy_branch_frontend <env_file>
deploy_branch_frontend() {
  local env_file="$1"
  DEPLOY_FAILURE_KIND="infrastructure"
  local install_script="${WORKTREE_DIR:-}/helm-charts/bin/install.sh"
  local ns
  ns=$(env_file_value "$env_file" "DATASPOKE_KUBE_DATASPOKE_NAMESPACE"); ns="${ns:-dataspoke-01}"
  [[ -z "${WORKTREE_DIR:-}" ]] || [[ ! -f "$install_script" ]] && { warn "Branch install.sh not found. Cannot deploy frontend."; return 1; }
  local tool
  for tool in kubectl helm docker; do
    command -v "$tool" >/dev/null 2>&1 || { warn "${tool} not available. Cannot deploy frontend."; return 1; }
  done

  info "Building and deploying the branch frontend..."
  local deploy_output deploy_exit=0
  deploy_output=$(bash "$install_script" --profile dev --components frontend --env-file "$env_file" 2>&1) || deploy_exit=$?
  if [[ "$deploy_exit" -ne 0 ]]; then classify_deploy_failure "$deploy_output"; warn "Frontend deploy failed (exit ${deploy_exit}, ${DEPLOY_FAILURE_KIND}):"; warn "$deploy_output"; return 1; fi

  info "Forcing a frontend rollout restart..."
  kubectl rollout restart deployment/dataspoke-frontend -n "$ns" >/dev/null 2>&1 || { DEPLOY_FAILURE_KIND="infrastructure"; warn "Could not restart frontend in ${ns}."; return 1; }

  local deployment status_exit
  for deployment in dataspoke-frontend dataspoke-api; do
    info "Waiting for ${deployment} rollout..."
    status_exit=0
    kubectl rollout status "deployment/${deployment}" -n "$ns" --timeout=5m >/dev/null 2>&1 || status_exit=$?
    [[ "$status_exit" -ne 0 ]] && { DEPLOY_FAILURE_KIND="infrastructure"; warn "${deployment} did not become ready in ${ns}."; return 1; }
  done
  info "Branch frontend deployed and rolled."
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
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — E2E test results: ${status_label}" 2>/dev/null || true
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
  local max_retries="${PRAUTO_E2E_FIX_MAX_RETRIES:-1}"
  if ! resolve_dev_env; then info "Dev-env file not found. Skipping E2E stage."; return 0; fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then info "Dev-env unhealthy. Skipping E2E stage."; return 0; fi
  local lock_url="$DEV_LOCK_URL"
  if ! curl -s --connect-timeout 2 "${lock_url}/status" >/dev/null 2>&1; then
    warn "Dev-env lock endpoint not reachable (${lock_url}/status). Skipping E2E stage."
    return 0
  fi

  local lock_code
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${lock_url}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\", \"message\": \"prauto E2E for issue #${issue_number}\"}")
  if [[ "$lock_code" != "200" ]]; then info "Could not acquire dev-env lock. Skipping E2E."; return 0; fi
  info "Dev-env lock acquired for E2E stage."

  local attempt e2e_output e2e_exit=0 deployed=false quota_paused=false
  for (( attempt = 1; attempt <= max_retries; attempt++ )); do
    info "E2E stage: attempt ${attempt}/${max_retries}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — E2E stage: attempt ${attempt}/${max_retries}" 2>/dev/null || true

    if ! deploy_branch_frontend "$DEV_ENV_FILE"; then warn "Skipping E2E — frontend deploy failed."; break; fi
    if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1; then warn "Skipping E2E — pnpm install failed."; break; fi
    if ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
      warn "Skipping E2E — could not install Playwright Chromium."
      break
    fi
    deployed=true

    info "Running E2E tests..."
    e2e_exit=0
    e2e_output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" \
      pnpm -C tests/e2e test 2>&1) || e2e_exit=$?
    [[ "$e2e_exit" -eq 0 ]] && { info "E2E tests passed on attempt ${attempt}."; break; }

    info "E2E tests failed (exit ${e2e_exit})."
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
  done

  curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
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

# implementation_complete <impl_output>
# A successful implementation must explicitly end with COMPLETE. Exit code 0
# alone is insufficient: an agent can return empty, partial, or otherwise
# malformed text after making no usable workflow progress.
implementation_complete() {
  local impl_output="$1" last_line
  last_line=$(printf '%s' "$impl_output" | sed '/^[[:space:]]*$/d' | tail -1) || true
  [[ "$last_line" == "PRAUTO_WORKFLOW_OUTCOME: COMPLETE" ]]
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

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Abandoning — implementation workflow escalated. A per-stage reviewer's findings persisted after a fix pass. Manual intervention needed.

${details}" \
    2>/dev/null || warn "Failed to post workflow-escalation comment on issue #${issue_number}."
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
      "$PAUSED_SESSION_ID" "${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"
    IMPL_SESSION_ID="$PAUSED_SESSION_ID"
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

  if implementation_escalated "$IMPL_OUTPUT"; then
    warn "Implementation workflow escalated for issue #${issue_number}. Abandoning."
    checkpoint_branch "$issue_number" "$branch"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — workflow escalated" 2>/dev/null || true
    abandon_workflow_escalation "$issue_number" "$IMPL_OUTPUT"
    return 0
  fi

  if ! implementation_complete "$IMPL_OUTPUT"; then
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
      ANALYSIS_OUTPUT="$AGENT_OUTPUT"
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
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — implementation starting" 2>/dev/null || true
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
    [[ "$change_size" == "minor" ]] && implement_and_finalize "$issue_number" "$branch" "$ANALYSIS_OUTPUT" "$issue_title"
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
