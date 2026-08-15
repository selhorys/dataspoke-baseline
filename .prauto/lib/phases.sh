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

# Read a single value from an env file without sourcing it.
# Sourcing would execute the file and export every key into the caller's shell;
# the cluster stages need one value, so they read one value.
# Usage: env_file_value <file> <key>
# Prints: the value with surrounding quotes stripped, empty when the key is absent.
env_file_value() {
  local file="$1" key="$2" line=""
  line=$(grep -E "^${key}=" "$file" 2>/dev/null | tail -1 || true)
  [[ -z "$line" ]] && return 0
  local value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

# Resolve the dev-env file and lock endpoint for the cluster-dependent stages.
# PRAUTO_DEV_ENV_FILE binds this worker to its own dev cluster. A relative value
# resolves under the repo checkout; an absolute value is used as given. It is ALWAYS
# anchored outside $PWD (the branch worktree): the worktree is Claude-writable, so
# resolving cluster credentials from it would let a branch redirect prauto's deploys
# and resets. Resolving from the checkout also keeps a gitignored path out of `git
# status`, and `source` out of executing branch-authored content.
# Usage: resolve_dev_env
# Sets: DEV_ENV_FILE, DEV_LOCK_URL
# Returns: 0 when the env file is present, 1 when it is not.
resolve_dev_env() {
  DEV_ENV_FILE=""
  DEV_LOCK_URL=""

  if [[ -z "${REPO_DIR:-}" ]]; then
    return 1
  fi

  local configured="${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}"
  local candidate
  if [[ "$configured" == /* ]]; then
    candidate="$configured"
  else
    candidate="${REPO_DIR}/${configured}"
  fi
  if [[ ! -f "$candidate" ]]; then
    return 1
  fi
  DEV_ENV_FILE="$candidate"

  local lock_base
  lock_base=$(env_file_value "$DEV_ENV_FILE" "DATASPOKE_DEV_LOCK_URL")
  DEV_LOCK_URL="${lock_base:-http://localhost:9221}/lock"
  return 0
}

# Return 0 when the branch diff against the base branch touches any of the given paths.
# The cluster deploys and stage gates key off which layers a diff reaches.
# Usage: diff_touches <path> [path...]
diff_touches() {
  local changed
  changed=$(git diff --name-only "origin/${PRAUTO_BASE_BRANCH}...HEAD" -- "$@" 2>/dev/null || true)
  [[ -n "$changed" ]]
}

# Run a command with the dev-env file exported, scoped to a subshell.
# `set -a` is required because the env file carries no export prefixes; the
# subshell is what keeps its credentials out of the heartbeat and out of every
# later Claude session, which inherit the orchestrator's environment.
# Usage: with_dev_env <env_file> <command> [args...]
with_dev_env() {
  local env_file="$1"
  shift
  (
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    "$@"
  )
}

# Provision the worker's own dev cluster with a full dev-profile install.
# Runs only from the repo checkout against the resolved env file, never the worktree.
# Provisioning is not an attempt at the issue: it runs inside the cluster-stage
# pre-flight (dev_env_healthy), whose callers return-and-skip on a non-zero without
# posting a heartbeat/retry marker, so the job's retry counter is untouched.
# install.sh is slow (minutes) and owns its own timeouts — it is intentionally not
# wrapped in one here.
# GKE Autopilot abort mode: a GMS scale-up timeout aborts install.sh before the DataHub
# ingress+PAT step; the operator resumes with `--from-component datahub` (see
# .prauto/README.md). Automatic resume is intentionally not implemented — a provisioning
# failure simply skips the cluster stage.
# Usage: provision_dev_env <env_file>
# Returns: 0 on a completed install, 1 when provisioning could not complete.
provision_dev_env() {
  local env_file="$1"
  # Fail closed: without $REPO_DIR the script path must not fall back to `.` (the
  # branch-controlled worktree). Skip rather than run branch-authored install.sh.
  if [[ -z "${REPO_DIR:-}" ]]; then
    warn "REPO_DIR is not set. Cannot provision the dev cluster."
    return 1
  fi
  local install_script="${REPO_DIR}/helm-charts/bin/install.sh"

  if [[ ! -f "$install_script" ]]; then
    warn "install.sh not found. Cannot provision the dev cluster."
    return 1
  fi

  info "Provisioning the dev cluster (install.sh --profile dev)..."
  local provision_output provision_exit=0
  provision_output=$(bash "$install_script" --profile dev --env-file "$env_file" 2>&1) || provision_exit=$?
  if [[ "$provision_exit" -ne 0 ]]; then
    warn "Cluster provisioning failed (exit ${provision_exit}):"
    warn "$provision_output"
    return 1
  fi
  info "Cluster provisioning completed."
  return 0
}

# run_health_check <script> <env_file>
# Run health-check.sh with a wall-clock backstop, leaving its output in
# HEALTH_CHECK_OUTPUT and returning its exit code (1 if the backstop fired).
#
# The check bounds each of its own probes, but this worker is UNSUPERVISED —
# nothing outside it would ever notice a run that does not return, and the whole
# heartbeat would park on it. The preflight hook carries a deadline of its own
# for the same reason; this is that deadline for the prauto path, set far above
# the check's own worst case so it only ever fires on a genuine stall.
#
# A stall is reported as 1, not 2: something was probed and it did not answer,
# which is evidence about the cluster, so the provisioning branch below may act
# on it. The child runs in a private TMPDIR because health-check.sh writes a
# copy of the kubeconfig — credentials — to $TMPDIR, and a run this function had
# to SIGKILL never reaches its own cleanup.
HEALTH_CHECK_OUTPUT=""
run_health_check() {
  local script="$1" env_file="$2"
  local timeout="${PRAUTO_HEALTH_CHECK_TIMEOUT_SECS:-300}"
  local tmpdir out pid deadline rc=0

  HEALTH_CHECK_OUTPUT=""
  tmpdir="$(mktemp -d)" || {
    HEALTH_CHECK_OUTPUT="Could not create a temporary directory for the health check."
    return 2
  }
  out="${tmpdir}/output"

  # Own process group, so the backstop takes the check's in-flight children with it.
  set -m
  TMPDIR="$tmpdir" bash "$script" --env-file "$env_file" --keep-lock </dev/null >"$out" 2>&1 &
  pid=$!
  set +m

  deadline=$(( $(date +%s) + timeout ))
  while kill -0 "$pid" 2>/dev/null; do
    if (( $(date +%s) >= deadline )); then
      # TERM first: bash runs the script's EXIT trap on its way out of a fatal
      # SIGTERM, which is what removes its kubeconfig copy.
      kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      sleep 2
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
      fi
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

# Pre-flight gate for the cluster-dependent stages.
# An unhealthy dev env is evidence about the cluster rather than the branch, so callers
# skip their stage instead of failing the issue. When the check is red and cluster
# provisioning is enabled, provision the worker's own cluster and re-check; the stage is
# skipped only if provisioning itself fails.
# --keep-lock leaves a lock held by another owner untouched and un-failed; prauto then
# meets a held lock through its own acquire, which skips on 409.
# Provisioning is keyed on health-check.sh's exit 1 — "probes ran, something is unhealthy".
# Its exit 2 means the check could not be set up at all (missing kubectl, an unresolvable
# context, an unreadable env file) and never probed anything, which is evidence about this
# worker's own configuration, not about a cluster: building a GKE cluster in response to a
# kubeconfig typo is the failure that split exists to prevent.
# Usage: dev_env_healthy <env_file>
# Returns: 0 when healthy or the check is unavailable, 1 when the check fails.
dev_env_healthy() {
  local env_file="$1"
  # Fail closed: without $REPO_DIR the script path must not fall back to `.` (the
  # branch-controlled worktree). Skip the gate rather than run branch-authored
  # health-check.sh — resolve_dev_env already bailed on an empty REPO_DIR, so this is a
  # latent-but-explicit local guard mirroring the missing-script case below.
  if [[ -z "${REPO_DIR:-}" ]]; then
    warn "REPO_DIR is not set. Proceeding without the dev-env pre-flight gate."
    return 0
  fi
  local script="${REPO_DIR}/helm-charts/bin/health-check.sh"

  if [[ ! -f "$script" ]]; then
    warn "health-check.sh not found. Proceeding without the dev-env pre-flight gate."
    return 0
  fi

  info "Running dev-env health check pre-flight..."
  local health_output health_exit=0
  run_health_check "$script" "$env_file" || health_exit=$?
  health_output="$HEALTH_CHECK_OUTPUT"
  if [[ "$health_exit" -eq 0 ]]; then
    info "Dev-env health check passed."
    return 0
  fi

  if [[ "$health_exit" -eq 2 ]]; then
    warn "Dev-env health check could not run (exit 2 — a setup fault on this worker, not a cluster verdict):"
    warn "$health_output"
    info "Skipping the cluster stage without provisioning: nothing was probed, so there is no evidence a cluster needs building."
    return 1
  fi

  warn "Dev-env health check failed (exit ${health_exit}):"
  warn "$health_output"

  if [[ "${PRAUTO_CLUSTER_PROVISION_ENABLED:-true}" != "true" ]]; then
    info "Cluster provisioning disabled. Skipping the cluster stage."
    return 1
  fi
  if ! provision_dev_env "$env_file"; then
    warn "Cluster provisioning failed. Skipping the cluster stage."
    return 1
  fi

  info "Re-running dev-env health check after provisioning..."
  health_exit=0
  run_health_check "$script" "$env_file" || health_exit=$?
  health_output="$HEALTH_CHECK_OUTPUT"
  if [[ "$health_exit" -ne 0 ]]; then
    warn "Dev-env still unhealthy after provisioning (exit ${health_exit}):"
    warn "$health_output"
    return 1
  fi
  info "Dev-env health check passed after provisioning."
  return 0
}

# Keep the last <max_chars> characters of a string.
# pytest and Playwright print the failure summary last, so the tail is the part a
# fix session needs.
# Usage: tail_chars <text> <max_chars>
tail_chars() {
  local text="$1" max_chars="$2"
  if [[ ${#text} -le $max_chars ]]; then
    printf '%s' "$text"
    return 0
  fi
  printf '(truncated — last %s characters)\n%s' "$max_chars" "${text: -max_chars}"
}

# Run the pytest integration groups separately, spot then api-wired.
# TESTING.md mandates the split: mixing the groups puts competing Airflow load on
# the shared dev cluster and flakes on timing.
# Usage: run_integration_groups <env_file>
# Sets: INTEG_SPOT_EXIT / INTEG_SPOT_OUTPUT, INTEG_API_WIRED_EXIT / INTEG_API_WIRED_OUTPUT,
#       INTEG_EXIT (non-zero when either group failed),
#       INTEG_OUTPUT (the failing groups only — the fix session's prompt)
run_integration_groups() {
  local env_file="$1"

  INTEG_SPOT_EXIT=0
  INTEG_SPOT_OUTPUT="tests/integration/spot/ not present — skipped."
  INTEG_API_WIRED_EXIT=0
  INTEG_API_WIRED_OUTPUT="tests/integration/api_wired/ not present — skipped."

  if [[ -d "tests/integration/spot" ]]; then
    info "Running spot integration tests (tests/integration/spot/)..."
    INTEG_SPOT_OUTPUT=$(DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$env_file" \
      uv run pytest tests/integration/spot/ --tb=short 2>&1) || INTEG_SPOT_EXIT=$?
  fi

  if [[ -d "tests/integration/api_wired" ]]; then
    info "Running api-wired integration tests (tests/integration/api_wired/)..."
    INTEG_API_WIRED_OUTPUT=$(DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$env_file" \
      uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || INTEG_API_WIRED_EXIT=$?
  fi

  INTEG_EXIT=0
  INTEG_OUTPUT=""
  # Carry only the failing groups, each truncated independently, so that a passing
  # group cannot crowd the failures out of the fix session's prompt.
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

# Run integration tests with the dev-env lock protocol.
# Acquires lock, resets dummy data, runs tests, resets again, releases lock.
# Skips gracefully if dev-env is not reachable or lock cannot be acquired.
# Usage: run_integration_tests_with_protocol <pr_number>
run_integration_tests_with_protocol() {
  local pr_number="$1"
  local lock_owner="prauto-${PRAUTO_WORKER_ID}"

  if ! resolve_dev_env; then
    info "Dev-env file (${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}) not found. Skipping integration tests."
    return 0
  fi
  local lock_url="$DEV_LOCK_URL"

  if ! dev_env_healthy "$DEV_ENV_FILE"; then
    info "Dev-env unhealthy. Skipping integration tests."
    return 0
  fi

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
  run_integration_groups "$DEV_ENV_FILE"

  # Release lock
  curl -s -X POST "${lock_url}/release" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
  info "Dev-env lock released."

  # Post each group's results as its own comment
  post_test_results_comment "$pr_number" "Integration (spot)" "$INTEG_SPOT_EXIT" "$INTEG_SPOT_OUTPUT"
  post_test_results_comment "$pr_number" "Integration (api-wired)" "$INTEG_API_WIRED_EXIT" "$INTEG_API_WIRED_OUTPUT"
  info "Integration test results posted on PR #${pr_number} (spot: ${INTEG_SPOT_EXIT}, api-wired: ${INTEG_API_WIRED_EXIT})."
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
  local max_retries="${PRAUTO_INTEGRATION_FIX_MAX_RETRIES:-2}"

  if ! resolve_dev_env; then
    info "Dev-env file (${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}) not found. Skipping integration test fix loop."
    return 0
  fi
  local lock_url="$DEV_LOCK_URL"

  if ! dev_env_healthy "$DEV_ENV_FILE"; then
    info "Dev-env unhealthy. Skipping integration test fix loop."
    return 0
  fi

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

  # Deploy the branch's API so the groups test the branch's code, not a stale image.
  # This is the api-then-frontend ordering constraint's first half — it must precede the
  # E2E stage's frontend deploy (see deploy_branch_api). A deploy failure skips this
  # stage rather than failing the issue.
  if diff_touches src/api/ src/backend/ src/shared/; then
    if ! deploy_branch_api "$DEV_ENV_FILE"; then
      warn "Branch API deploy failed. Skipping the integration test fix loop."
      curl -s -X POST "${lock_url}/release" \
        -H "Content-Type: application/json" \
        -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
      return 0
    fi
  fi

  local attempt
  for (( attempt = 1; attempt <= max_retries; attempt++ )); do
    info "Integration test fix loop: attempt ${attempt}/${max_retries}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — integration test fix loop: attempt ${attempt}/${max_retries}" \
      2>/dev/null || warn "Failed to post integration fix comment on issue #${issue_number}."

    # Run integration tests (conftest.py handles dummy-data resets via Python utilities)
    run_integration_groups "$DEV_ENV_FILE"

    if [[ "$INTEG_EXIT" -eq 0 ]]; then
      info "Integration tests passed on attempt ${attempt}."
      break
    fi

    info "Integration tests failed on attempt ${attempt}/${max_retries} (spot: ${INTEG_SPOT_EXIT}, api-wired: ${INTEG_API_WIRED_EXIT})."

    if [[ "$attempt" -lt "$max_retries" ]]; then
      # Invoke Claude to fix integration test failures
      info "Invoking Claude to fix integration test failures..."
      run_integration_fix_session "$issue_number" "$branch" "$INTEG_OUTPUT"
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

# Deploy the branch's API so the integration groups exercise the branch's code rather
# than a stale image. `--components api` pins the rebuilt image by digest (helm upgrade
# then rolls dataspoke-api by construction — see spec/feature/HELM_CHART.md §Digest
# stamping) and runs its own `kubectl rollout status` wait, so no restart/wait logic is
# needed here — just invoke and check exit.
# ORDERING: this must precede any deploy_branch_frontend. `--components api` is a
# full-release upgrade that reverts frontend.enabled→false, deleting the cluster
# frontend; running it after the frontend deploy would destroy the UI the E2E stage
# needs. The pipeline runs the integration stage (this deploy) before the E2E stage, so
# ordering already holds — do not reorder them.
# SOURCE vs CLUSTER split: install.sh (→ build-image.sh) runs from the branch WORKTREE, so
# the branch's src/, Dockerfile, and chart are what gets built and deployed — that is the
# point of the cluster stage. --env-file stays $REPO_DIR-anchored (resolve_dev_env), so the
# branch cannot redirect which cluster is targeted. Scripts from the worktree; cluster
# selection from the checkout — never blend the two.
# Usage: deploy_branch_api <env_file>
# Returns: 0 on a completed rollout, 1 when the deploy could not be completed.
deploy_branch_api() {
  local env_file="$1"
  local install_script="${WORKTREE_DIR:-}/helm-charts/bin/install.sh"

  if [[ -z "${WORKTREE_DIR:-}" ]] || [[ ! -f "$install_script" ]]; then
    warn "Branch worktree install.sh not found. Cannot deploy the branch API."
    return 1
  fi

  local tool
  for tool in kubectl helm docker; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      warn "${tool} not available. Cannot deploy the branch API."
      return 1
    fi
  done

  info "Building and deploying the branch API..."
  local deploy_output deploy_exit=0
  deploy_output=$(bash "$install_script" --profile dev --components api \
    --env-file "$env_file" 2>&1) || deploy_exit=$?
  if [[ "$deploy_exit" -ne 0 ]]; then
    warn "API deploy failed (exit ${deploy_exit}):"
    warn "$deploy_output"
    return 1
  fi

  info "Branch API deployed and rolled."
  return 0
}

# Deploy the branch's frontend so the E2E suite exercises the branch's UI.
# The umbrella upgrade pins the rebuilt image by digest, which rolls the pod by
# construction even though the tag string (:dev) stays the same (see
# spec/feature/HELM_CHART.md §Digest stamping) — a resolution failure aborts
# install.sh outright (checked via deploy_exit below) rather than deploying a
# stale image, so this stage never reaches the forced restart on a resolution
# failure. The forced restart below is a belt-and-braces guarantee on top of
# that, independent of install.sh's own digest-pin/restart logic.
# SOURCE vs CLUSTER split: install.sh (→ build-image.sh) runs from the branch WORKTREE so
# the branch's src/, Dockerfile, and chart are built and deployed. The env_file (namespace
# lookup below, cluster selection) stays $REPO_DIR-anchored via resolve_dev_env, so the
# branch cannot redirect the target cluster. Scripts from the worktree; cluster from the
# checkout.
# Usage: deploy_branch_frontend <env_file>
# Returns: 0 on a completed rollout, 1 when the deploy could not be completed.
deploy_branch_frontend() {
  local env_file="$1"
  local install_script="${WORKTREE_DIR:-}/helm-charts/bin/install.sh"
  local ns
  ns=$(env_file_value "$env_file" "DATASPOKE_KUBE_DATASPOKE_NAMESPACE")
  ns="${ns:-dataspoke-01}"

  if [[ -z "${WORKTREE_DIR:-}" ]] || [[ ! -f "$install_script" ]]; then
    warn "Branch worktree install.sh not found. Cannot deploy the branch frontend."
    return 1
  fi

  local tool
  for tool in kubectl helm docker; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      warn "${tool} not available. Cannot deploy the branch frontend."
      return 1
    fi
  done

  info "Building and deploying the branch frontend..."
  local deploy_output deploy_exit=0
  deploy_output=$(bash "$install_script" --profile dev --components frontend \
    --env-file "$env_file" 2>&1) || deploy_exit=$?
  if [[ "$deploy_exit" -ne 0 ]]; then
    warn "Frontend deploy failed (exit ${deploy_exit}):"
    warn "$deploy_output"
    return 1
  fi

  info "Forcing a frontend rollout restart..."
  local restart_exit=0
  kubectl rollout restart deployment/dataspoke-frontend -n "$ns" >/dev/null 2>&1 || restart_exit=$?
  if [[ "$restart_exit" -ne 0 ]]; then
    warn "Could not restart deployment/dataspoke-frontend in ${ns} (exit ${restart_exit})."
    return 1
  fi

  # The umbrella upgrade rolls the API pod as well; both must settle before the
  # suite calls the API, and before the caller runs any further test group.
  local deployment status_exit
  for deployment in dataspoke-frontend dataspoke-api; do
    info "Waiting for the ${deployment} rollout to complete..."
    status_exit=0
    kubectl rollout status "deployment/${deployment}" -n "$ns" --timeout=5m >/dev/null 2>&1 || status_exit=$?
    if [[ "$status_exit" -ne 0 ]]; then
      warn "${deployment} did not become ready in ${ns} (exit ${status_exit})."
      return 1
    fi
  done

  info "Branch frontend deployed and rolled."
  return 0
}

# Report E2E results on the branch's PR, falling back to the issue when the
# branch has no PR yet.
# Usage: report_e2e_results <issue_number> <branch> <exit_code> <output>
report_e2e_results() {
  local issue_number="$1" branch="$2" exit_code="$3" output="$4"

  get_pr_number_for_branch "$branch"
  if [[ -n "$BRANCH_PR_NUMBER" ]]; then
    post_test_results_comment "$BRANCH_PR_NUMBER" "E2E" "$exit_code" "$output"
    info "E2E test results posted on PR #${BRANCH_PR_NUMBER} (exit: ${exit_code})."
    return 0
  fi

  local status_label="Passed"
  if [[ "$exit_code" -ne 0 ]]; then
    status_label="Failed (exit code ${exit_code})"
  fi
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — E2E test results: ${status_label}" \
    2>/dev/null || warn "Failed to post E2E results on issue #${issue_number}."
}

# Run the Playwright E2E suite against the branch's deployed UI, in a fix loop.
# The stage deploys the frontend, so it runs only for diffs that reach the UI it
# deploys, the suite itself, or the API contract the UI consumes (src/api/).
# Holds the dev-env lock for its run and must follow the integration groups: the
# frontend deploy also rolls the API pod, so the two must never overlap.
# Usage: run_e2e_test_fix <issue_number> <branch>
run_e2e_test_fix() {
  local issue_number="$1"
  local branch="$2"

  if [[ ! -d "tests/e2e" ]]; then
    info "No tests/e2e/ directory. Skipping E2E stage."
    return 0
  fi
  if ! command -v pnpm >/dev/null 2>&1; then
    info "pnpm not available. Skipping E2E stage."
    return 0
  fi

  # Skip when the diff reaches neither the deployed UI, the suite, nor the API contract
  # the UI consumes. src/backend/ and src/shared/ are excluded — api-wired already
  # proves those over REST against the deployed API image.
  if ! diff_touches src/frontend/ tests/e2e/ src/api/; then
    info "Diff touches no src/frontend/, tests/e2e/, or src/api/ paths. Skipping E2E stage."
    return 0
  fi

  local lock_owner="prauto-${PRAUTO_WORKER_ID}"
  local max_retries="${PRAUTO_E2E_FIX_MAX_RETRIES:-1}"

  if ! resolve_dev_env; then
    info "Dev-env file (${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}) not found. Skipping E2E stage."
    return 0
  fi
  local lock_url="$DEV_LOCK_URL"

  if ! dev_env_healthy "$DEV_ENV_FILE"; then
    info "Dev-env unhealthy. Skipping E2E stage."
    return 0
  fi

  # Check if lock endpoint is reachable
  if ! curl -s --connect-timeout 2 "${lock_url}/status" >/dev/null 2>&1; then
    info "Dev-env lock endpoint not reachable. Skipping E2E stage."
    return 0
  fi

  # Acquire lock
  local lock_code
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${lock_url}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\", \"message\": \"prauto E2E for issue #${issue_number}\"}")

  if [[ "$lock_code" != "200" ]]; then
    info "Could not acquire dev-env lock (HTTP ${lock_code}). Skipping E2E stage."
    return 0
  fi
  info "Dev-env lock acquired for E2E stage."

  local attempt e2e_output e2e_exit=0 deployed=false
  for (( attempt = 1; attempt <= max_retries; attempt++ )); do
    info "E2E stage: attempt ${attempt}/${max_retries}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — E2E stage: attempt ${attempt}/${max_retries}" \
      2>/dev/null || warn "Failed to post E2E comment on issue #${issue_number}."

    if ! deploy_branch_frontend "$DEV_ENV_FILE"; then
      warn "Skipping E2E stage — the branch frontend could not be deployed."
      break
    fi

    info "Installing E2E dependencies..."
    if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1; then
      warn "Skipping E2E stage — pnpm install --frozen-lockfile failed."
      break
    fi

    # A missing browser is a host gap rather than a branch defect, so it skips the
    # stage instead of reporting a suite failure on the PR.
    info "Ensuring the Playwright browser is installed..."
    if ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
      warn "Skipping E2E stage — could not install the Playwright Chromium browser."
      break
    fi
    deployed=true

    info "Running E2E tests..."
    e2e_exit=0
    e2e_output=$(DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" \
      pnpm -C tests/e2e test 2>&1) || e2e_exit=$?

    if [[ "$e2e_exit" -eq 0 ]]; then
      info "E2E tests passed on attempt ${attempt}."
      break
    fi

    info "E2E tests failed (exit ${e2e_exit}) on attempt ${attempt}/${max_retries}."

    if [[ "$attempt" -lt "$max_retries" ]]; then
      info "Invoking Claude to fix E2E test failures..."
      run_e2e_fix_session "$issue_number" "$branch" "$(tail_chars "$e2e_output" 28000)"
    else
      info "Max E2E fix retries reached. Proceeding with current state."
    fi
  done

  # Release lock
  curl -s -X POST "${lock_url}/release" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
  info "Dev-env lock released after E2E stage."

  if [[ "$deployed" == "true" ]]; then
    report_e2e_results "$issue_number" "$branch" "$e2e_exit" "$e2e_output"
  fi
}

# Whether the implementation session reported a wf-minimal ESCALATE.
# The session ends its output with a `PRAUTO_WORKFLOW_OUTCOME:` sentinel line, required by
# the prompt to be the final line with nothing after it; only an explicit ESCALATED
# abandons. Match ONLY the last such line: implementation.md carries the sentinel verbatim
# inside a fenced instruction block, so a COMPLETE report that echoes that block would
# false-trigger a whole-output grep. A missing sentinel (e.g. the session died) is treated
# as non-escalated, so the run proceeds to tests — the cluster stages deploy and exercise
# the branch's own code, so that retry path tests the branch rather than a base-branch
# image, backstopping the proceed-on-missing-sentinel default.
# Usage: implementation_escalated <impl_output>
# Returns: 0 when the session reported ESCALATED, 1 otherwise.
implementation_escalated() {
  local impl_output="$1"
  local last_sentinel
  last_sentinel=$(echo "$impl_output" | grep -E '^PRAUTO_WORKFLOW_OUTCOME:' | tail -1) || true
  [[ "$last_sentinel" =~ ^PRAUTO_WORKFLOW_OUTCOME:[[:space:]]*ESCALATED ]]
}

# Abandon a job whose implementation workflow escalated. A wf-minimal ESCALATE halts the
# run at the escalating stage group, leaving a partial, uncommitted implementation that
# must not be carried into tests or a PR. Swaps labels to prauto:failed and posts an
# abandonment comment naming the escalating stage and the reviewer findings the session
# reported.
# Usage: abandon_workflow_escalation <issue_number> <impl_output>
abandon_workflow_escalation() {
  local issue_number="$1" impl_output="$2"

  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    --add-label "$PRAUTO_GITHUB_LABEL_FAILED" 2>/dev/null || \
    warn "Failed to update labels on issue #${issue_number}."

  # The session's report names the escalating stage + findings; drop the sentinel line and
  # scrub any credentials before the report enters a GitHub comment.
  local details
  details=$(echo "$impl_output" | grep -vE '^PRAUTO_WORKFLOW_OUTCOME:' || true)
  details=$(scrub_secrets "$details")
  details=$(tail_chars "$details" 12000)

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Abandoning — implementation workflow escalated. A per-stage reviewer's findings persisted after a fix pass, so the workflow halted before completing. Manual intervention needed.

${details}" \
    2>/dev/null || warn "Failed to post workflow-escalation comment on issue #${issue_number}."

  info "Job for issue #${issue_number} abandoned (workflow escalation)."
}

# Combined helper: implement (drive wf-minimal) → integration test fix loop → E2E stage
# → finalize PR. A wf-minimal ESCALATE abandons before tests or a PR.
# Usage: implement_and_finalize <issue_number> <branch> <plan> <issue_title>
implement_and_finalize() {
  local issue_number="$1" branch="$2" plan="$3" issue_title="$4"
  # Post implementation start comment (not idempotent — each attempt is a new marker)
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — implementation starting" \
    2>/dev/null || warn "Failed to post implementation start comment on issue #${issue_number}."
  run_implementation "$issue_number" "$branch" "$plan"

  # The implementation session drives wf-minimal, which runs per-stage generator →
  # adversarial-reviewer cycles. An ESCALATE halts it mid-run, leaving a partial,
  # uncommitted implementation — abandon rather than carry it into tests or a PR.
  if implementation_escalated "$IMPL_OUTPUT"; then
    warn "Implementation workflow escalated for issue #${issue_number}. Abandoning."
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — workflow escalated" \
      2>/dev/null || warn "Failed to post workflow-escalation heartbeat on issue #${issue_number}."
    abandon_workflow_escalation "$issue_number" "$IMPL_OUTPUT"
    return 0
  fi

  # Ordering is a correctness constraint: the integration stage deploys the branch API
  # (--components api, a full-release upgrade that reverts frontend.enabled→false), and
  # the E2E stage deploys the frontend and rolls the API pod. Integration must run before
  # E2E and never concurrently — do not reorder these two calls.
  run_integration_test_fix "$issue_number" "$branch"
  run_e2e_test_fix "$issue_number" "$branch"
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
  # Evidence-based gate: minor (auto-proceed) requires BOTH the author's hint AND the
  # analysis confirming its own plan meets CLAUDE.md's skip-plan criteria.
  local change_size
  change_size=$(resolve_change_size "$issue_body_raw" "$ANALYSIS_OUTPUT")
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
    change_size=$(resolve_change_size "$issue_body_raw" "$ANALYSIS_OUTPUT")
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
    change_size=$(resolve_change_size "$issue_body_raw" "$ANALYSIS_OUTPUT")
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
