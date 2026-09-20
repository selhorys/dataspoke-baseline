# shellcheck shell=bash
# Include guard: this file defines top-level state, so re-sourcing it
# mid-run would reset that state to its startup values.
[[ -n "${PRAUTO_DEV_ENV_SH_LOADED:-}" ]] && return 0
PRAUTO_DEV_ENV_SH_LOADED=1
# Development-cluster lifecycle for prauto: provisioning, health, teardown and
# branch deploys.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, config loaded, REPO_DIR and PRAUTO_DIR set.
#
# Everything here talks to a real cluster and costs real money, so the invariants
# are about evidence rather than convenience: a cluster is only ever torn down
# when this worker recorded starting it, and a long-running command is started
# inside a verified, terminable process group before it is allowed to act.

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

# run_gated_group <label> <timeout_secs> <body_fn> [on_ready_fn]
# Run <body_fn> as a contained, terminable process group, and wait for it.
#
# The problem this solves: a long external command (install.sh, health-check.sh)
# spawns a tree of its own. If the heartbeat is signalled mid-run, it must be able
# to terminate that whole tree rather than orphan it — which means knowing the
# tree's process-group id BEFORE anything in it starts doing work.
#
# So the group is started inert. The wrapper blocks reading a FIFO and cannot
# reach <body_fn> until this parent has verified it owns a private process group
# and writes "start". If verification fails, the parent closes the FIFO instead:
# the wrapper reads EOF and exits having touched nothing. The FIFO is opened
# O_RDWR so the open does not block on a reader, and so heartbeat cleanup can
# close the only writer during the signal window and free a waiting wrapper.
#
# <on_ready_fn>, when given, runs after the group is verified and published but
# before the body is authorized — the one window where a caller can record
# durable evidence that the work is about to start. Returning non-zero from it
# aborts without running the body.
#
# Sets GATED_GROUP_EXIT to the body's exit status (124 if the backstop killed it).
# Returns: 0 ran to completion · 1 the gate could not be built · 2 the group could
# not be verified · 3 <on_ready_fn> declined. A caller maps those to its own
# contract; 1, 2 and 3 all mean the body never ran.
run_gated_group() {
  local label="$1" timeout_secs="$2" body_fn="$3" on_ready_fn="${4:-}"
  local gate_dir gate_fifo pid pgid rc=0
  GATED_GROUP_EXIT=0

  gate_dir=$(mktemp -d "${TMPDIR:-/tmp}/prauto-${label}-gate.XXXXXX") || return 1
  gate_fifo="${gate_dir}/start"
  if ! mkfifo "$gate_fifo"; then
    rmdir "$gate_dir" 2>/dev/null || true
    return 1
  fi

  exec 9<>"$gate_fifo"
  CONTAINMENT_GATE_FD_OPEN=true
  set -m
  ( exec 9>&-; IFS= read -r permit < "$gate_fifo"; [[ "$permit" == start ]] || exit 0; "$body_fn" ) &
  pid=$!
  CONTAINMENT_GATE_WRAPPER_PID="$pid"
  set +m

  # Abandon the inert wrapper: close the FIFO, let it read EOF, reap it.
  _abandon_gated_group() {
    exec 9>&-
    CONTAINMENT_GATE_FD_OPEN=false
    wait "$pid" 2>/dev/null || true
    CONTAINMENT_GATE_WRAPPER_PID=""
    rm -f "$gate_fifo"; rmdir "$gate_dir" 2>/dev/null || true
  }

  if ! pgid=$(private_process_group_for_leader "$pid"); then
    _abandon_gated_group
    return 2
  fi

  # Publish the verified group before authorizing the body, so a signal arriving
  # immediately after still group-terminates the work.
  PROVISION_PGID="$pgid"
  PROVISION_LEADER_PID="$pid"

  if [[ -n "$on_ready_fn" ]] && ! "$on_ready_fn"; then
    PROVISION_PGID=""
    PROVISION_LEADER_PID=""
    _abandon_gated_group
    return 3
  fi

  printf 'start\n' >&9
  exec 9>&-
  CONTAINMENT_GATE_FD_OPEN=false
  CONTAINMENT_GATE_WRAPPER_PID=""
  rm -f "$gate_fifo"; rmdir "$gate_dir" 2>/dev/null || true

  wait_with_group_backstop "$timeout_secs" "$pid" "$pgid" || rc=$?
  # Heartbeat cleanup owns the containment globals while this call is blocked;
  # only clear them once the wait has returned.
  PROVISION_PGID=""
  PROVISION_LEADER_PID=""
  GATED_GROUP_EXIT="$rc"
  return 0
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

  info "Provisioning the dev cluster (install.sh --profile dev)..."

  # Keep the transcript private and bounded while preserving both the opening
  # diagnostics and the final failure context. The byte-stream filter reads
  # fixed-size chunks, retains at most 12 KiB for short output and 6 KiB from
  # each end for larger output, and never buffers an entire line. pipefail is
  # required so the installer's exit status, rather than the filter's,
  # controls this job.
  _provision_body() {
    set -o pipefail
    LC_ALL=C bash "$install_script" --profile dev --env-file "$env_file" </dev/null 2>&1 | python3 -c '
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
' >"$prov_log" 2>&1
  }

  # Runs after the process group is verified and published, but before the
  # installer is authorized — the only window where durable evidence that a
  # cluster is about to exist can be recorded.
  #
  # The marker goes down BEFORE install.sh runs rather than after it succeeds: a
  # cluster install.sh has only partially built is exactly the case it exists for.
  # If this heartbeat is killed mid-install, both its own EXIT trap and a later
  # heartbeat's recover_orphaned_dev_env must still find evidence to tear it down.
  # provision_dev_env only runs when dev_env_healthy has already decided
  # provisioning is needed, so it never marks — and never tears down — a
  # pre-existing healthy cluster prauto did not start.
  #
  # It fails closed. Without a persisted marker, a crash that skips this wake's
  # EXIT trap leaves a cluster no later wake can discover, billing indefinitely.
  # Declining here leaves the wrapper inert and the cluster untouched.
  _provision_mark_started() {
    DEV_ENV_PROVISIONED=true
    DEV_ENV_PROVISIONED_ENV_FILE="$env_file"
    if write_dev_env_state_marker "$env_file"; then
      return 0
    fi
    DEV_ENV_PROVISIONED=false
    DEV_ENV_PROVISIONED_ENV_FILE=""
    return 1
  }

  run_gated_group provision "$timeout" _provision_body _provision_mark_started
  case $? in
    0) : ;;
    3) warn "Could not persist the dev-env provisioning marker. Provisioning was not started."
       return 1 ;;
    2) warn "Could not verify a private provisioning process group. Provisioning was not started."
       return 1 ;;
    *) warn "Could not create the provisioning containment gate. Cannot provision."
       return 1 ;;
  esac

  if [[ "$GATED_GROUP_EXIT" -eq 124 ]]; then
    warn "Cluster provisioning timed out after ${timeout}s. The private provisioning log was retained locally."
    return 1
  fi
  if [[ "$GATED_GROUP_EXIT" -ne 0 ]]; then
    warn "Cluster provisioning failed (exit ${GATED_GROUP_EXIT}). The private provisioning log was retained locally."
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
  local tmpdir out rc

  HEALTH_CHECK_OUTPUT=""
  tmpdir="$(mktemp -d)" || { HEALTH_CHECK_OUTPUT="Could not create a temp dir."; return 2; }
  out="${tmpdir}/output"

  # TMPDIR is redirected into this run's own directory so the health check's
  # scratch files cannot collide with another run's.
  _health_check_body() {
    exec env TMPDIR="$tmpdir" bash "$script" --env-file "$env_file" --keep-lock \
      </dev/null >"$out" 2>&1
  }

  run_gated_group health "$timeout" _health_check_body
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    HEALTH_CHECK_OUTPUT="Could not start a contained health check."
    rm -rf "$tmpdir"
    return 2
  fi

  HEALTH_CHECK_OUTPUT="$(cat "$out" 2>/dev/null || true)"
  if [[ "$GATED_GROUP_EXIT" -eq 124 ]]; then
    HEALTH_CHECK_OUTPUT="${HEALTH_CHECK_OUTPUT}
[health-check did not finish within ${timeout}s and was stopped]"
    rm -rf "$tmpdir"
    return 1
  fi
  rm -rf "$tmpdir"
  return "$GATED_GROUP_EXIT"
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
