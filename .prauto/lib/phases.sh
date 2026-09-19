# shellcheck shell=bash
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

# private_process_group_for_leader <leader_pid>
# Print a verified private process-group id for a job-control child. A process
# group is safe to signal only when its PGID equals its leader PID: that proves
# it cannot be the heartbeat's group or an inherited caller group.
private_process_group_for_leader() {
  local leader_pid="$1"
  [[ "$leader_pid" =~ ^[0-9]+$ ]] || return 1
  # The job-control launch makes the child its own group leader.  A successful
  # group-directed signal-zero probe for that same numeric id proves that the
  # leader owns a live group with PGID == PID; otherwise no process can lead a
  # group named by this still-live PID. Unlike ps(1), this works in restricted
  # test/runtime sandboxes as well as on macOS.
  kill -0 "$leader_pid" 2>/dev/null || return 1
  kill -0 -"$leader_pid" 2>/dev/null || return 1
  printf '%s' "$leader_pid"
}

# managed_process_group_is_live <pgid>
# Test group membership rather than the leader's liveness: the leader can exit
# while a credential helper or other descendant remains in its private group.
managed_process_group_is_live() {
  local pgid="$1"
  [[ "$pgid" =~ ^[0-9]+$ ]] || return 1
  kill -0 -"$pgid" 2>/dev/null
}

# wait_with_group_backstop <timeout_secs> <leader_pid> <verified_pgid>
# Shared TERM-then-KILL wall-clock backstop for a command already backgrounded
# under `set -m`. The caller passes its leader PID plus a verified private
# PGID. Blocks until the whole group exits or the timeout fires; on timeout it
# group-kills the whole process tree (TERM, a short grace window, then KILL)
# so descendants — e.g. helm's `docker-credential-desktop` helper — cannot
# outlive it the way a plain `kill <pid>` would. Returns the command's exit
# code, or 124 on timeout. It never falls back to bare-PID signalling.
wait_with_group_backstop() {
  local timeout_secs="$1" leader_pid="$2" pgid="$3"
  [[ "$leader_pid" =~ ^[0-9]+$ && "$pgid" =~ ^[0-9]+$ ]] || return 2
  local deadline=$(( $(date +%s) + timeout_secs )) rc=0
  while managed_process_group_is_live "$pgid"; do
    if (( $(date +%s) >= deadline )); then
      kill -TERM -"$pgid" 2>/dev/null || true
      sleep 2
      managed_process_group_is_live "$pgid" && kill -9 -"$pgid" 2>/dev/null || true
      wait "$leader_pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
  done
  wait "$leader_pid" || rc=$?
  return "$rc"
}

# prune_old_provision_logs <log_dir>
# Provisioning logs are unscrubbed local transcripts of a real install.sh run
# (see spec/AI_PRAUTO.md §Provisioning) — never posted anywhere, but not kept
# forever either. Keeps the newest 4 so this run's own new file makes 5 total.
prune_old_provision_logs() {
  local log_dir="$1"
  [[ -d "$log_dir" ]] || return 0
  local -a old_logs=()
  while IFS= read -r f; do old_logs+=("$f"); done < <(ls -1t "${log_dir}"/provision-* 2>/dev/null | tail -n +5)
  [[ "${#old_logs[@]}" -gt 0 ]] && rm -f "${old_logs[@]}"
  return 0
}

# provision_dev_env <env_file>
# Provision the worker's dev cluster with a full dev-profile install, from the
# repo checkout only (never the worktree). Returns 0 on a completed install, 1
# when provisioning could not complete (including a timeout) — handled exactly
# like any other provisioning failure by every caller (see
# spec/AI_PRAUTO.md §Provisioning).
#
# install.sh runs under a wall-clock backstop (PRAUTO_PROVISION_TIMEOUT_SECS,
# default 3600) using the same group-kill mechanics as run_health_check
# (wait_with_group_backstop) rather than quota.sh's run_with_timeout: that
# helper only kills the process group when `setsid` is available, which macOS
# does not guarantee, and install.sh can wedge on a `helm dependency build` ->
# docker-credential-helper grandchild that a single-process TERM/KILL would
# leave running. install.sh itself may background helm under its own `set -m`
# job and TERM that group from its own EXIT trap, so this function's backstop
# gives install.sh's whole tree the same TERM-then-grace-then-KILL treatment
# rather than an immediate KILL that could cut its trap off mid-cleanup.
# Output is written only to a per-run, mode-600 log file under the state dir.
# The heartbeat emits fixed safe lifecycle messages, never raw or tailed log
# text: provisioning output can contain operational details inappropriate for
# stdout, stderr, GitHub comments, or scheduler logs. The private log is
# mandatory: falling back to an unprotected stdout-only transcript would
# violate the private-log contract.
#
# PROVISION_PGID (declared near the top of this file) is set the moment the
# process is backgrounded and cleared immediately after its wait — see its
# declaration comment for why heartbeat.sh's EXIT trap needs it.
provision_dev_env() {
  local env_file="$1"
  [[ -z "${REPO_DIR:-}" ]] && { warn "REPO_DIR is not set. Cannot provision."; return 1; }
  local install_script="${REPO_DIR}/helm-charts/bin/install.sh"
  [[ -f "$install_script" ]] || { warn "install.sh not found. Cannot provision."; return 1; }

  local timeout="${PRAUTO_PROVISION_TIMEOUT_SECS:-3600}"
  if [[ ! "$timeout" =~ ^[0-9]+$ ]] || [[ "$timeout" -lt 1 ]]; then
    [[ -n "${PRAUTO_PROVISION_TIMEOUT_SECS:-}" ]] && \
      warn "Invalid PRAUTO_PROVISION_TIMEOUT_SECS='${PRAUTO_PROVISION_TIMEOUT_SECS}'; using the default (3600s)."
    timeout=3600
  fi
  local log_dir="${STATE_DIR:-${PRAUTO_DIR}/state}"
  if ! mkdir -p "$log_dir" 2>/dev/null; then
    warn "Could not create the private provisioning-log directory. Cannot provision."
    return 1
  fi
  prune_old_provision_logs "$log_dir"
  local prov_log=""
  # BSD mktemp (macOS) expands Xs only at the end of its template. Keep the
  # random suffix terminal so the same private-log path works on macOS and
  # GNU systems; the provision-* retention glob intentionally has no suffix
  # dependency.
  prov_log=$(mktemp "${log_dir}/provision-XXXXXX" 2>/dev/null) || {
    warn "Could not create a private provisioning log. Cannot provision."
    return 1
  }
  if ! chmod 600 "$prov_log" 2>/dev/null; then
    warn "Could not protect the provisioning log. Cannot provision."
    rm -f "$prov_log"
    return 1
  fi

  # Write the durable marker (and set the in-memory globals) BEFORE launching
  # install.sh rather than after a successful exit. A cluster install.sh has
  # only partially built is exactly the case this exists for: if this
  # heartbeat is killed mid-install, both its own EXIT trap and a later
  # heartbeat's recover_orphaned_dev_env must still find evidence to tear it
  # down. This function only ever runs when the caller (dev_env_healthy) has
  # already decided provisioning is needed, so it never marks — and therefore
  # never tears down — a pre-existing healthy cluster prauto did not start
  # provisioning.
  DEV_ENV_PROVISIONED=true
  DEV_ENV_PROVISIONED_ENV_FILE="$env_file"
  write_dev_env_state_marker "$env_file"

  info "Provisioning the dev cluster (install.sh --profile dev)..."
  local pid pgid rc=0 gate_dir gate_fifo
  # Start an inert gated wrapper, not install.sh itself. It cannot exec the
  # provisioning command until this parent has verified the wrapper's private
  # process group and writes "start" to its FIFO. If verification fails, close
  # the FIFO instead: the wrapper reads EOF and exits without cluster work.
  gate_dir=$(mktemp -d "${TMPDIR:-/tmp}/prauto-provision-gate.XXXXXX") || {
    warn "Could not create the provisioning containment gate. Cannot provision."
    return 1
  }
  gate_fifo="${gate_dir}/start"
  if ! mkfifo "$gate_fifo"; then
    rmdir "$gate_dir" 2>/dev/null || true
    warn "Could not create the provisioning containment gate. Cannot provision."
    return 1
  fi
  # O_RDWR makes the FIFO open non-blocking before the wrapper starts. It also
  # lets heartbeat cleanup close the only writer in the signal window, causing
  # the inert wrapper to receive EOF rather than wait indefinitely.
  exec 9<>"$gate_fifo"
  CONTAINMENT_GATE_FD_OPEN=true
  set -m
  # Keep the transcript private and bounded while preserving both the opening
  # diagnostics and the final failure context. The byte-stream filter reads
  # fixed-size chunks, retains at most 12 KiB for short output and 6 KiB from
  # each end for larger output, and never buffers an entire line. pipefail is
  # required so the installer's exit status, rather than the filter's,
  # controls this job.
  ( exec 9>&-; IFS= read -r permit < "$gate_fifo"; [[ "$permit" == start ]] || exit 0; set -o pipefail; LC_ALL=C bash "$install_script" --profile dev --env-file "$env_file" </dev/null 2>&1 | python3 -c '
import sys

LIMIT = 12000
HALF = 6000
CHUNK = 4096
prefix = bytearray()
suffix = bytearray()
total = 0
written = 0

while True:
    chunk = sys.stdin.buffer.read(CHUNK)
    if not chunk:
        break
    total += len(chunk)
    if written < LIMIT:
        visible = chunk[: LIMIT - written]
        sys.stdout.buffer.write(visible)
        sys.stdout.buffer.flush()
        written += len(visible)
    if len(prefix) < HALF:
        prefix.extend(chunk[: HALF - len(prefix)])
    suffix.extend(chunk)
    if len(suffix) > HALF:
        del suffix[:-HALF]

if total > LIMIT:
    # The first 12 KiB were streamed so the private log is useful while the
    # installer is still running. Once EOF proves that the stream was larger,
    # replace the provisional middle with the retained tail. Both buffers are
    # fixed-size, so even a giant single line cannot grow memory without bound.
    sys.stdout.buffer.seek(0)
    sys.stdout.buffer.write(prefix)
    sys.stdout.buffer.write(suffix)
    sys.stdout.buffer.truncate()
    sys.stdout.buffer.flush()
' >"$prov_log" 2>&1 ) &
  pid=$!
  CONTAINMENT_GATE_WRAPPER_PID="$pid"
  set +m
  if ! pgid=$(private_process_group_for_leader "$pid"); then
    exec 9>&-
    CONTAINMENT_GATE_FD_OPEN=false
    wait "$pid" 2>/dev/null || true
    CONTAINMENT_GATE_WRAPPER_PID=""
    rm -f "$gate_fifo"; rmdir "$gate_dir" 2>/dev/null || true
    warn "Could not verify a private provisioning process group. Provisioning was not started."
    return 1
  fi
  PROVISION_PGID="$pgid"
  PROVISION_LEADER_PID="$pid"
  # Store the verified group before authorizing the exec, so a signal that
  # arrives immediately after this write still group-terminates the command.
  printf 'start\n' >&9
  exec 9>&-
  CONTAINMENT_GATE_FD_OPEN=false
  CONTAINMENT_GATE_WRAPPER_PID=""
  rm -f "$gate_fifo"; rmdir "$gate_dir" 2>/dev/null || true

  rc=0
  wait_with_group_backstop "$timeout" "$pid" "$pgid" || rc=$?
  PROVISION_PGID=""
  PROVISION_LEADER_PID=""

  if [[ "$rc" -eq 124 ]]; then
    warn "Cluster provisioning timed out after ${timeout}s. The private provisioning log was retained locally."
    return 1
  fi
  if [[ "$rc" -ne 0 ]]; then
    warn "Cluster provisioning failed (exit ${rc}). The private provisioning log was retained locally."
    return 1
  fi
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
  local tmpdir out pid rc=0

  HEALTH_CHECK_OUTPUT=""
  tmpdir="$(mktemp -d)" || { HEALTH_CHECK_OUTPUT="Could not create a temp dir."; return 2; }
  out="${tmpdir}/output"

  local gate_dir gate_fifo pgid
  gate_dir=$(mktemp -d "${TMPDIR:-/tmp}/prauto-health-gate.XXXXXX") || {
    HEALTH_CHECK_OUTPUT="Could not create a health-check containment gate."
    rm -rf "$tmpdir"
    return 2
  }
  gate_fifo="${gate_dir}/start"
  if ! mkfifo "$gate_fifo"; then
    rmdir "$gate_dir" 2>/dev/null || true
    HEALTH_CHECK_OUTPUT="Could not create a health-check containment gate."
    rm -rf "$tmpdir"
    return 2
  fi
  exec 9<>"$gate_fifo"
  CONTAINMENT_GATE_FD_OPEN=true
  set -m
  ( exec 9>&-; IFS= read -r permit < "$gate_fifo"; [[ "$permit" == start ]] || exit 0; exec env TMPDIR="$tmpdir" bash "$script" --env-file "$env_file" --keep-lock </dev/null >"$out" 2>&1 ) &
  pid=$!
  CONTAINMENT_GATE_WRAPPER_PID="$pid"
  set +m
  rc=0
  pgid=$(private_process_group_for_leader "$pid") || {
    HEALTH_CHECK_OUTPUT="Could not verify a private health-check process group."
    exec 9>&-
    CONTAINMENT_GATE_FD_OPEN=false
    wait "$pid" 2>/dev/null || true
    CONTAINMENT_GATE_WRAPPER_PID=""
    rm -f "$gate_fifo"; rmdir "$gate_dir" 2>/dev/null || true
    rm -rf "$tmpdir"
    return 2
  }
  # Publish the verified group before authorizing the health-check command. If
  # the heartbeat is signalled while the command is running, its EXIT trap can
  # now terminate and reap this whole private group rather than waiting on a
  # wedged leader or leaving descendants behind.
  PROVISION_PGID="$pgid"
  PROVISION_LEADER_PID="$pid"
  printf 'start\n' >&9
  exec 9>&-
  CONTAINMENT_GATE_FD_OPEN=false
  CONTAINMENT_GATE_WRAPPER_PID=""
  rm -f "$gate_fifo"; rmdir "$gate_dir" 2>/dev/null || true
  wait_with_group_backstop "$timeout" "$pid" "$pgid" || rc=$?
  # Do not clear the shared containment state until the group wait/backstop has
  # completed; heartbeat cleanup owns it while this function is blocked.
  if [[ "$rc" -eq 124 ]]; then
    HEALTH_CHECK_OUTPUT="$(cat "$out" 2>/dev/null || true)
[health-check did not finish within ${timeout}s and was stopped]"
    PROVISION_PGID=""
    PROVISION_LEADER_PID=""
    rm -rf "$tmpdir"
    return 1
  fi
  HEALTH_CHECK_OUTPUT="$(cat "$out" 2>/dev/null || true)"
  PROVISION_PGID=""
  PROVISION_LEADER_PID=""
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

# Flake signatures. Every grep reads a here-string rather than a pipe: under
# `pipefail`, `grep -q` exiting on its first match would SIGPIPE a large writer
# and turn a match into a miss.
#
# TRANSPORT_FLAKE_DISQUALIFIER_RE: assertions, contract/schema mismatches, and
# application response statuses. Any match keeps a failure blocking. Gateway
# statuses (502/503/504) are judged per failure instead, against
# TRANSPORT_FLAKE_GATEWAY_SOURCE_RE.
TRANSPORT_FLAKE_DISQUALIFIER_RE='assertionerror|assert .*failed|^E[[:space:]]+assert[[:space:]]|[[:space:]]-[[:space:]]assert[[:space:]]|^E[[:space:]]+Failed:[[:space:]]|expected .*(got|but)|^[[:space:]]*expected:|^[[:space:]]*received:|expect\(.*\)\.(to|not)|api mismatch|validationerror|(status[_ ]?code|http/[0-9.]+|<response \[|returned|status)[^0-9]{0,12}\b(400|401|403|404|409|422|500)\b'
# TRANSPORT_FLAKE_GATEWAY_SOURCE_RE: a 502/503/504 attributed to the ingress
# controller or the Kubernetes control plane. A failure reason carrying a
# gateway status without this source is not a transport flake.
TRANSPORT_FLAKE_GATEWAY_STATUS_RE='\b(502|503|504)\b'
TRANSPORT_FLAKE_GATEWAY_SOURCE_RE='(ingress-nginx|ingress controller|nginx).{0,40}\b(502|503|504)\b|\b(502|503|504)\b.{0,80}<center>nginx</center>|(kubernetes|kube-apiserver|apiserver|control[- ]plane|gke).{0,60}\b(502|503|504)\b'
# TRANSPORT_FLAKE_ALLOWLIST_RE: the closed spec allowlist. Client-side
# transport errors; control-plane-sourced statuses and timeouts; pod/node
# lifecycle events; ingress-controller-sourced gateway statuses.
TRANSPORT_FLAKE_ALLOWLIST_RE='econnreset|econnrefused|etimedout|eai_again|temporary failure in name resolution|upstream reset|connectionrefusederror|connection refused|connect call failed|connecterror|all connection attempts failed|(kubernetes|kube-apiserver|apiserver|control[- ]plane|gke).{0,60}(\b(429|500|502|503|504)\b|i/o timeout|context deadline exceeded|tls handshake timeout|connection reset)|pod.{0,80}\b(evicted|preempted)\b|reason:[[:space:]]*(evicted|preempted)\b|\bnodenotready\b|(ingress-nginx|ingress controller|nginx).{0,40}\b(502|503|504)\b|\b(502|503|504)\b.{0,80}<center>nginx</center>'

# is_environmental_transport_failure <text>
# True only when text matches the allowlist and nothing in it disqualifies.
# Unrecognized or ambiguous text (e.g. a bare gateway status with no ingress
# or control-plane source) stays blocking.
is_environmental_transport_failure() {
  local text="$1"
  grep -Eqi "$TRANSPORT_FLAKE_DISQUALIFIER_RE" <<< "$text" && return 1
  if grep -Eqi "$TRANSPORT_FLAKE_GATEWAY_STATUS_RE" <<< "$text"; then
    grep -Eqi "$TRANSPORT_FLAKE_GATEWAY_SOURCE_RE" <<< "$text" || return 1
  fi
  grep -Eqi "$TRANSPORT_FLAKE_ALLOWLIST_RE" <<< "$text"
}

# transport_flake_category <text>
# Name the allowlisted category an (already qualifying) failure reason
# matched, for the public flake notice.
transport_flake_category() {
  local text="$1"
  if grep -Eqi '(kubernetes|kube-apiserver|apiserver|control[- ]plane|gke)' <<< "$text"; then
    printf 'control-plane request failure'
  elif grep -Eqi 'evicted|preempted|nodenotready' <<< "$text"; then
    printf 'pod eviction/preemption or node not ready'
  elif grep -Eqi "$TRANSPORT_FLAKE_GATEWAY_STATUS_RE" <<< "$text"; then
    printf 'ingress gateway error'
  elif grep -Eqi 'eai_again|temporary failure in name resolution' <<< "$text"; then
    printf 'DNS resolution failure'
  else
    printf 'client connection refused/reset/timeout'
  fi
}

# record_heartbeat_stage_pass <stage> <sha>
# Record an executor-observed passing cluster stage against the exact commit
# it ran on. Only a full 40-hex sha is recorded.
record_heartbeat_stage_pass() {
  local stage="$1" sha="$2"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
  HEARTBEAT_STAGE_PASSES="${HEARTBEAT_STAGE_PASSES:-}${stage}"$'\t'"${sha}"$'\n'
}

# heartbeat_stage_passed_at <stage> <sha>
# True when this heartbeat's executor recorded <stage> passing at exactly <sha>.
heartbeat_stage_passed_at() {
  local stage="$1" sha="$2" entry_stage entry_sha
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 1
  while IFS=$'\t' read -r entry_stage entry_sha; do
    [[ "$entry_stage" == "$stage" && "$entry_sha" == "$sha" ]] && return 0
  done <<< "${HEARTBEAT_STAGE_PASSES:-}"
  return 1
}

# executor_test_head
# Print HEAD for a pass record or deployed-artifact binding, or nothing when the
# worktree has any modified or untracked file (then the sha would not describe
# what ran or was built). Nothing is excluded: test artifacts (pytest cache,
# Playwright reports/auth, node_modules) are gitignored and so never listed.
executor_test_head() {
  local head status
  head=$(git rev-parse HEAD 2>/dev/null) || return 0
  status=$(git status --porcelain --untracked-files=all 2>/dev/null) || return 0
  [[ -z "$status" ]] || return 0
  printf '%s' "$head"
}

# current_head
# Print HEAD, or nothing when it cannot be resolved.
current_head() {
  git rev-parse HEAD 2>/dev/null || true
}

# is_cluster_health_abort <output>
# True when an integration pytest session aborted at the conftest session-start
# health gate (require_server) before any test ran. That is an infrastructure
# condition, never a branch-attributable failure or a flake.
is_cluster_health_abort() {
  local output="$1"
  grep -Eq 'helm-charts/bin/health-check\.sh (failed \(exit|did not finish within)' <<< "$output"
}

# integration_health_abort_blocked <issue> <branch> <stage> <exit> <output>
# Returns 0 after releasing the required lock and posting the blocked notice
# when a non-zero integration run aborted at the harness health gate; the
# caller then records exit 2 and stops. Returns 1 otherwise.
integration_health_abort_blocked() {
  local issue_number="$1" branch="$2" stage="$3" exit_code="$4" output="$5"
  [[ "$exit_code" -ne 0 ]] || return 1
  is_cluster_health_abort "$output" || return 1
  release_required_dev_lock
  regression_blocked "$issue_number" "integration harness health gate failed during ${stage}" "$branch" || true
  return 0
}

# dev_env_probe_healthy <env_file>
# Post-stage health probe for flake classification. Unlike dev_env_healthy it
# never provisions or reinstalls, and it fails closed: a flake needs positive
# evidence, so a missing checkout, script, or env file is unhealthy.
dev_env_probe_healthy() {
  local env_file="$1" script health_exit=0
  [[ -n "${REPO_DIR:-}" && -n "$env_file" ]] || { warn "Post-stage health probe cannot run: REPO_DIR or env file is unset."; return 1; }
  script="${REPO_DIR}/helm-charts/bin/health-check.sh"
  [[ -f "$script" ]] || { warn "Post-stage health probe cannot run: health-check.sh not found."; return 1; }
  info "Running post-stage dev-env health probe (no provisioning)..."
  run_health_check "$script" "$env_file" || health_exit=$?
  if [[ "$health_exit" -ne 0 ]]; then
    warn "Post-stage dev-env health probe failed (exit ${health_exit})."
    return 1
  fi
  info "Post-stage dev-env health probe passed."
  return 0
}

# pytest clips short-summary reasons to the terminal width (80 columns when not
# a TTY). Regression runs widen it so every FAILED/ERROR line keeps the reason
# that the listing and per-failure flake classification read.
PYTEST_REPORT_COLUMNS=1000

# failed_test_output_kind <stage>
# Map a regression stage name to its output parser; prints nothing for a stage
# without test identifiers (deploys) or an unknown stage.
failed_test_output_kind() {
  case "$1" in
    "Unit (Python)"|"Integration (spot)"|"Integration (api-wired)") printf 'pytest' ;;
    "E2E") printf 'playwright' ;;
    "Static (ruff)") printf 'ruff' ;;
    "Static (mypy)") printf 'mypy' ;;
    "Static (frontend typecheck)"|"Static (E2E typecheck)") printf 'tsc' ;;
    "Static (frontend eslint)") printf 'eslint' ;;
    "Unit (frontend)") printf 'vitest' ;;
  esac
}

# parse_failed_test_entries <kind> <output>
# Shared parser for the PR listing and flake classification, so both see the
# same failures. Prints raw "E<TAB>identifier<TAB>reason" lines (pytest short
# summary FAILED/ERROR lines; Playwright numbered failure headers with their
# first Error: line; static-check diagnostics; Vitest failures) and
# "S<TAB>text" summary count lines. Only tabs are normalized and ANSI colour
# sequences stripped; no escaping happens here. sed and awk both consume their
# whole input, so the pipeline has no early-exit reader.
parse_failed_test_entries() {
  local kind="$1" output="$2"
  [[ -n "$kind" ]] || return 0
  # sed rather than ${var//}: bash pattern substitution is slow on multi-MB logs.
  # shellcheck disable=SC2001
  sed $'s/\x1b\\[[0-9;]*[A-Za-z]//g' <<< "$output" | awk -v kind="$kind" '
    function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
    function clean(s) { gsub(/\t/, " ", s); return trim(s) }
    function emit(id, reason) {
      id = clean(id)
      if (id == "" || seen[id]++) return
      printf "E\t%s\t%s\n", id, clean(reason)
    }
    function summary(s) { s = clean(s); if (s != "" && !seen_summary[s]++) printf "S\t%s\n", s }
    kind == "pytest" {
      if ($0 ~ /^(FAILED|ERROR) /) {
        line = $0
        sub(/^(FAILED|ERROR) /, "", line)
        i = index(line, " - ")
        if (i > 0) emit(substr(line, 1, i - 1), substr(line, i + 3)); else emit(line, "")
      } else if ($0 ~ /^=+ .*(failed|error|passed).* in [0-9.]+s/) {
        line = $0; gsub(/^=+ /, "", line); gsub(/ =+$/, "", line); summary(line)
      }
      next
    }
    kind == "playwright" {
      if ($0 ~ /^[ \t]*[0-9]+\) \[[^]]+\] › /) {
        if (pending != "") emit(pending, "")
        line = $0; sub(/^[ \t]*[0-9]+\) /, "", line); gsub(/[ \t]*(─)+[ \t]*$/, "", line)
        pending = line
      } else if (pending != "" && $0 ~ /^[ \t]*Error:/) {
        emit(pending, $0); pending = ""
      } else if ($0 ~ /^[ \t]*[0-9]+ (failed|flaky)/) {
        if (pending != "") { emit(pending, ""); pending = "" }
        summary($0)
      }
      next
    }
    kind == "ruff" {
      if ($0 ~ /^[^ \t]+:[0-9]+:[0-9]+: [A-Z]+[0-9]+ /) {
        i = index($0, ": "); emit(substr($0, 1, i - 1), substr($0, i + 2))
      } else if ($0 ~ /^[A-Z]+[0-9]+ /) {
        rule = $0
      } else if (rule != "" && $0 ~ /^[ \t]*--> [^ \t]+:[0-9]+:[0-9]+/) {
        line = $0; sub(/^[ \t]*--> /, "", line); emit(line, rule); rule = ""
      }
      next
    }
    kind == "mypy" {
      if ($0 ~ /^[^ \t]+:[0-9]+: error: /) {
        i = index($0, ": error: "); emit(substr($0, 1, i - 1), substr($0, i + 9))
      }
      next
    }
    kind == "tsc" {
      if ($0 ~ /^[^ \t]+\([0-9]+,[0-9]+\): error TS[0-9]+/) {
        i = index($0, "): error "); loc = substr($0, 1, i)
        sub(/\(/, ":", loc); sub(/,/, ":", loc); sub(/\)$/, "", loc)
        emit(loc, substr($0, i + 9))
      }
      next
    }
    kind == "eslint" {
      if ($0 ~ /^[ \t]*[0-9]+:[0-9]+[ \t]+(error|Error:)[ \t]/) {
        line = trim($0); split(line, parts, /[ \t]+/); pos = parts[1]
        sub(/^[0-9]+:[0-9]+[ \t]+(error|Error:)[ \t]+/, "", line)
        emit((file != "" ? file ":" : "") pos, line)
      } else if ($0 ~ /^[^ \t✖]/ && $0 !~ /^[0-9]+:[0-9]+/ && $0 !~ /^(>|info|warn|Warning|ESLint)/) {
        file = trim($0)
      }
      next
    }
    kind == "vitest" {
      if ($0 ~ /^[ \t]*(FAIL|×|✗) /) {
        if (pending != "") emit(pending, "")
        line = $0; sub(/^[ \t]*(FAIL|×|✗) +/, "", line); sub(/ [0-9]+ms$/, "", line)
        pending = line
      } else if (pending != "" && $0 ~ /^[ \t]*([A-Za-z]*Error:|→ )/) {
        line = $0; sub(/^[ \t]*→ /, "", line); emit(pending, line); pending = ""
      }
      next
    }
    END { if (pending != "") emit(pending, "") }
  '
}

# sanitize_failed_test_entries <parsed_entries>
# Neutralize already-scrubbed parser entries for markdown rendering. Invalid
# UTF-8 becomes U+FFFD. Identifiers are restricted to a conservative charset
# (anything else becomes '?'; '>' is kept for Vitest name paths and is inert
# inside a code span); reasons and summaries have backticks replaced so
# they stay inside their code spans. Every field is clipped to 200 characters
# (whole characters, never a split byte sequence).
sanitize_failed_test_entries() {
  local parsed="$1"
  # shellcheck disable=SC2016
  perl -MEncode=decode,encode -e '
    sub clip { my ($s) = @_; return length($s) > 200 ? substr($s, 0, 200) : $s }
    while (my $line = <STDIN>) {
      chomp $line;
      my ($tag, $first, $second) = split /\t/, $line, 3;
      next unless defined $tag && ($tag eq "E" || $tag eq "S");
      my $a = decode("UTF-8", $first // "");
      my $b = decode("UTF-8", $second // "");
      if ($tag eq "E") {
        $a =~ s{[^A-Za-z0-9_./:\[\]()=,+\- \@*>\x{203A}]}{?}g;
        $b =~ tr/`/\x27/;
        print encode("UTF-8", "E\t" . clip($a) . "\t" . clip($b)), "\n";
      } else {
        $a =~ tr/`/\x27/;
        print encode("UTF-8", "S\t" . clip($a)), "\n";
      }
    }
  ' <<< "$parsed"
}

# extract_failed_tests <stage> <output> [with_reasons]
# Print a bounded, secret-scrubbed markdown bullet list of the failing test
# identifiers in one stage's output: pytest node IDs, Playwright test titles,
# Vitest test names, or static-check diagnostics as path:line entries. With
# with_reasons=false (flake notices) only identifiers are listed. Raw entries
# are scrubbed before any neutralization or clipping, and the rendered block is
# scrubbed again. Identifiers, reasons, and summaries each sit in their own
# code span, so mentions, links, images, issue references, and HTML are inert.
FAILED_TESTS_MAX_ENTRIES=50
FAILED_TESTS_MAX_SUMMARIES=5
extract_failed_tests() {
  local stage="$1" output="$2" with_reasons="${3:-true}"
  local kind parsed=""
  case "$stage" in
    "Deploy (API)"|"Deploy (frontend)")
      printf '%s\n' "- (deploy stage; no test identifiers)"; return 0 ;;
  esac
  if ! command -v perl >/dev/null 2>&1; then
    printf '%s\n' "- Failing test identifiers withheld: the output sanitizer is unavailable."; return 0
  fi
  kind=$(failed_test_output_kind "$stage")
  if [[ -n "$kind" ]]; then
    parsed=$(parse_failed_test_entries "$kind" "$output")
    parsed=$(scrub_secrets "$parsed")
    parsed=$(sanitize_failed_test_entries "$parsed") || parsed=""
  fi

  local rendered="" total=0 shown=0 summaries="" summary_count=0 tag id reason
  while IFS=$'\t' read -r tag id reason; do
    case "$tag" in
      E)
        total=$((total + 1))
        [[ "$total" -le "$FAILED_TESTS_MAX_ENTRIES" ]] || continue
        shown=$((shown + 1))
        if [[ "$with_reasons" == true && -n "$reason" ]]; then
          rendered="${rendered}- \`${id}\` — \`${reason}\`"$'\n'
        elif [[ "$with_reasons" == true ]]; then
          rendered="${rendered}- \`${id}\` — no reason reported"$'\n'
        else
          rendered="${rendered}- \`${id}\`"$'\n'
        fi
        ;;
      S)
        summary_count=$((summary_count + 1))
        [[ "$summary_count" -le "$FAILED_TESTS_MAX_SUMMARIES" ]] || continue
        summaries="${summaries}- Summary: \`${id}\`"$'\n'
        ;;
    esac
  done <<< "$parsed"

  if [[ "$shown" -eq 0 ]]; then
    rendered="- No failing test identifiers could be extracted from this stage's output."$'\n'
  elif [[ "$total" -gt "$shown" ]]; then
    rendered="${rendered}- … and $((total - shown)) more"$'\n'
  fi
  rendered="${rendered}${summaries}"
  scrub_secrets "${rendered%$'\n'}"
}

# render_failed_tests_block <stage> <exit_code> <output> <with_reasons>
# One collapsible block per stage. The heading is built only from the fixed
# internal stage name and a numeric exit code (empty for identifier-only flake
# listings); it never carries branch or output text.
render_failed_tests_block() {
  local stage="$1" exit_code="$2" output="$3" with_reasons="$4" heading
  heading="$stage"
  [[ "$exit_code" =~ ^[0-9]+$ ]] && heading="${stage} — failed (exit ${exit_code})"
  printf '<details><summary>%s</summary>\n\n%s\n\n</details>' "$heading" "$(extract_failed_tests "$stage" "$output" "$with_reasons")"
}

# truncate_failed_tests_block <block> <max_chars>
# Clip a single rendered block by whole lines so it fits max_chars, keeping the
# opening <details> line and closing </details> tag.
truncate_failed_tests_block() {
  local block="$1" max_chars="$2" out="" line first=true
  local footer=$'- Listing truncated (size limit).\n\n</details>'
  [[ ${#block} -le $max_chars ]] && { printf '%s' "$block"; return 0; }
  while IFS= read -r line; do
    if [[ "$first" == true ]]; then out="$line"; first=false; continue; fi
    [[ "$line" == "</details>" ]] && break
    (( ${#out} + ${#line} + 1 + ${#footer} + 1 <= max_chars )) || break
    out="${out}"$'\n'"${line}"
  done <<< "$block"
  printf '%s\n%s' "$out" "$footer"
}

# append_failed_tests_block <var_name> <block>
# Append one rendered stage block to an accumulator, bounded so a PR comment
# stays within size limits. A first block larger than the cap is clipped by
# whole lines; later blocks are appended whole (keeping <details> tags
# balanced) and, once the cap would be exceeded, a single omission line is
# added instead.
FAILED_TESTS_DETAILS_MAX_CHARS=20000
append_failed_tests_block() {
  local var_name="$1" block="$2" current marker="- Further failed-stage listings omitted (size limit)."
  current="${!var_name:-}"
  if [[ -z "$current" ]]; then
    printf -v "$var_name" '%s' "$(truncate_failed_tests_block "$block" "$FAILED_TESTS_DETAILS_MAX_CHARS")"
    return 0
  fi
  if [[ $(( ${#current} + ${#block} )) -gt "$FAILED_TESTS_DETAILS_MAX_CHARS" ]]; then
    case "$current" in
      *"$marker") ;;
      *) printf -v "$var_name" '%s\n\n%s' "$current" "$marker" ;;
    esac
    return 0
  fi
  printf -v "$var_name" '%s\n\n%s' "$current" "$block"
}

# stage_failures_are_transport_flakes <stage> <output>
# Per-failure, fail-closed flake signature check for a cluster stage. The
# whole output must carry no disqualifier, at least one failure must be
# extracted (pytest short-summary FAILED/ERROR lines, or Playwright failure
# headers), and every extracted failure's own reason must match the allowlist
# without a disqualifier.
stage_failures_are_transport_flakes() {
  local stage="$1" output="$2" kind entries tag id reason count=0
  case "$stage" in
    "Integration (spot)"|"Integration (api-wired)") kind=pytest ;;
    "E2E") kind=playwright ;;
    *) return 1 ;;
  esac
  grep -Eqi "$TRANSPORT_FLAKE_DISQUALIFIER_RE" <<< "$output" && return 1
  entries=$(parse_failed_test_entries "$kind" "$output")
  while IFS=$'\t' read -r tag id reason; do
    [[ "$tag" == E ]] || continue
    count=$((count + 1))
    [[ -n "$reason" ]] || return 1
    is_environmental_transport_failure "$reason" || return 1
  done <<< "$entries"
  [[ "$count" -gt 0 ]]
}

# stage_transport_flake_categories <stage> <output>
# Distinct allowlisted categories across a qualifying stage's failure reasons.
stage_transport_flake_categories() {
  local stage="$1" output="$2" kind entries tag id reason category categories=""
  case "$stage" in
    "Integration (spot)"|"Integration (api-wired)") kind=pytest ;;
    "E2E") kind=playwright ;;
    *) return 0 ;;
  esac
  entries=$(parse_failed_test_entries "$kind" "$output")
  while IFS=$'\t' read -r tag id reason; do
    [[ "$tag" == E && -n "$reason" ]] || continue
    category=$(transport_flake_category "$reason")
    case "|${categories}|" in
      *"|${category}|"*) ;;
      *) categories="${categories:+${categories}|}${category}" ;;
    esac
  done <<< "$entries"
  printf '%s' "${categories//|/, }"
}

# record_post_pr_flake_classification <stage> <output>
# A failed cluster stage is an environmental flake only when every failure is
# an allowlisted transport signature AND this heartbeat's executor recorded the
# same stage passing at exactly the commit the post-PR regression tested.
# Changed paths are never a basis: the initial full regression has no
# preceding fix.
record_post_pr_flake_classification() {
  local stage="$1" output="$2"
  case "$stage" in
    "Integration (spot)"|"Integration (api-wired)"|"E2E")
      if stage_failures_are_transport_flakes "$stage" "$output" && \
         heartbeat_stage_passed_at "$stage" "${POST_PR_REGRESSION_HEAD:-}"; then
        POST_PR_FLAKE_STAGES="${POST_PR_FLAKE_STAGES:+${POST_PR_FLAKE_STAGES}, }${stage}"
        POST_PR_FLAKE_CATEGORIES="${POST_PR_FLAKE_CATEGORIES:+${POST_PR_FLAKE_CATEGORIES}; }${stage}: $(stage_transport_flake_categories "$stage" "$output")"
        return 0
      fi
      ;;
  esac
  POST_PR_NON_FLAKE_STAGES="${POST_PR_NON_FLAKE_STAGES:+${POST_PR_NON_FLAKE_STAGES}, }${stage}"
  return 1
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
      # Keep stage-specific, bounded evidence for the one post-regression fix
      # session.  It is intentionally local-only: public PR status comments
      # name failed stages but never expose raw test output.
      POST_PR_FAILURE_EVIDENCE="${POST_PR_FAILURE_EVIDENCE:+${POST_PR_FAILURE_EVIDENCE}

}=== ${stage} (exit ${exit_code}) ===
$(tail_chars "$output" 14000)"
      # The public listing carries only extracted, scrubbed identifiers and
      # one-line reasons; flake notices carry identifiers only.
      append_failed_tests_block POST_PR_FAILED_TEST_DETAILS \
        "$(render_failed_tests_block "$stage" "$exit_code" "$output" true)"
      if record_post_pr_flake_classification "$stage" "$output"; then
        append_failed_tests_block POST_PR_FLAKE_TEST_IDS \
          "$(render_failed_tests_block "$stage" "" "$output" false)"
      fi
    fi
    return 0
  fi
  get_pr_number_for_branch "$branch"
  [[ -n "$BRANCH_PR_NUMBER" ]] && post_test_results_comment "$BRANCH_PR_NUMBER" "$stage" "$exit_code" "$output"
}

# stage_is_recorded <stage>
# POST_PR_FAILED_STAGES is a human-readable comma-separated list assembled by
# post_result.  Match whole entries so similarly named stages cannot select one
# another by accident.
stage_is_recorded() {
  local stage="$1" entry
  local -a _prauto_stage_entries
  IFS=',' read -r -a _prauto_stage_entries <<< "${POST_PR_FAILED_STAGES:-}"
  # An empty array expansion is unbound under `set -u` in bash 3.2.
  [[ "${#_prauto_stage_entries[@]}" -gt 0 ]] || return 1
  for entry in "${_prauto_stage_entries[@]}"; do
    entry="${entry# }"; entry="${entry% }"
    [[ "$entry" == "$stage" ]] && return 0
  done
  return 1
}

# require_pushed_head <branch>
# Targeted verification must test precisely the head the executor published,
# never an unpushed worker commit or an asynchronously changed remote ref.
require_pushed_head() {
  local branch="$1" local_head remote_head
  local_head=$(git rev-parse HEAD 2>/dev/null || printf '')
  remote_head=$(git ls-remote origin "refs/heads/${branch}" 2>/dev/null | awk 'NR == 1 { print $1 }')
  if [[ -z "$local_head" || -z "$remote_head" || "$local_head" != "$remote_head" ]]; then
    warn "Targeted regression refuses to run: local HEAD and origin/${branch} do not match."
    return 1
  fi
  return 0
}

# targeted_verification_json <agent_output>
# Extract only an explicitly prefixed, single-line JSON record. Agent prose and
# untrusted test output are never parsed as verification authority.
targeted_verification_json() {
  local agent_output="$1"
  printf '%s\n' "$agent_output" | sed -n 's/^PRAUTO_TARGETED_VERIFICATION_JSON: //p' | tail -n 1
}

# validate_targeted_verification <agent_output> <expected_revision>
# Accept a worker attestation only when it is well-formed, names exactly the
# originally failed stages, reports pass for each, and binds them to the local
# committed revision which the executor subsequently pushes and re-verifies.
validate_targeted_verification() {
  local agent_output="$1" expected_revision="$2" payload stage expected_count=0 actual_count
  local -a _prauto_expected_stage_entries
  payload=$(targeted_verification_json "$agent_output")
  [[ -n "$payload" ]] || return 1
  jq -e --arg revision "$expected_revision" '
    (.revision == $revision)
    and (.stages | type == "array")
    and all(.stages[]; (.name | type == "string" and length > 0)
        and (.outcome == "pass")
        and (.evidence_ref | type == "string" and length > 0))
  ' >/dev/null 2>&1 <<< "$payload" || return 1
  actual_count=$(jq '.stages | length' <<< "$payload" 2>/dev/null) || return 1
  local stage_entry
  IFS=',' read -r -a _prauto_expected_stage_entries <<< "${POST_PR_FAILED_STAGES:-}"
  # An attestation over zero recorded stages verifies nothing (and an empty
  # array expansion is unbound under `set -u` in bash 3.2).
  [[ "${#_prauto_expected_stage_entries[@]}" -gt 0 ]] || return 1
  for stage_entry in "${_prauto_expected_stage_entries[@]}"; do
    stage_entry="${stage_entry# }"; stage_entry="${stage_entry% }"
    [[ -n "$stage_entry" ]] || continue
    expected_count=$((expected_count + 1))
    jq -e --arg stage "$stage_entry" '[.stages[] | select(.name == $stage)] | length == 1' \
      >/dev/null 2>&1 <<< "$payload" || return 1
  done
  [[ "$expected_count" -gt 0 ]] || return 1
  [[ "$actual_count" -eq "$expected_count" ]]
}

# post_post_pr_regression_comment <branch> <body>
# Regression status belongs to the PR conversation, not its linked issue.
# The body is caller-supplied summary text. Beyond fixed status wording it may
# carry only the bounded, secret-scrubbed failed-test listing produced by
# extract_failed_tests and the sanitized targeted evidence from
# targeted_failure_comment_evidence — never raw command output. The body is
# passed through a private temporary file, not argv, so it is neither exposed
# in the process table nor limited by argument size.
post_post_pr_regression_comment() {
  local branch="$1" body="$2" body_dir rc=0
  get_pr_number_for_branch "$branch"
  if [[ -z "${BRANCH_PR_NUMBER:-}" ]]; then
    warn "No PR found for branch ${branch}; could not post regression status."
    return 1
  fi
  if ! body_dir=$(mktemp -d); then
    warn "Could not create a temp dir for the regression status on PR #${BRANCH_PR_NUMBER}."
    return 1
  fi
  chmod 700 "$body_dir" 2>/dev/null || true
  if ! printf 'prauto(%s): %s' "$PRAUTO_WORKER_ID" "$body" > "${body_dir}/body.md"; then
    rm -rf "$body_dir"
    warn "Could not write the regression status body for PR #${BRANCH_PR_NUMBER}."
    return 1
  fi
  gh pr comment "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --body-file "${body_dir}/body.md" 2>/dev/null || rc=$?
  rm -rf "$body_dir"
  if [[ "$rc" -ne 0 ]]; then
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
    exit_code=0; output=$(pnpm -C src/frontend run lint 2>&1) || exit_code=$?
    post_result "$branch" "Static (frontend eslint)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  fi
  if diff_touches tests/e2e/; then
    exit_code=0; output=$(pnpm -C tests/e2e typecheck 2>&1) || exit_code=$?
    post_result "$branch" "Static (E2E typecheck)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  fi

  [[ -d tests/unit ]] || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="tests/unit is missing"; return 0; }
  exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" uv run pytest tests/unit/ --tb=short 2>&1) || exit_code=$?
  post_result "$branch" "Unit (Python)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  if diff_touches src/frontend/; then
    exit_code=0; output=$(pnpm -C src/frontend test 2>&1) || exit_code=$?
    post_result "$branch" "Unit (frontend)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || rc=1
  fi
  LOCAL_REGRESSION_EXIT="$rc"
}

# acquire_required_dev_lock <issue> <purpose>
# Sets REQUIRED_LOCK_OWNER only once the lock is actually held, so a release
# (including the heartbeat EXIT trap's) never targets a lock this worker does
# not own. Every failure is a blocked result: a required regression must never
# become a passing skip.
acquire_required_dev_lock() {
  local issue_number="$1" purpose="$2" lock_code
  local owner="prauto-${PRAUTO_WORKER_ID}"
  REQUIRED_LOCK_OWNER=""
  if ! resolve_dev_env; then regression_blocked "$issue_number" "dev env file is unavailable" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then regression_blocked "$issue_number" "dev cluster health/provisioning failed" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  if ! curl -s --connect-timeout 2 "${DEV_LOCK_URL}/status" >/dev/null 2>&1; then
    regression_blocked "$issue_number" "dev-env lock endpoint is unreachable" "${CURRENT_REGRESSION_BRANCH:-}"; return 1
  fi
  lock_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${DEV_LOCK_URL}/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"owner\": \"${owner}\", \"message\": \"prauto ${purpose} for issue #${issue_number}\"}")
  if [[ "$lock_code" != 200 ]]; then regression_blocked "$issue_number" "dev-env lock acquisition returned HTTP ${lock_code}" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  REQUIRED_LOCK_OWNER="$owner"
  return 0
}

# release_required_dev_lock
# Idempotent: the owner is cleared after one release attempt, so a later call
# (e.g. from the heartbeat EXIT trap) is a no-op. Bounded so the trap cannot hang
# on an unreachable lock endpoint.
release_required_dev_lock() {
  [[ -n "${DEV_LOCK_URL:-}" && -n "${REQUIRED_LOCK_OWNER:-}" ]] || return 0
  local owner="$REQUIRED_LOCK_OWNER"
  REQUIRED_LOCK_OWNER=""
  curl -s --connect-timeout 5 --max-time 30 -X POST "${DEV_LOCK_URL}/release" -H "Content-Type: application/json" \
    -d "{\"owner\": \"${owner}\"}" >/dev/null 2>&1 || warn "Failed to release dev-env lock."
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
  # acquire_required_dev_lock has just completed the required pre-test health
  # check. Preserve that executor-owned fact for possible flake classification.
  POST_PR_FLAKE_HEALTH_BEFORE=true

  if ! deploy_branch_api "$DEV_ENV_FILE"; then
    release_required_dev_lock
    if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then
      CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "API deploy could not reach or operate the development environment" "$branch"
    else
      CLUSTER_REGRESSION_EXIT=1; post_result "$branch" "Deploy (API)" 1 "Branch API build/deploy failed."
    fi
    return 0
  fi
  # A session aborted at the integration harness health gate ran no test: it is
  # infrastructure-blocked and ends this regression without recording a stage.
  exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/spot/ --tb=short 2>&1) || exit_code=$?
  if integration_health_abort_blocked "$issue_number" "$branch" "Integration (spot)" "$exit_code" "$output"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
  post_result "$branch" "Integration (spot)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || exit_code=$?
  if integration_health_abort_blocked "$issue_number" "$branch" "Integration (api-wired)" "$exit_code" "$output"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
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

# record_targeted_result <stage> <exit> <output>
# Unlike the initial full regression recorder, this deliberately distinguishes
# successful targeted retries in the final PR status.
record_targeted_result() {
  local stage="$1" exit_code="$2" output="$3"
  if [[ "$exit_code" -eq 0 ]]; then
    POST_PR_TARGETED_PASSES="${POST_PR_TARGETED_PASSES:+${POST_PR_TARGETED_PASSES}, }${stage}"
  else
    POST_PR_TARGETED_FAILURES="${POST_PR_TARGETED_FAILURES:+${POST_PR_TARGETED_FAILURES}, }${stage}"
    POST_PR_TARGETED_EVIDENCE="${POST_PR_TARGETED_EVIDENCE:+${POST_PR_TARGETED_EVIDENCE}

}=== ${stage} (exit ${exit_code}) ===
$(tail_chars "$output" 14000)"
    append_failed_tests_block POST_PR_TARGETED_FAILED_TEST_DETAILS \
      "$(render_failed_tests_block "$stage" "$exit_code" "$output" true)"
  fi
}

# Render bounded, secret-scrubbed executor evidence for a public PR comment.
targeted_failure_comment_evidence() {
  local safe body max_chars=12000
  # Scrub the complete evidence before truncating, so a cut can never split a
  # secret into an unrecognizable fragment; drop the partial first line the
  # cut leaves, then scrub the bounded text again.
  safe=$(scrub_secrets "${POST_PR_TARGETED_EVIDENCE:-}")
  if [[ ${#safe} -gt $max_chars ]]; then
    body="${safe: -max_chars}"
    [[ "$body" == *$'\n'* ]] && body="${body#*$'\n'}"
    safe="(truncated — last ${#body} characters)"$'\n'"${body}"
  fi
  safe=$(scrub_secrets "$safe")
  safe=$(printf '%s' "$safe" | sed 's/```/`&#8203;``/g')
  printf '%s' "$safe"
}

# run_targeted_post_pr_regression <issue> <branch>
# Re-run only stages which failed the initial full regression.  Cluster stages
# reacquire the lock and rebuild their prerequisite artifact from the exact
# pushed branch head.  Sets TARGETED_REGRESSION_EXIT: 0 pass, 1 code failure,
# 2 setup/infrastructure block.
run_targeted_post_pr_regression() {
  local issue_number="$1" branch="$2" output exit_code=0 targeted_head
  TARGETED_REGRESSION_EXIT=0
  POST_PR_TARGETED_PASSES=""; POST_PR_TARGETED_FAILURES=""; POST_PR_TARGETED_EVIDENCE=""
  POST_PR_TARGETED_FAILED_TEST_DETAILS=""
  CURRENT_REGRESSION_BRANCH="$branch"
  # A retry with nothing recorded to verify can never be readiness success.
  local recorded_stages="${POST_PR_FAILED_STAGES:-}"
  if [[ -z "${recorded_stages//[[:space:],]/}" ]]; then
    TARGETED_REGRESSION_EXIT=2
    regression_blocked "$issue_number" "targeted regression invoked with no recorded failed stages" "$branch"
    return 0
  fi
  require_pushed_head "$branch" || { TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "pushed branch head could not be verified" "$branch"; return 0; }
  # All success evidence below is executor-observed. Bind the entire targeted
  # run to an already-pushed revision before any command starts, then repeat
  # this check after the selected stages complete. A test runner may create
  # ignored artifacts, so cleanliness is checked before the worker's push;
  # it is not a meaningful post-test criterion. An agent attestation is only
  # an admission ticket to this executor retry; it can never supply pass
  # evidence or bridge a moved head.
  targeted_head=$(current_head)
  if [[ -z "$targeted_head" ]]; then
    TARGETED_REGRESSION_EXIT=2
    regression_blocked "$issue_number" "targeted regression could not resolve the executor head" "$branch"
    return 0
  fi

  # Local checks: dependency sync is setup, while every selected check is a
  # branch-attributable target. Do not rerun an unrelated successful gate.
  if stage_is_recorded "Static (ruff)" || stage_is_recorded "Static (mypy)" || \
     stage_is_recorded "Static (frontend typecheck)" || stage_is_recorded "Static (frontend eslint)" || \
     stage_is_recorded "Static (E2E typecheck)" || stage_is_recorded "Unit (Python)" || \
     stage_is_recorded "Unit (frontend)"; then
    output=$(uv sync 2>&1) || { TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "uv sync failed during targeted regression" "$branch"; return 0; }
  fi
  if stage_is_recorded "Static (ruff)"; then
    exit_code=0; output=$(uv run ruff check src/ tests/ 2>&1) || exit_code=$?
    record_targeted_result "Static (ruff)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi
  if stage_is_recorded "Static (mypy)"; then
    exit_code=0; output=$(uv run mypy src/ 2>&1) || exit_code=$?
    record_targeted_result "Static (mypy)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi
  if stage_is_recorded "Static (frontend typecheck)"; then
    exit_code=0; output=$(pnpm -C src/frontend exec tsc --noEmit 2>&1) || exit_code=$?
    record_targeted_result "Static (frontend typecheck)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi
  if stage_is_recorded "Static (frontend eslint)"; then
    exit_code=0; output=$(pnpm -C src/frontend run lint 2>&1) || exit_code=$?
    record_targeted_result "Static (frontend eslint)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi
  if stage_is_recorded "Static (E2E typecheck)"; then
    exit_code=0; output=$(pnpm -C tests/e2e typecheck 2>&1) || exit_code=$?
    record_targeted_result "Static (E2E typecheck)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi
  if stage_is_recorded "Unit (Python)"; then
    exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" uv run pytest tests/unit/ --tb=short 2>&1) || exit_code=$?
    record_targeted_result "Unit (Python)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi
  if stage_is_recorded "Unit (frontend)"; then
    exit_code=0; output=$(pnpm -C src/frontend test 2>&1) || exit_code=$?
    record_targeted_result "Unit (frontend)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
  fi

  # API is the prerequisite for both integration groups. If the deploy itself
  # failed initially, retry it alone; otherwise deploy before only the failed
  # integration groups.
  if stage_is_recorded "Deploy (API)" || stage_is_recorded "Integration (spot)" || stage_is_recorded "Integration (api-wired)"; then
    if ! acquire_required_dev_lock "$issue_number" "targeted regression API/integration"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
    if ! deploy_branch_api "$DEV_ENV_FILE"; then
      release_required_dev_lock
      if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "API deploy could not reach or operate the development environment" "$branch"; return 0
      else record_targeted_result "Deploy (API)" 1 "Branch API build/deploy failed."; TARGETED_REGRESSION_EXIT=1; fi
    else
      stage_is_recorded "Deploy (API)" && record_targeted_result "Deploy (API)" 0 "Branch API build/deploy passed."
      if stage_is_recorded "Integration (spot)"; then
        exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/spot/ --tb=short 2>&1) || exit_code=$?
        if integration_health_abort_blocked "$issue_number" "$branch" "Integration (spot)" "$exit_code" "$output"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
        record_targeted_result "Integration (spot)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
      fi
      if stage_is_recorded "Integration (api-wired)"; then
        exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || exit_code=$?
        if integration_health_abort_blocked "$issue_number" "$branch" "Integration (api-wired)" "$exit_code" "$output"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
        record_targeted_result "Integration (api-wired)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
      fi
      release_required_dev_lock
    fi
  fi

  if stage_is_recorded "Deploy (frontend)" || stage_is_recorded "E2E"; then
    if ! acquire_required_dev_lock "$issue_number" "targeted regression frontend/E2E"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
    if ! deploy_branch_frontend "$DEV_ENV_FILE"; then
      release_required_dev_lock
      if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "frontend deploy could not reach or operate the development environment" "$branch"; return 0
      else record_targeted_result "Deploy (frontend)" 1 "Branch frontend build/deploy failed."; TARGETED_REGRESSION_EXIT=1; fi
    else
      stage_is_recorded "Deploy (frontend)" && record_targeted_result "Deploy (frontend)" 0 "Branch frontend build/deploy passed."
      if stage_is_recorded "E2E"; then
        if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1 || ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
          release_required_dev_lock; TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "E2E runner/browser setup failed" "$branch"; return 0
        fi
        exit_code=0; output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" pnpm -C tests/e2e test 2>&1) || exit_code=$?
        record_targeted_result "E2E" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
      fi
      release_required_dev_lock
    fi
  fi

  # Do not turn successful command exits into a pass if the local or remote
  # branch moved while the executor was testing. This is deliberately an
  # infrastructure block, not a branch failure: the recorded stage result no
  # longer proves the exact pushed head and must be rerun on a later heartbeat.
  if [[ "$TARGETED_REGRESSION_EXIT" -eq 0 ]] && \
     { [[ "$(current_head)" != "$targeted_head" ]] || ! require_pushed_head "$branch"; }; then
    TARGETED_REGRESSION_EXIT=2
    regression_blocked "$issue_number" "targeted regression lost its exact clean pushed-head binding" "$branch"
    return 0
  fi
}

# run_post_pr_regression <issue> <branch>
# Run one full regression. If it finds branch-attributable failures, a single
# turn-bounded worker fixes them and the executor verifies only those recorded
# stages against the exact pushed head. A targeted pass is final; no second
# full regression is permitted.
run_post_pr_regression() {
  local issue_number="$1" branch="$2"
  if ! branch_is_code_affecting "$branch"; then
    info "Diff is confined to the explicit non-code exclusion set; full regression is not required."
    return 0
  fi
  [[ -n "$issue_number" ]] || { warn "No issue number for regression gate."; return 1; }
  POST_PR_REGRESSION_SUMMARY_MODE=true
  POST_PR_FAILED_STAGES=""; POST_PR_FAILURE_EVIDENCE=""
  POST_PR_FLAKE_STAGES=""; POST_PR_NON_FLAKE_STAGES=""; POST_PR_FLAKE_HEALTH_BEFORE=false
  POST_PR_FAILED_TEST_DETAILS=""; POST_PR_FLAKE_TEST_IDS=""; POST_PR_FLAKE_CATEGORIES=""
  # The commit every initial-regression stage tests; finalize_issue_pr has
  # just pushed it. The flake basis compares recorded passes against it.
  POST_PR_REGRESSION_HEAD=$(git rev-parse HEAD 2>/dev/null || printf '')
  run_static_and_unit_regression "$branch"
  if [[ "$LOCAL_REGRESSION_EXIT" -eq 2 ]]; then
    regression_blocked "$issue_number" "$LOCAL_REGRESSION_REASON" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  run_full_cluster_regression "$issue_number" "$branch"
  if [[ "$CLUSTER_REGRESSION_EXIT" -eq 2 ]]; then POST_PR_REGRESSION_SUMMARY_MODE=false; return 1; fi
  if [[ "$LOCAL_REGRESSION_EXIT" -eq 0 && "$CLUSTER_REGRESSION_EXIT" -eq 0 ]]; then
    if ! post_post_pr_regression_comment "$branch" "Full post-PR regression passed for the current pushed PR head."; then
      regression_blocked "$issue_number" "required regression success notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
    fi
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 0
  fi
  # A flake shortcut is deliberately narrow: every failure in every failed stage
  # must match the deterministic transport allowlist, each such stage must have
  # an executor-recorded pass from earlier in this heartbeat, bound to the
  # deployed artifact, at POST_PR_REGRESSION_HEAD, and the real dev
  # environment must be healthy both before and after.
  # The post-stage check is a probe only; it never provisions a cluster.
  # No worker is dispatched on this path, and no raw failure output is posted.
  if [[ -n "$POST_PR_FLAKE_STAGES" && -z "$POST_PR_NON_FLAKE_STAGES" && \
        "$POST_PR_FLAKE_HEALTH_BEFORE" == true && -n "$POST_PR_REGRESSION_HEAD" && \
        "$(git rev-parse HEAD 2>/dev/null || printf '')" == "$POST_PR_REGRESSION_HEAD" ]] && \
     require_pushed_head "$branch" && dev_env_probe_healthy "${DEV_ENV_FILE:-}"; then
    local flake_ids_section=""
    [[ -n "$POST_PR_FLAKE_TEST_IDS" ]] && flake_ids_section="

Failing test identifiers:

${POST_PR_FLAKE_TEST_IDS}"
    if ! post_post_pr_regression_comment "$branch" "Initial full regression reported environmental transport failures in ${POST_PR_FLAKE_STAGES} (allowlisted categories — ${POST_PR_FLAKE_CATEGORIES}). Pre- and post-test dev-environment health checks passed; the same stages passed earlier in this heartbeat against commit ${POST_PR_REGRESSION_HEAD:0:12}. Classified as an environmental flake; no coding agent was dispatched.${flake_ids_section}"; then
      regression_blocked "$issue_number" "environmental-flake status notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
    fi
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 0
  fi
  if ! post_post_pr_regression_comment "$branch" "Initial full post-PR regression failed: ${POST_PR_FAILED_STAGES:-an unclassified branch-attributable stage}. One turn-bounded coding-agent fix-and-targeted-test loop will retry only these failed stages.

Failed tests:

${POST_PR_FAILED_TEST_DETAILS:-- No failing test identifiers were recorded.}"; then
    regression_blocked "$issue_number" "required regression failure notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  info "Initial full regression failed; invoking one targeted worker fix loop."
  run_integration_fix_session "$issue_number" "$branch" "$POST_PR_FAILED_STAGES" "$POST_PR_FAILURE_EVIDENCE" post-pr
  checkpoint_branch "$issue_number" "$branch"
    # A PR already exists by this point, so derive_phase_from_github will always
    # report "pr" on the next wake — a phase the quota-resume dispatch in
    # heartbeat.sh does not know how to resume. Posting the normal resumable
    # pause marker here would strand the issue forever (paused, unresumable).
    # Defer instead: no marker, no burned fix attempt, plain retry next wake.
  if [[ "$AGENT_STATUS" == "quota" ]]; then
    warn "Issue #${issue_number}: targeted regression fix worker hit quota (${ACTIVE_AGENT}). Deferring without a second invocation."
    post_post_pr_regression_comment "$branch" "Targeted regression fix paused: ${ACTIVE_AGENT} quota is exhausted. The PR remains in prauto:wip and will retry on a later heartbeat." || true
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  local worker_revision
  worker_revision=$(git rev-parse HEAD 2>/dev/null || printf '')
  if [[ "$AGENT_STATUS" != "ok" ]] || ! validate_targeted_verification "$AGENT_OUTPUT" "$worker_revision"; then
    regression_set_wip "$issue_number" "$branch"
    post_post_pr_regression_comment "$branch" "Targeted regression fix did not produce a valid structured verification record for the committed head; the PR remains in prauto:wip." || true
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  local dirty_worktree
  dirty_worktree=$(git status --porcelain --untracked-files=all 2>/dev/null || true)
  if [[ -n "$dirty_worktree" ]]; then
    regression_set_wip "$issue_number" "$branch"
    post_post_pr_regression_comment "$branch" "Targeted regression fix left uncommitted changes; the PR remains in prauto:wip." || true
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  push_branch "$branch"
  create_or_update_pr "$issue_number" "" "$branch"
  if ! require_pushed_head "$branch"; then
    regression_blocked "$issue_number" "pushed branch head could not be verified after targeted fix" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  run_targeted_post_pr_regression "$issue_number" "$branch"
  # Exit 2 has already posted its infrastructure-blocked notice and established
  # prauto:wip; it is never readiness success and never a code-failure report.
  if [[ "$TARGETED_REGRESSION_EXIT" -eq 2 ]]; then POST_PR_REGRESSION_SUMMARY_MODE=false; return 1; fi
  if [[ "$TARGETED_REGRESSION_EXIT" -eq 0 ]]; then
    if ! post_post_pr_regression_comment "$branch" "Initial full regression failures: ${POST_PR_FAILED_STAGES}. Targeted retry passed on the current pushed PR head: ${POST_PR_TARGETED_PASSES}. No second full regression was run."; then
      regression_blocked "$issue_number" "required targeted regression success notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
    fi
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 0
  fi
  regression_set_wip "$issue_number" "$branch"
  local targeted_evidence
  targeted_evidence=$(targeted_failure_comment_evidence)
  post_post_pr_regression_comment "$branch" "Initial full regression failures: ${POST_PR_FAILED_STAGES}. Targeted retry still failed: ${POST_PR_TARGETED_FAILURES:-an unrecorded stage}. The PR remains in prauto:wip.

Failed tests:

${POST_PR_TARGETED_FAILED_TEST_DETAILS:-- No failing test identifiers were recorded.}

<details><summary>Sanitized executor evidence</summary>

\`\`\`text
${targeted_evidence:-No executor output was captured.}
\`\`\`
</details>" || true
  POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
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
        curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
          -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || true
        return 0
      fi
    fi

    # Exactly one comment per loop iteration: a flake rerun folds its
    # (fixed-string, numbers-only) note into this same comment instead of
    # posting a second one — see the flake-classification block below, which
    # only sets is_flake_rerun and logs locally.
    local attempt_comment="prauto(${PRAUTO_WORKER_ID}): Heartbeat — integration test fix loop: attempt ${attempt}/${max_retries}"
    [[ "$is_flake_rerun" == true ]] && attempt_comment="${attempt_comment}, flake rerun ${flake_reruns}/${max_flake_reruns}"
    is_flake_rerun=false
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" --body "$attempt_comment" 2>/dev/null || true

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
      curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
        -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || true
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
          curl -s -X POST "${lock_url}/release" -H "Content-Type: application/json" \
            -d "{\"owner\": \"${lock_owner}\"}" >/dev/null 2>&1 || true
          return 0
        fi
      fi
      need_pre_probe=true
    else
      info "Max integration fix retries reached. Proceeding with current state."
    fi
    attempt=$((attempt + 1))
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
  if grep -Eqi 'failed to solve.*(Dockerfile|COPY|RUN)|Dockerfile.*(error|failed)|(^|[^[:alpha:]])(tsc|typescript|eslint|mypy|ruff)[^[:alpha:]].*(error|failed)|chart.*(schema|validation)|(schema|validation).*chart|values.*(invalid|required|must be)|template.*(executing|error).*\.yaml' <<< "$deploy_output"; then
    DEPLOY_FAILURE_KIND="branch"
  fi
}

deploy_branch_api() {
  local env_file="$1"
  DEPLOY_FAILURE_KIND="infrastructure"
  local install_script="${WORKTREE_DIR:-}/helm-charts/bin/install.sh"
  [[ -z "${WORKTREE_DIR:-}" ]] || [[ ! -f "$install_script" ]] && { DEPLOYED_API_SHA=""; DEPLOYED_FRONTEND_SHA=""; warn "Branch install.sh not found. Cannot deploy API."; return 1; }
  local tool
  for tool in kubectl helm docker; do
    command -v "$tool" >/dev/null 2>&1 || { DEPLOYED_API_SHA=""; DEPLOYED_FRONTEND_SHA=""; warn "${tool} not available. Cannot deploy API."; return 1; }
  done

  # Bind the artifact to the clean commit it is built from. An API upgrade
  # removes the cluster frontend, so the frontend binding is always cleared.
  local build_head
  build_head=$(executor_test_head)
  DEPLOYED_API_SHA=""; DEPLOYED_FRONTEND_SHA=""
  info "Building and deploying the branch API..."
  local deploy_output deploy_exit=0
  deploy_output=$(bash "$install_script" --profile dev --components api --env-file "$env_file" 2>&1) || deploy_exit=$?
  if [[ "$deploy_exit" -ne 0 ]]; then classify_deploy_failure "$deploy_output"; warn "API deploy failed (exit ${deploy_exit}, ${DEPLOY_FAILURE_KIND}):"; warn "$deploy_output"; return 1; fi
  DEPLOYED_API_SHA="$build_head"
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
  [[ -z "${WORKTREE_DIR:-}" ]] || [[ ! -f "$install_script" ]] && { DEPLOYED_FRONTEND_SHA=""; warn "Branch install.sh not found. Cannot deploy frontend."; return 1; }
  local tool
  for tool in kubectl helm docker; do
    command -v "$tool" >/dev/null 2>&1 || { DEPLOYED_FRONTEND_SHA=""; warn "${tool} not available. Cannot deploy frontend."; return 1; }
  done

  # Bind the artifact to the clean commit it is built from. The umbrella
  # upgrade also rolls the API pod, so a failed frontend deploy clears the API
  # binding as well; a successful one leaves the API image binding intact.
  local build_head
  build_head=$(executor_test_head)
  DEPLOYED_FRONTEND_SHA=""
  info "Building and deploying the branch frontend..."
  local deploy_output deploy_exit=0
  deploy_output=$(bash "$install_script" --profile dev --components frontend --env-file "$env_file" 2>&1) || deploy_exit=$?
  if [[ "$deploy_exit" -ne 0 ]]; then DEPLOYED_API_SHA=""; classify_deploy_failure "$deploy_output"; warn "Frontend deploy failed (exit ${deploy_exit}, ${DEPLOY_FAILURE_KIND}):"; warn "$deploy_output"; return 1; fi

  info "Forcing a frontend rollout restart..."
  kubectl rollout restart deployment/dataspoke-frontend -n "$ns" >/dev/null 2>&1 || { DEPLOYED_API_SHA=""; DEPLOY_FAILURE_KIND="infrastructure"; warn "Could not restart frontend in ${ns}."; return 1; }

  local deployment status_exit
  for deployment in dataspoke-frontend dataspoke-api; do
    info "Waiting for ${deployment} rollout..."
    status_exit=0
    kubectl rollout status "deployment/${deployment}" -n "$ns" --timeout=5m >/dev/null 2>&1 || status_exit=$?
    [[ "$status_exit" -ne 0 ]] && { DEPLOYED_API_SHA=""; DEPLOY_FAILURE_KIND="infrastructure"; warn "${deployment} did not become ready in ${ns}."; return 1; }
  done
  DEPLOYED_FRONTEND_SHA="$build_head"
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
  local max_retries="${PRAUTO_E2E_FIX_MAX_RETRIES:-3}"
  local max_flake_reruns="${PRAUTO_E2E_FLAKE_RERUNS:-2}"
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

  # attempt only advances when a real (non-flake) run happens; a flake-only
  # rerun (below) repeats the suite without spending a fix attempt, mirroring
  # run_integration_test_fix's flake_reruns/is_flake_rerun split — see that
  # function's comment for why a plain `for` loop cannot express this.
  local attempt=1 e2e_output e2e_exit=0 deployed=false quota_paused=false
  local flake_reruns=0 is_flake_rerun=false
  while [[ "$attempt" -le "$max_retries" ]]; do
    local attempt_comment="prauto(${PRAUTO_WORKER_ID}): Heartbeat — E2E stage: attempt ${attempt}/${max_retries}"
    [[ "$is_flake_rerun" == true ]] && attempt_comment="${attempt_comment}, flake rerun ${flake_reruns}/${max_flake_reruns}"
    is_flake_rerun=false
    info "E2E stage: attempt ${attempt}/${max_retries}"
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" --body "$attempt_comment" 2>/dev/null || true

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
