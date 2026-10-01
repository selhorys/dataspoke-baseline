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

# DEV_ENV_TOUCHED_THIS_WAKE — set true by every code path that uses the dev
# cluster this wake (health check or probe, provisioning, a lock acquisition, a
# branch deploy). reap_ownerless_idle_dev_env reads it as "somebody — this worker —
# is demonstrably using the cluster right now, do not reap it out from under that".
# It only ever moves false -> true within a wake: there is deliberately no reset,
# because a cluster touched earlier in the wake and then abandoned (a failed
# stage, a skipped retry) is still not evidence of idleness. Declared here rather
# than left to first assignment so it is defined for the heartbeat's `set -u`
# EXIT trap even on a wake that never reached a cluster stage.
DEV_ENV_TOUCHED_THIS_WAKE=false
# Filled by dev_env_read_cluster_namespaces; shared between the teardown
# confirmation and the idle reaper so both read the env file's cluster and
# namespace keys by exactly one rule. Declared for the same `set -u` reason.
DEV_ENV_CLUSTER=""
DEV_ENV_DATASPOKE_NS=""
DEV_ENV_NAMESPACES=()
# The reaper's "newest Helm release update" in ISO-8601 UTC, for its one warn line.
REAP_NEWEST_ISO=""
# Why teardown_provisioned_dev_env is running — `provisioned` (this worker built the
# cluster) or `reap` (an ownerless idle cluster, or the retry of an interrupted
# one). It only selects the wording of the log lines; the deletion is identical.
DEV_ENV_TEARDOWN_REASON="provisioned"

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
# DEV_LOCK_URL, DEV_LOCK_HEALTH_URL. Returns 0 if the env file is present, 1 otherwise.
resolve_dev_env() {
  DEV_ENV_FILE=""; DEV_LOCK_URL=""; DEV_LOCK_HEALTH_URL=""
  [[ -z "${REPO_DIR:-}" ]] && return 1

  local configured="${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}" candidate
  if [[ "$configured" == /* ]]; then candidate="$configured"; else candidate="${REPO_DIR}/${configured}"; fi
  [[ ! -f "$candidate" ]] && return 1
  DEV_ENV_FILE="$candidate"

  local lock_base
  lock_base=$(env_file_value "$DEV_ENV_FILE" "DATASPOKE_DEV_LOCK_URL")
  local resolved_base="${lock_base:-http://localhost:9221}"
  DEV_LOCK_URL="${resolved_base}/lock"
  # The service's liveness route hangs off the BASE, not off /lock — a probe at
  # ${DEV_LOCK_URL}/health is a 404 the service answers happily.
  DEV_LOCK_HEALTH_URL="${resolved_base}/health"
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

# prune_old_provision_logs <log_dir> [prefix]
# <prefix> defaults to `provision`; teardown logs reuse the same retention with
# `teardown`.
# Provisioning logs are unscrubbed local transcripts of a real install.sh run
# (see spec/AI_PRAUTO.md §Provisioning) — never posted anywhere, but not kept
# forever either. Keeps the newest 4 so this run's own new file makes 5 total.
prune_old_provision_logs() {
  local log_dir="$1" prefix="${2:-provision}"
  [[ -d "$log_dir" ]] || return 0
  local -a old_logs=()
  while IFS= read -r f; do old_logs+=("$f"); done < <(ls -1t "${log_dir}"/"${prefix}"-* 2>/dev/null | tail -n +5)
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
  DEV_ENV_TOUCHED_THIS_WAKE=true
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

# write_dev_env_state_marker <env_file> [kind]
# <kind> records where the marker came from: `provision` (the default, and what a
# marker with no kind is read as) or `reap`. A teardown that was only ever
# justified by an idleness verdict must not become an unconditional delete when a
# later heartbeat retries it, so recover_orphaned_dev_env sends a `reap` marker
# back through the reap gates instead of straight to uninstall.sh.
# Persist proof of a provisioned-but-not-yet-torn-down cluster to disk, atomically
# (mktemp + chmod + mv, matching record_codex_native_session in state.sh). This is
# what survives a crash that skips the in-memory globals and the EXIT trap.
write_dev_env_state_marker() {
  local env_file="$1" kind="${2:-provision}" tmp_file
  mkdir -p "$(dirname "$DEV_ENV_STATE_FILE")" 2>/dev/null || true
  tmp_file=$(mktemp "${DEV_ENV_STATE_FILE}.tmp.XXXXXX") || return 1
  if ! jq -n --arg env_file "$env_file" --arg kind "$kind" --arg provisioned_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{env_file: $env_file, kind: $kind, provisioned_at: $provisioned_at}' > "$tmp_file"; then
    rm -f "$tmp_file"
    return 1
  fi
  chmod 600 "$tmp_file" 2>/dev/null || true
  mv -f "$tmp_file" "$DEV_ENV_STATE_FILE"
}

# dev_env_read_cluster_namespaces <env_file> [strict]
# With `strict`, all four namespace keys must be set (the reaper's rule: a dev
# profile file names every namespace, and a partial one is not one to delete by).
# Read the env file's kube context and the four dev-profile namespaces into
# DEV_ENV_CLUSTER / DEV_ENV_NAMESPACES (and the DataSpoke one alone into
# DEV_ENV_DATASPOKE_NS, empty when the key is unset). Runs in the CALLER's shell:
# the results are globals, so it must never be invoked in a command substitution.
# Fails CLOSED, with the two consumers needing exactly the same rule: no context,
# no namespace at all, or any value that is not a DNS-1123 label is "could not
# read the cluster identity", never "nothing to check".
#
# DNS-1123 matters because a malformed or partially-written env value could
# otherwise arrive as a kubectl OPTION. `--selector=...` as a namespace name
# returns exit 0 and no output — indistinguishable from "all four are gone",
# which is the failure the teardown confirmation exists to stop, and from "no
# releases" for the reaper.
dev_env_read_cluster_namespaces() {
  local env_file="$1" strict="${2:-}" key ns
  DEV_ENV_CLUSTER=""; DEV_ENV_DATASPOKE_NS=""; DEV_ENV_NAMESPACES=()

  DEV_ENV_CLUSTER=$(env_file_value "$env_file" "DATASPOKE_KUBE_CLUSTER")
  [[ -n "$DEV_ENV_CLUSTER" ]] || return 1

  for key in DATASPOKE_KUBE_DATASPOKE_NAMESPACE DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE \
             DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE; do
    ns=$(env_file_value "$env_file" "$key")
    if [[ -z "$ns" ]]; then
      [[ "$strict" == strict ]] && return 1
      continue
    fi
    [[ "$ns" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && "${#ns}" -le 63 ]] || return 1
    [[ "$key" == DATASPOKE_KUBE_DATASPOKE_NAMESPACE ]] && DEV_ENV_DATASPOKE_NS="$ns"
    DEV_ENV_NAMESPACES+=("$ns")
  done
  [[ "${#DEV_ENV_NAMESPACES[@]}" -gt 0 ]] || return 1
  return 0
}

# dev_env_namespaces_absent <env_file>
# Confirm the dev profile's namespaces are actually gone. Fails CLOSED: only a
# clean answer from the API server OF THE CLUSTER THIS ENV FILE NAMES counts as
# proof of deletion. An auth failure, a DNS outage, a missing kubectl, an
# unresolvable context, a malformed namespace name, or a timeout is "could not
# ask" — not evidence, and never to be read as success. That conflation is
# exactly what lets a cluster survive its own teardown.
#
# The --context pin is load-bearing, not hygiene. Ambient current-context is
# mutable global state (helpers.sh:110-158 pins a private kubeconfig copy for
# this reason), and uninstall.sh's own EXIT trap deletes that copy before we get
# here, so this call inherits nothing from the teardown it is checking. Against
# any other cluster the dev namespaces are trivially absent — which would delete
# the durable marker and reproduce the very failure this function exists to stop.
#
# --ignore-not-found reads absence from the API server's own answer (exit 0, no
# output) instead of matching its error prose, so this does not depend on how a
# given kubectl release words "not found".
#
# Hard-bounded by run_with_timeout because this runs from the EXIT trap and its
# expected bad case is an unreachable cluster. --request-timeout is a per-request
# bound, not a wall-clock one: the discovery calls underneath it are retried, and
# how long an unreachable endpoint takes to fail depends on whether the network
# refuses the connection or blackholes it. Only the outer bound is a guarantee.
dev_env_namespaces_absent() {
  local env_file="$1" out rc output
  local timeout_secs="${PRAUTO_DEV_ENV_NS_PROBE_TIMEOUT_SECS:-20}"
  [[ "$timeout_secs" =~ ^[1-9][0-9]*$ ]] || timeout_secs=20
  command -v kubectl >/dev/null 2>&1 || return 1
  declare -F run_with_timeout >/dev/null 2>&1 || return 1

  dev_env_read_cluster_namespaces "$env_file" || return 1

  out=$(mktemp) || return 1
  rc=0
  run_with_timeout "$timeout_secs" \
    kubectl --context "$DEV_ENV_CLUSTER" get namespace "${DEV_ENV_NAMESPACES[@]}" --ignore-not-found -o name \
    --request-timeout="${PRAUTO_KUBECTL_REQUEST_TIMEOUT:-3s}" >"$out" 2>&1 || rc=$?
  output=$(cat "$out" 2>/dev/null || true)
  rm -f "$out"
  [[ "$rc" -eq 0 ]] || return 1        # timed out, unreachable, unauthorized, or no such context
  [[ -z "$output" ]]                   # any name printed = still present
}

# teardown_provisioned_dev_env
# Remove a dev profile that this heartbeat (or a recovered earlier one, via
# recover_orphaned_dev_env) provisioned — or, with DEV_ENV_TEARDOWN_REASON=reap,
# one the idle reaper decided to delete. Full deletion includes PVCs and
# namespaces so a temporary test cluster does not keep incurring cost. This is
# intentionally best-effort because it runs from the EXIT trap — but it only
# clears the durable marker on an actual success, so a failure keeps the
# evidence around for the next heartbeat to retry instead of giving up forever.
#
# uninstall.sh's output goes to a private mode-600 log under the state dir, never
# to the scheduler log: it echoes cluster endpoints, namespaces and resource
# names, and only a fixed line with the exit code is safe to publish. (Same
# retention as the provisioning logs; if no private log can be made the output is
# discarded rather than shown — the teardown itself must still run.)
teardown_provisioned_dev_env() {
  [[ "${DEV_ENV_PROVISIONED:-false}" == true ]] || return 0
  [[ "${DEV_ENV_TEARDOWN_ATTEMPTED:-false}" == true ]] && return 0
  DEV_ENV_TEARDOWN_ATTEMPTED=true

  local env_file="${DEV_ENV_PROVISIONED_ENV_FILE:-}"
  local uninstall_script="${REPO_DIR:-}/helm-charts/bin/uninstall.sh"
  local start_msg="Tearing down the dev cluster provisioned by this heartbeat..."
  local done_msg="Provisioned dev cluster torn down."
  if [[ "${DEV_ENV_TEARDOWN_REASON:-provisioned}" == reap ]]; then
    start_msg="Tearing down ownerless idle dev cluster..."
    done_msg="Ownerless idle dev cluster torn down."
  fi
  if [[ -z "$env_file" || ! -f "$uninstall_script" ]]; then
    warn "Cannot tear down the provisioned dev cluster: uninstall.sh or its env file is missing."
    return 0
  fi

  info "$start_msg"
  local td_log="" log_dir="${STATE_DIR:-${PRAUTO_DIR:-.prauto}/state}" teardown_exit=0
  if mkdir -p "$log_dir" 2>/dev/null; then
    prune_old_provision_logs "$log_dir" teardown
    td_log=$(mktemp "${log_dir}/teardown-XXXXXX" 2>/dev/null) || td_log=""
    if [[ -n "$td_log" ]] && ! chmod 600 "$td_log" 2>/dev/null; then rm -f "$td_log"; td_log=""; fi
  fi
  [[ -n "$td_log" ]] || td_log=/dev/null
  bash "$uninstall_script" \
    --profile dev --env-file "$env_file" --no-question --delete-all >"$td_log" 2>&1 || teardown_exit=$?
  if [[ "$teardown_exit" -ne 0 ]]; then
    warn "Dev cluster teardown failed (exit ${teardown_exit}). The private teardown log was retained locally."
    warn "Leaving the durable marker in place for a later heartbeat to retry."
    return 0
  fi
  # An exit status is a claim, not evidence. uninstall.sh reports what it did;
  # only the API server reports what is gone. Clear the marker — the one thing
  # that lets a later heartbeat find this cluster at all — on confirmation only.
  if ! dev_env_namespaces_absent "$env_file"; then
    warn "uninstall.sh exited 0 but the dev namespaces are not confirmed gone."
    warn "Leaving the durable marker in place for a later heartbeat to retry."
    return 0
  fi
  rm -f "$DEV_ENV_STATE_FILE"
  info "$done_msg"
}

# dev_env_marker_path_is_sane <path>
# The marker's env_file must be exactly the dev env file this worker resolves for
# itself. Compared after realpath so a symlink or a `..` segment cannot point
# somewhere else that merely spells the same.
dev_env_marker_path_is_sane() {
  local candidate="$1" expected resolved_candidate resolved_expected
  [[ -f "$candidate" ]] || return 1
  resolve_dev_env || return 1
  expected="$DEV_ENV_FILE"
  resolved_candidate=$(cd "$(dirname "$candidate")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$candidate")") || return 1
  resolved_expected=$(cd "$(dirname "$expected")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$expected")") || return 1
  [[ "$resolved_candidate" == "$resolved_expected" ]]
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
  # This path is the target of a non-interactive `--profile dev --delete-all`
  # teardown, so it is checked before it is acted on rather than trusted because
  # this worker wrote it. It must be the dev env file this worker is configured
  # for, resolved under $REPO_DIR — never an arbitrary path, and never a prod
  # env file. The marker is now deliberately long-lived across failed teardowns,
  # which makes validating it matter more, not less.
  if ! dev_env_marker_path_is_sane "$env_file"; then
    warn "Dev-env state marker names an unexpected env file (${env_file}); removing it without a teardown attempt."
    rm -f "$DEV_ENV_STATE_FILE"
    return 0
  fi
  # The marker's origin decides what authorizes the retry. A `provision` marker
  # (or an old one with no kind) records a cluster THIS worker built, so its
  # teardown is retried as-is. A `reap` marker records only an idleness verdict
  # that was true when it was written; deleting on it later would destroy a cluster
  # someone has since reinstalled, pinned or started using. Any other kind value is
  # treated the strict way, as a reap marker.
  local kind marked_at
  kind=$(jq -r '.kind // "provision"' "$DEV_ENV_STATE_FILE" 2>/dev/null) || kind="reap"
  if [[ "$kind" != provision ]]; then
    marked_at=$(jq -r '.provisioned_at // empty' "$DEV_ENV_STATE_FILE" 2>/dev/null || true)
    warn "Found a dev-env reap marker from an earlier heartbeat (env_file=${env_file}). Re-checking it before any teardown."
    recover_reap_marker "$marked_at"
    return 0
  fi
  warn "Found a dev-env state marker from an earlier heartbeat (env_file=${env_file}). Retrying its teardown now."
  DEV_ENV_PROVISIONED=true
  DEV_ENV_PROVISIONED_ENV_FILE="$env_file"
  DEV_ENV_TEARDOWN_ATTEMPTED=false
  DEV_ENV_TEARDOWN_REASON="provisioned"
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
  # Every health probe — the pre-flight gate, the post-provision re-check and the
  # post-stage flake probe — funnels through here, so marking at entry covers all
  # of them, including a run the backstop later stops: it still reached for the
  # cluster, which is all the idle reaper needs to know.
  DEV_ENV_TOUCHED_THIS_WAKE=true
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
  # Marked here as well as in run_health_check: every cluster stage (including the
  # two inline lock acquires in phases.sh) goes through this gate before it takes
  # the lock, and the early "proceeding without the pre-flight gate" returns below
  # skip run_health_check yet still let the stage run against the cluster.
  DEV_ENV_TOUCHED_THIS_WAKE=true
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
  DEV_ENV_TOUCHED_THIS_WAKE=true
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

# DEV_LOCK_TOKEN_FILE — where this worker keeps the token the service minted for
# its current acquisition. The token, not the owner name, is what lets this
# worker reclaim a lock it left behind: the name is published on every GitHub
# comment and is identical between two workers sharing PRAUTO_WORKER_ID, so a
# name match cannot tell a leaked lock from a sibling's live one.
#
# Scope of the guarantee, stated precisely: the token stops an honest sibling
# from reclaiming a live lock by name. It is NOT an access control on the
# service, which is unauthenticated over plain HTTP and still offers both
# `DELETE /lock` and an owner-only release to any caller that can reach it.
# Mode 600 all the same — it is this worker's own claim, and cheap to protect.
DEV_LOCK_TOKEN_FILE="${DEV_LOCK_TOKEN_FILE:-${PRAUTO_DIR:-.prauto}/state/dev-lock-token.json}"
# The token for the acquisition currently held, and the PID of the process
# renewing its lease. Declared here rather than left to first assignment so both
# are defined for the heartbeat's `set -u` EXIT trap even when no lock was ever
# taken — the trap runs on every exit path, including ones that never acquired.
DEV_LOCK_TOKEN=""
DEV_LOCK_RENEWER_PID=""
DEV_LOCK_RENEWER_PGID=""

# dev_lock_json_body <key> <value> [key value ...]
# Build a request body with jq so a value carrying a quote, a backslash or a
# newline cannot reshape the JSON. Every field here is operator- or
# service-supplied rather than attacker-supplied today, but a hand-interpolated
# body is one config edit away from being a body-shaping primitive.
dev_lock_json_body() {
  local filter="{}" key
  local -a names=() values=()
  while [[ "$#" -gt 0 ]]; do
    key="$1"; shift
    # Values are inert (read from the environment below), but the KEY is
    # interpolated into the jq program, so it is constrained too.
    if [[ ! "$key" =~ ^[a-z][a-z0-9_]*$ ]]; then
      printf '{}'
      return 1
    fi
    names+=("$key"); values+=("${1:-}"); shift
    filter="${filter} | .${key} = env.DEV_LOCK_JQV_${key}"
  done
  # Values reach jq through its ENVIRONMENT, not its argv: a token passed as
  # `--arg` is visible to any local user in ps(1) and, on Linux, in the
  # world-readable /proc/<pid>/cmdline. /proc/<pid>/environ is owner-only.
  (
    local i
    for (( i = 0; i < ${#names[@]}; i++ )); do
      export "DEV_LOCK_JQV_${names[$i]}=${values[$i]}"
    done
    jq -nc "$filter" 2>/dev/null
  ) || { printf '{}'; return 1; }
}

# dev_lock_store_token <owner> <token>
# The owner is passed in, NOT read from REQUIRED_LOCK_OWNER: that global is set
# only after the acquire returns, so reading it here would file every token under
# an empty owner and dev_lock_load_token would never match one again — which
# silently disables the reclaim this token exists for.
dev_lock_store_token() {
  local owner="$1" token="$2" dir tmp
  DEV_LOCK_TOKEN="$token"
  [[ -n "$token" ]] || return 0
  dir=$(dirname "$DEV_LOCK_TOKEN_FILE")
  mkdir -p "$dir" 2>/dev/null || return 0
  tmp=$(mktemp "${DEV_LOCK_TOKEN_FILE}.tmp.XXXXXX") || return 0
  chmod 600 "$tmp" 2>/dev/null || true
  # Only install a body jq actually produced. The fallback used to be a literal
  # `{}`, which installs cleanly and then never matches on load — silently
  # disabling the very reclaim this file exists for.
  if dev_lock_json_body url "${DEV_LOCK_URL:-}" owner "$owner" token "$token" > "$tmp"; then
    mv -f "$tmp" "$DEV_LOCK_TOKEN_FILE" 2>/dev/null || rm -f "$tmp"
  else
    rm -f "$tmp"
    warn "Could not record the dev-env lock token; a leaked lock will not be reclaimable by this worker."
  fi
  return 0
}

# dev_lock_load_token
# Echo the stored token, but only when it was minted for THIS lock endpoint and
# owner — a token from another cluster's service must never authorize anything here.
dev_lock_load_token() {
  local owner="$1"
  [[ -f "$DEV_LOCK_TOKEN_FILE" ]] || return 0
  jq -r --arg url "${DEV_LOCK_URL:-}" --arg owner "$owner" '
    select(.url == $url and .owner == $owner) | .token // empty
  ' "$DEV_LOCK_TOKEN_FILE" 2>/dev/null || true
}

# dev_lock_clear_token
dev_lock_clear_token() {
  DEV_LOCK_TOKEN=""
  rm -f "$DEV_LOCK_TOKEN_FILE" 2>/dev/null || true
  return 0
}

# dev_lock_adopt_token <owner>
# Load the token dev_lock_acquire persisted into this shell. Required because
# that function necessarily runs in a subshell (see its docstring).
dev_lock_adopt_token() {
  DEV_LOCK_TOKEN=$(dev_lock_load_token "$1")
  [[ -n "$DEV_LOCK_TOKEN" ]]
}

# dev_lock_acquire <owner> <message>
# Echo the HTTP status of one acquire attempt and, on 200, persist the token the
# service minted to DEV_LOCK_TOKEN_FILE.
#
# Callers invoke this in a command substitution to read the status, which makes
# it a SUBSHELL: the DEV_LOCK_TOKEN it assigns cannot reach the caller. The file
# is the crossing point — the caller adopts the token with dev_lock_adopt_token
# once the acquire has succeeded. NEVER fails: a transport error echoes 000 and returns 0, so a
# bare `code=$(dev_lock_acquire ...)` assignment cannot abort a `set -e` caller
# before it has had the chance to report the block and release its lock.
dev_lock_acquire() {
  local owner="$1" message="$2" body resp code
  # `|| body='{}'` preserves this function's never-fails contract: a bare
  # assignment inheriting jq's non-zero status would abort a `set -e` caller
  # before it could report the block or release its lock. An empty body reaches
  # the service as a 400, which the caller already handles as a failed acquire.
  body=$(dev_lock_json_body owner "$owner" message "$message") || body='{}'
  resp=$(printf '%s' "$body" | curl -s -w $'\n%{http_code}' \
    --connect-timeout 5 --max-time 30 \
    -X POST "${DEV_LOCK_URL}/acquire" \
    -H "Content-Type: application/json" \
    --data-binary @- 2>/dev/null) || resp=$'\n000'
  code="${resp##*$'\n'}"
  if [[ "$code" == 200 ]]; then
    dev_lock_store_token "$owner" \
      "$(printf '%s' "${resp%$'\n'*}" | jq -r '.token // empty' 2>/dev/null || true)"
  fi
  printf '%s' "${code:-000}"
  return 0
}

# dev_lock_release_request <owner> <token>
# Echo the HTTP status of one release attempt. Never fails, same reasoning as above.
dev_lock_release_request() {
  local owner="$1" token="${2:-}" body code
  if [[ -n "$token" ]]; then
    body=$(dev_lock_json_body owner "$owner" token "$token") || body='{}'
  else
    body=$(dev_lock_json_body owner "$owner") || body='{}'
  fi
  # Body on stdin, not argv — it carries the acquisition token and argv is
  # world-readable via /proc on Linux.
  code=$(printf '%s' "$body" | curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 5 --max-time 30 \
    -X POST "${DEV_LOCK_URL}/release" \
    -H "Content-Type: application/json" \
    --data-binary @- 2>/dev/null) || code="000"
  printf '%s' "${code:-000}"
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
  # Surrender anything already held rather than just forgetting it: a bare reset
  # here, followed by a pre-flight failure below, would strand an acquisition
  # that release_required_dev_lock can no longer even see. Idempotent when
  # nothing is held.
  release_required_dev_lock
  REQUIRED_LOCK_OWNER=""
  if ! resolve_dev_env; then regression_blocked "$issue_number" "dev env file is unavailable" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  if ! dev_env_healthy "$DEV_ENV_FILE"; then regression_blocked "$issue_number" "dev cluster health/provisioning failed" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  # dev_lock_acquire below runs in a command-substitution subshell, so it cannot
  # carry this flag out to the caller; the acquire's caller is the place to mark it.
  DEV_ENV_TOUCHED_THIS_WAKE=true
  if ! dev_lock_endpoint_reachable; then
    regression_blocked "$issue_number" "dev-env lock endpoint is unreachable" "${CURRENT_REGRESSION_BRANCH:-}"; return 1
  fi
  lock_code=$(dev_lock_acquire "$owner" "prauto ${purpose} for issue #${issue_number}")
  # A 409 is reclaimable only on PROOF, never on a name. The holder's owner
  # string is published on every GitHub comment and is byte-identical between two
  # workers sharing PRAUTO_WORKER_ID, so matching it would let this worker
  # force-release a live sibling's lock on the shared dev cluster. The stored
  # token is the evidence: the service minted it for this worker's own earlier
  # acquisition, so presenting it releases exactly that acquisition — a sibling
  # that has since taken the lock answers 403 and the acquire stays blocked.
  if [[ "$lock_code" == 409 ]]; then
    local stale_token release_code
    stale_token=$(dev_lock_load_token "$owner")
    if [[ -n "$stale_token" ]]; then
      release_code=$(dev_lock_release_request "$owner" "$stale_token")
      if [[ "$release_code" == 200 ]]; then
        warn "Reclaimed a stale dev-env lock left by this worker (owner=${owner})."
        dev_lock_clear_token
        lock_code=$(dev_lock_acquire "$owner" "prauto ${purpose} for issue #${issue_number}")
      else
        # 403 = the token is not the current holder's. Someone else holds it.
        dev_lock_clear_token
      fi
    fi
  fi
  if [[ "$lock_code" != 200 ]]; then regression_blocked "$issue_number" "dev-env lock acquisition returned HTTP ${lock_code}" "${CURRENT_REGRESSION_BRANCH:-}"; return 1; fi
  REQUIRED_LOCK_OWNER="$owner"
  dev_lock_adopt_token "$owner" || true
  if ! dev_lock_start_renewer "$owner"; then
    release_required_dev_lock
    regression_blocked "$issue_number" "dev-env lock cannot be renewed (no acquisition token)" "${CURRENT_REGRESSION_BRANCH:-}"
    return 1
  fi
  return 0
}

# dev_lock_endpoint_reachable
# -f, so a route the service does not implement fails the probe. Without it a
# 404 exits 0 and every "live server, wrong path" fault survives to the acquire.
dev_lock_endpoint_reachable() {
  [[ -n "${DEV_LOCK_HEALTH_URL:-}" ]] || return 1
  curl -sf --connect-timeout 2 --max-time 10 "$DEV_LOCK_HEALTH_URL" >/dev/null 2>&1
}

# dev_lock_start_renewer <owner>
# The lease measures time since last contact, not total run length — that is what
# lets the TTL be short enough to free a lock a dead holder left behind. A live
# holder must therefore keep renewing.
#
# An UNBOUNDED renewer would defeat the very lease it supports: orphaned by a
# SIGKILL or an OOM kill (either of which skips every trap), it would keep
# extending the lease every interval forever, and the lock would never become
# reclaimable by anyone — the exact wedge the TTL exists to end, reintroduced by
# its own helper. So it is bounded three independent ways, any one of which ends
# it:
#   * the parent heartbeat's PID disappears (covers a trapless death),
#   * a hard wall-clock ceiling (covers PID reuse, where the check above can be
#     fooled by an unrelated new process),
#   * the service answers 403 or 409, both of which mean this acquisition no
#     longer exists — someone else holds the lock, or nothing does.
# dev_lock_stop_renewer remains the fast, ordinary path.
dev_lock_start_renewer() {
  local owner="$1" interval="${PRAUTO_DEV_LOCK_RENEW_SECS:-300}"
  local max_secs="${PRAUTO_DEV_LOCK_RENEW_MAX_SECS:-21600}"   # 6h ceiling
  [[ "$interval" =~ ^[1-9][0-9]*$ ]] || interval=300
  [[ "$max_secs" =~ ^[1-9][0-9]*$ ]] || max_secs=21600
  dev_lock_stop_renewer
  # No token means no renewal, and with a lease measured from last contact that
  # is a lock which expires mid-stage while this worker still believes it holds
  # the cluster. Report it; the caller treats it as a failed acquisition.
  if [[ -z "${DEV_LOCK_TOKEN:-}" ]]; then
    warn "Dev-env lock acquired but no renewal token was returned; the lease cannot be extended."
    return 1
  fi
  local url="$DEV_LOCK_URL" token="$DEV_LOCK_TOKEN" body parent_pid=$$
  # A renewer that cannot build its request is not a renewer; fail rather than
  # start a loop that will only ever post an empty body.
  if ! body=$(dev_lock_json_body owner "$owner" token "$token"); then
    warn "Could not build the dev-env lock renewal request; the lease will not be extended."
    return 1
  fi

  # A separate `bash -c` child with all three descriptors redirected, NOT a
  # backgrounded `( ... ) &` subshell. Measured: with the subshell form, any
  # caller that captures output around an acquire (`out=$(...)`) blocks until
  # the renewer exits — it waits for EOF on a pipe the renewer still holds, and
  # the renewer runs for the length of the regression. Redirecting the subshell
  # is not sufficient; giving the loop its own process with `</dev/null` is.
  #
  # setsid additionally makes it a process-group leader, which is what
  # dev_lock_stop_renewer's `kill -TERM -<pid>` needs to reach the in-flight
  # `sleep`. It is absent on stock macOS, where the plain-child form above is
  # used and the stop path falls through to killing the pid directly.
  local renew_loop='
    body="$DEV_LOCK_RENEW_BODY"
    url="$1"; interval="$2"; max_secs="$3"; parent_pid="$4"
    deadline=$(( $(date +%s) + max_secs ))
    while sleep "$interval"; do
      kill -0 "$parent_pid" 2>/dev/null || exit 0
      [ "$(date +%s)" -lt "$deadline" ] || exit 0
      code=$(printf "%s" "$body" | curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 5 --max-time 20 \
        -X POST "${url}/renew" -H "Content-Type: application/json" \
        --data-binary @- 2>/dev/null) || code="000"
      case "$code" in
        403|409) exit 0 ;;
      esac
    done'
  # The body carries the acquisition token, so it travels in the child's
  # ENVIRONMENT and never on its command line: this process lives for the whole
  # regression, and an argv word there is readable by any local user for that
  # entire span (ps(1); /proc/<pid>/cmdline is world-readable on Linux, whereas
  # /proc/<pid>/environ is owner-only). Only non-secret arguments go in argv.
  if command -v setsid >/dev/null 2>&1; then
    DEV_LOCK_RENEW_BODY="$body" setsid bash -c "$renew_loop" _ \
      "$url" "$interval" "$max_secs" "$parent_pid" \
      </dev/null >/dev/null 2>&1 &
    DEV_LOCK_RENEWER_PID=$!
    # Group-signalling is only safe against a PROVEN group leader, so the pgid is
    # recorded only when setsid actually made one.
    DEV_LOCK_RENEWER_PGID=$(private_process_group_for_leader "$DEV_LOCK_RENEWER_PID" 2>/dev/null || true)
  else
    DEV_LOCK_RENEW_BODY="$body" bash -c "$renew_loop" _ \
      "$url" "$interval" "$max_secs" "$parent_pid" \
      </dev/null >/dev/null 2>&1 &
    DEV_LOCK_RENEWER_PID=$!
    DEV_LOCK_RENEWER_PGID=""
  fi
  # Drop it from the job table: otherwise bash announces its termination
  # ("Terminated: 15" plus the whole subshell body) into the executor log every
  # time the renewer is stopped, which is on every release. wait_for_pid_bounded
  # reaps it by polling `kill -0`, not by `wait`, so disowning costs nothing.
  disown "$DEV_LOCK_RENEWER_PID" 2>/dev/null || true
  return 0
}

# dev_lock_stop_renewer
# dev_lock_stop_renewer
# Bounded, because this runs from the heartbeat EXIT trap — the one place in the
# harness that must not block. A plain `wait` here would block on an in-flight
# `sleep` of up to the renew interval.
#
# Group-signals ONLY a proven group leader. `kill -TERM -<pid>` against an
# unverified pgid is not a harmless no-op: if the renewer is not a leader (no
# setsid) and the kernel has recycled that number for an unrelated group, the
# signal lands on someone else's processes. This mirrors the verified-PGID rule
# private_process_group_for_leader exists to enforce elsewhere in this file.
dev_lock_stop_renewer() {
  [[ -n "${DEV_LOCK_RENEWER_PID:-}" ]] || return 0
  local pid="$DEV_LOCK_RENEWER_PID" pgid="${DEV_LOCK_RENEWER_PGID:-}"
  DEV_LOCK_RENEWER_PID=""
  DEV_LOCK_RENEWER_PGID=""
  if [[ -n "$pgid" ]] && pgid=$(private_process_group_for_leader "$pgid" 2>/dev/null); then
    kill -TERM -"$pgid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
  if declare -F wait_for_pid_bounded >/dev/null 2>&1; then
    wait_for_pid_bounded "$pid" "${PRAUTO_DEV_LOCK_RENEWER_STOP_SECS:-5}" || true
  fi
  # Only escalate against a process that is still alive: a pid that already
  # exited may since have been recycled by an unrelated process.
  if kill -0 "$pid" 2>/dev/null; then
    if [[ -n "$pgid" ]]; then
      kill -KILL -"$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  return 0
}

# release_required_dev_lock
# Clears the owner only once the SERVICE confirms the release — HTTP 200, not
# merely a completed exchange. curl exits 0 for a 403, a 404 and a 500 alike, so
# trusting its exit status would clear the owner while the lock is still held and
# recreate the structural no-op this function exists to remove.
#
# Bounded (attempts x --max-time) so the trap cannot hang on a dead endpoint.
#
# Always returns 0. Most call sites invoke this bare under `set -euo pipefail`
# and one is a function's final command, so a non-zero return would abort the
# heartbeat over a lock the retained owner already schedules for another try.
# A failure is reported through warn(), not through the exit status.
release_required_dev_lock() {
  [[ -n "${DEV_LOCK_URL:-}" && -n "${REQUIRED_LOCK_OWNER:-}" ]] || return 0
  local owner="$REQUIRED_LOCK_OWNER" attempts="${PRAUTO_DEV_LOCK_RELEASE_ATTEMPTS:-3}" attempt code
  local retry_secs="${PRAUTO_DEV_LOCK_RELEASE_RETRY_SECS:-5}"
  [[ "$attempts" =~ ^[1-9][0-9]*$ ]] || attempts=3
  [[ "$retry_secs" =~ ^[0-9]+$ ]] || retry_secs=5
  dev_lock_stop_renewer
  for (( attempt = 1; attempt <= attempts; attempt++ )); do
    code=$(dev_lock_release_request "$owner" "${DEV_LOCK_TOKEN:-}")
    case "$code" in
      200)
        REQUIRED_LOCK_OWNER=""; dev_lock_clear_token; return 0 ;;
      403)
        # Someone else holds it now; this worker's claim is void either way.
        warn "Dev-env lock is held by another owner; dropping this worker's claim (owner=${owner})."
        REQUIRED_LOCK_OWNER=""; dev_lock_clear_token; return 0 ;;
    esac
    if [[ "$attempt" -lt "$attempts" ]]; then sleep "$retry_secs"; fi
  done
  warn "Failed to release dev-env lock (owner=${owner}, last HTTP ${code}); keeping the owner set so a later release retries."
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
  DEV_ENV_TOUCHED_THIS_WAKE=true
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
  DEV_ENV_TOUCHED_THIS_WAKE=true
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


# ---------------------------------------------------------------------------
# Idle reaping (spec/AI_PRAUTO.md §Provisioning, "Ownerless idle clusters are
# reaped"). Everything below fails closed: reaping is cost hygiene, never worth
# destroying a cluster that is in use, or that is not provably a dev cluster.
# ---------------------------------------------------------------------------

# _reap_run <timeout_secs> <out_file> <command> [args...]
# One bounded probe for the reaper: stdout to <out_file>, stderr discarded.
# stderr is dropped, not captured, because the reaper only ever logs fixed
# strings — kubectl/helm error text can carry cluster endpoints and is not for
# the scheduler log. Returns the command's exit code, or 124 on timeout.
_reap_run() {
  local timeout_secs="$1" out_file="$2"; shift 2
  run_with_timeout "$timeout_secs" "$@" >"$out_file" 2>/dev/null
}

# _reap_iso_utc_to_epoch <YYYY-MM-DDTHH:MM:SSZ>
# Print the epoch for a strict ISO-8601 UTC timestamp, portably: BSD date (macOS)
# parses with -j -f, GNU date with -d, and each rejects the other's flags, so
# trying them in order needs no uname sniffing. The shape is checked first so
# neither parser ever sees free-form text (GNU `-d` would happily accept "next
# friday" or "@123"). Returns 1 on anything it cannot read — callers treat that
# as "unparseable", which for a keep pin means "pinned".
_reap_iso_utc_to_epoch() {
  local value="$1" epoch=""
  [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
  epoch=$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$value" +%s 2>/dev/null) \
    || epoch=$(date -u -d "$value" +%s 2>/dev/null) \
    || return 1
  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$epoch"
}

# _reap_newest_helm_update_program
# Print the python3 program that reads newline-separated Helm `updated` values
# from the environment variable REAP_UPDATED_LINES and prints
# "<epoch> <iso-utc>" for the newest. It is a program TEXT, not a function that
# runs it, because run_with_timeout launches its command through `setsid`, which
# can exec a binary but not a shell function. Helm renders
# them as `2026-10-01 21:57:09.194417 +0900 KST` — a local wall-clock time, a
# numeric offset and a zone NAME. Only the offset is trusted (zone abbreviations
# are ambiguous); the fraction and name are ignored. python3 does the arithmetic
# because BSD and GNU date disagree on every flag involved, and the data arrives
# through the environment, never interpolated into the program text. Exits
# non-zero if ANY line is unreadable or out of range, or if there is none: one
# value that cannot be understood must not let the rest vouch for idleness.
_reap_newest_helm_update_program() {
  cat <<'PY'
import calendar, datetime, os, re, sys

pat = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)? ([+-])(\d{2}):?(\d{2})(?:\s|$)"
)
best = None
for line in os.environ.get("REAP_UPDATED_LINES", "").splitlines():
    line = line.strip()
    if not line:
        continue
    m = pat.match(line)
    if not m:
        sys.exit(1)
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    sign = 1 if m.group(7) == "+" else -1
    oh, om = int(m.group(8)), int(m.group(9))
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h <= 23 and mi <= 59 and s <= 60 and oh <= 14 and om <= 59):
        sys.exit(1)
    epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - sign * (oh * 3600 + om * 60)
    if best is None or epoch > best:
        best = epoch
if best is None:
    sys.exit(1)
iso = datetime.datetime.fromtimestamp(best, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
print(best, iso)
PY
}

# _reap_env_profile <env_file>
# `dev`, `prod`, `ambiguous` or `none`, by the same rule as helm-charts' own
# seed_profile (helm-charts/bin/lib/helpers.sh): from the file's own non-comment
# assignments, a DATASPOKE_PROD_* name means prod and a DATASPOKE_DEV_* name means
# dev. Duplicated rather than sourced because that library is a deployment-script
# toolbox this harness must not load; keep the two in step. `.env.prod.example`
# carries no DATASPOKE_DEV_* line by design, which is what makes the verdict
# reliable here.
_reap_env_profile() {
  awk '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      sub(/^export[[:space:]]+/, "", line)
      if (line !~ /^[A-Za-z_][A-Za-z0-9_]*=/) next
      name = substr(line, 1, index(line, "=") - 1)
      if (name ~ /^DATASPOKE_PROD_/) prod = 1
      else if (name ~ /^DATASPOKE_DEV_/) dev = 1
    }
    END {
      if (prod && dev) print "ambiguous"
      else if (prod) print "prod"
      else if (dev) print "dev"
      else print "none"
    }
  ' "$1" 2>/dev/null || printf 'none'
}

# dev_env_reap_gate_env
# Is the resolved env file (DEV_ENV_FILE) one the reaper may ever delete by? The
# reaper runs a non-interactive `--delete-all` against whatever cluster the file
# names, so the file must be provably a dev-profile file, and the identity this
# harness reads from it must be exactly what uninstall.sh will act on. Returns 0
# only when ALL hold; every refusal logs one fixed line and returns 1. Purely
# local (no network): a refusal is a property of the file, never of cluster state,
# which is what lets marker recovery treat it as final. Fills DEV_ENV_CLUSTER,
# DEV_ENV_NAMESPACES and DEV_ENV_DATASPOKE_NS.
#   * the basename is not a prod env file;
#   * its own assignments name the dev profile (DATASPOKE_DEV_* present, no
#     DATASPOKE_PROD_*);
#   * DATASPOKE_DEV_LOCK_URL is set EXPLICITLY — resolve_dev_env's localhost
#     default would point the lock at an unrelated service on this host;
#   * the cluster context and all four namespaces are set and DNS-1123 valid;
#   * sourcing the file the way uninstall.sh does (clean environment) yields those
#     same values, so a computed or shadowed assignment this harness's line-grep
#     misread cannot make it delete somewhere else than it checked.
dev_env_reap_gate_env() {
  local file="${DEV_ENV_FILE:-}" base lock_url expected actual out rc=0
  if [[ -z "$file" || ! -f "$file" ]]; then
    info "Reap refused: the dev env file is unavailable."; return 1
  fi
  base="${file##*/}"
  case "$base" in
    *env.prod*) info "Reap refused: the env file is a prod env file."; return 1 ;;
  esac
  if [[ "$(_reap_env_profile "$file")" != dev ]]; then
    info "Reap refused: the env file does not identify as a dev-profile file."; return 1
  fi
  lock_url=$(env_file_value "$file" "DATASPOKE_DEV_LOCK_URL")
  if [[ ! "$lock_url" =~ ^https?://[^[:space:]]+$ ]]; then
    info "Reap refused: the env file does not set DATASPOKE_DEV_LOCK_URL explicitly."; return 1
  fi
  if ! dev_env_read_cluster_namespaces "$file" strict; then
    info "Reap refused: the env file does not name a cluster and all four dev namespaces."; return 1
  fi
  if ! declare -F run_with_timeout >/dev/null 2>&1; then
    info "Reap refused: no bounded runner is available."; return 1
  fi
  out=$(mktemp) || { info "Reap refused: could not create a scratch file."; return 1; }
  _reap_run 10 "$out" env -i PATH="$PATH" bash -c '
    set -a
    source "$1" >/dev/null 2>&1
    printf "%s\n" "${DATASPOKE_KUBE_CLUSTER-}" "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE-}" \
      "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE-}" "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE-}" \
      "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE-}" "${DATASPOKE_DEV_LOCK_URL-}"
  ' _ "$file" || rc=$?
  actual=$(cat "$out" 2>/dev/null || true)
  rm -f "$out"
  expected=$(printf '%s\n' "$DEV_ENV_CLUSTER" "${DEV_ENV_NAMESPACES[@]}" "$lock_url")
  if [[ "$rc" -ne 0 || "$actual" != "$expected" ]]; then
    info "Reap refused: the env file's sourced values do not match what was read from it."; return 1
  fi
  return 0
}

# _reap_lock_bound_to_cluster <cluster> <dataspoke_ns> <owner> <timeout_secs> <scratch_file>
# After a 200 acquire: is the lock we now hold THE TARGET CLUSTER'S lock? The lock
# URL is plain HTTP from an env file; nothing in it ties it to the cluster the
# uninstall will delete, and "I hold a lock" on some other service says nothing
# about this cluster's users. So prove it on the target cluster itself: present the
# token the acquire minted to that cluster's own dev-lock service (installed in the
# DataSpoke namespace as service dev-lock:8080 — helm-charts/bin/dev-peripherals/
# dev-lock.sh), through the API server's service proxy, as a lease renewal.
# /lock/renew answers 200 only when the token AND owner match the current holder
# (403 token mismatch, 409 not locked; lock-service.yaml), so a 200 naming us is
# proof that the holder of THIS cluster's lock is the acquisition we just made.
# Proof by owner NAME alone — a plain GET of /lock — is not enough: two workers can
# share a worker id, and the name is printed on every GitHub comment.
#
# `kubectl create --raw` prints the response body on success and exits non-zero on
# any HTTP error status, so exit 0 plus a body naming us is the only pass; no
# answer, a non-2xx, another owner or an unreadable reply is a mismatch. The renew
# also extends the lease, which is harmless: we hold the lock.
#
# The body carries the token, so it is built with dev_lock_json_body (token via the
# environment, never argv) and handed to kubectl as a mode-600 file deleted at
# once, not on its command line: run_with_timeout backgrounds its command, which
# gives it a /dev/null stdin, so `-f -` cannot be bounded and fed from a pipe.
_reap_lock_bound_to_cluster() {
  local cluster="$1" dsns="$2" owner="$3" timeout_secs="$4" out="$5" rc=0 body bodyfile
  [[ -n "${DEV_LOCK_TOKEN:-}" ]] || return 1
  body=$(dev_lock_json_body owner "$owner" token "$DEV_LOCK_TOKEN") || return 1
  bodyfile=$(mktemp) || return 1
  chmod 600 "$bodyfile" 2>/dev/null || { rm -f "$bodyfile"; return 1; }
  printf '%s' "$body" >"$bodyfile"
  _reap_run "$timeout_secs" "$out" \
    kubectl --context "$cluster" create --raw "/api/v1/namespaces/${dsns}/services/dev-lock:8080/proxy/lock/renew" \
    -f "$bodyfile" --request-timeout="${PRAUTO_KUBECTL_REQUEST_TIMEOUT:-3s}" || rc=$?
  rm -f "$bodyfile"
  [[ "$rc" -eq 0 ]] || return 1
  jq -e --arg owner "$owner" '(.locked == true) and (.owner == $owner)' <"$out" >/dev/null 2>&1
}

# _reap_namespaces_not_recreated <cluster> <marker_epoch> <probe_timeout_secs> <scratch_file>
# Lockless recovery only. The Helm `since` check cannot see two kinds of human
# reinstall: install.sh creates the dev namespaces FIRST (a wake landing before any
# release exists finds no newer Helm update), and dummy-data has no Helm release at
# all (re-applying it leaves no Helm trace). A namespace is the earlier and more
# durable trace, so compare each still-present dev namespace's creationTimestamp
# with the marker time. One context-pinned, bounded call for all four names;
# --ignore-not-found makes the absent ones simply not appear, and kubectl answers
# a single hit as an object and several as a List, which the jq filter flattens.
# Returns 0 = every present namespace predates the marker (none present passes);
# 1 = one was created after it (conclusive: the cluster is being rebuilt);
# 2 = could not tell (probe error or timeout, unreadable or missing timestamp).
_reap_namespaces_not_recreated() {
  local cluster="$1" marker_epoch="$2" timeout_secs="$3" out="$4" rc=0 stamps stamp epoch
  _reap_run "$timeout_secs" "$out" \
    kubectl --context "$cluster" get namespace "${DEV_ENV_NAMESPACES[@]}" --ignore-not-found -o json \
    --request-timeout="${PRAUTO_KUBECTL_REQUEST_TIMEOUT:-3s}" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    info "Skipping idle reap: could not read the dev namespaces' creation times."; return 2
  fi
  [[ -s "$out" ]] || return 0
  if ! stamps=$(jq -r 'if .kind == "List" then .items[] else . end | (.metadata.creationTimestamp // "missing")' <"$out" 2>/dev/null) \
      || [[ -z "$stamps" ]]; then
    info "Skipping idle reap: the dev namespaces' creation times were unreadable."; return 2
  fi
  while IFS= read -r stamp; do
    if ! epoch=$(_reap_iso_utc_to_epoch "$stamp"); then
      info "Skipping idle reap: a dev namespace creation time could not be parsed."; return 2
    fi
    if (( epoch > marker_epoch )); then
      info "Skipping idle reap: a dev namespace was created after the reap began."; return 1
    fi
  done <<<"$stamps"
  return 0
}

# _reap_final_checks_pass <cluster> <dataspoke_ns> <idle|since> <arg> <probe_timeout_secs> <scratch_file> [ds_absent_ok]
# With `ds_absent_ok` (recover mode, after a partly-run uninstall) a DataSpoke
# namespace that is cleanly gone is not "could not tell": it can carry no keep pin,
# so only the Helm check runs.
# The keep-pin and idleness verdict. Run twice per reap: once lock-free as an
# advisory gate (so an ordinary busy cluster never makes this worker contact its
# lock service at all), then again with the lock held as the authoritative check,
# so nothing can change between this answer and the uninstall.
#   idle  <threshold_secs>: pass only if the newest Helm release update is strictly
#         older than the threshold. No releases at all is "idle unknown" (a fresh
#         or half-installed cluster looks exactly like this) — not a pass.
#   since <marker_epoch>: pass only if nothing was updated after the reap began
#         (newest update <= marker time). Used when a reap marker is retried; there,
#         no releases at all is expected (a partly-run uninstall removes them first)
#         and passes.
# Returns 0 = proven idle and unpinned; 1 = conclusively NOT reapable (a keep pin,
# or activity inside the window); 2 = could not tell (failed/timed-out/unparseable
# probe). Reap treats both non-zero results as "skip"; marker recovery drops its
# marker on 1 and keeps it on 2. Logs one fixed line either way; sets
# REAP_NEWEST_ISO on pass.
_reap_final_checks_pass() {
  local cluster="$1" dsns="$2" mode="$3" arg="$4" timeout_secs="$5" out="$6" ds_absent_ok="${7:-}"
  local request_timeout="${PRAUTO_KUBECTL_REQUEST_TIMEOUT:-3s}"
  local rc=0 ns_json pin pin_epoch now ns updated lines="" newest newest_epoch
  REAP_NEWEST_ISO=""

  # --ignore-not-found turns "namespace gone" into exit 0 with no output, which
  # is read below as its own (non-error) answer rather than as a failure.
  _reap_run "$timeout_secs" "$out" \
    kubectl --context "$cluster" get namespace "$dsns" --ignore-not-found -o json \
    --request-timeout="$request_timeout" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    info "Skipping idle reap: could not read the DataSpoke namespace."; return 2
  fi
  ns_json=$(cat "$out" 2>/dev/null || true)
  now=$(date +%s)
  if [[ -z "$ns_json" && "$ds_absent_ok" == ds_absent_ok ]]; then
    pin="none"   # no namespace, no annotation: nothing to pin
  elif [[ -z "$ns_json" ]]; then
    info "Skipping idle reap: the DataSpoke namespace is no longer present."; return 2
  else
    # Keep pin. `has()` rather than `// empty`: an annotation set to the empty
    # string is a pin nobody can parse, which must block, not read as "no pin".
    pin=$(printf '%s' "$ns_json" | jq -r '
      (.metadata.annotations // {})
      | if has("dataspoke.io/keep-until") then "pinned:" + (.["dataspoke.io/keep-until"] | tostring) else "none" end
    ' 2>/dev/null) || pin=""
  fi
  case "$pin" in
    none) : ;;
    pinned:*)
      if ! pin_epoch=$(_reap_iso_utc_to_epoch "${pin#pinned:}"); then
        info "Skipping idle reap: the dataspoke.io/keep-until pin is unparseable."; return 1
      fi
      if (( pin_epoch > now )); then
        info "Skipping idle reap: a dataspoke.io/keep-until pin is still in effect."; return 1
      fi ;;
    *)
      info "Skipping idle reap: could not read the DataSpoke namespace annotations."; return 2 ;;
  esac

  # Idleness: newest Helm release update across the dev namespaces. `--all` so a
  # pending or failed release counts — an install in flight is the clearest
  # activity there is, and the default (deployed-only) listing would hide it.
  # helm lists a namespace that does not exist as empty, so absent namespaces
  # simply contribute nothing.
  for ns in "${DEV_ENV_NAMESPACES[@]}"; do
    rc=0
    _reap_run "$timeout_secs" "$out" \
      helm --kube-context "$cluster" list --all -n "$ns" -o json || rc=$?
    if [[ "$rc" -ne 0 ]]; then
      info "Skipping idle reap: a Helm release listing failed."; return 2
    fi
    if ! updated=$(jq -r '.[] | (.updated // "missing")' "$out" 2>/dev/null); then
      info "Skipping idle reap: a Helm release listing was unreadable."; return 2
    fi
    [[ -z "$updated" ]] || lines="${lines}${updated}"$'\n'
  done
  if [[ -z "$lines" ]]; then
    if [[ "$mode" == since ]]; then return 0; fi
    info "Skipping idle reap: no Helm releases to measure idleness from."; return 2
  fi
  if ! newest=$(REAP_UPDATED_LINES="$lines" run_with_timeout 10 python3 -c "$(_reap_newest_helm_update_program)" 2>/dev/null); then
    info "Skipping idle reap: Helm release timestamps could not be parsed."; return 2
  fi
  newest_epoch="${newest%% *}"
  if [[ ! "$newest_epoch" =~ ^[0-9]+$ ]]; then
    info "Skipping idle reap: Helm release timestamps could not be parsed."; return 2
  fi
  if [[ "$mode" == since ]]; then
    if (( newest_epoch > arg )); then
      info "Skipping idle reap: the dev cluster was updated after the reap began."; return 1
    fi
  # Strictly older than the threshold. A timestamp in the future (clock skew)
  # yields a negative age, which is never idle.
  elif (( now - newest_epoch <= arg )); then
    info "Skipping idle reap: the dev cluster was updated within the idle threshold."; return 1
  fi
  REAP_NEWEST_ISO="${newest#* }"
  return 0
}

# _reap_engine <reap|recover> <threshold_secs | marker_epoch>
# The shared body of a fresh reap and of a reap-marker retry, run once the env
# file has passed dev_env_reap_gate_env. Order matters: cheap lock-free checks
# first, so an ordinary busy wake never contacts the lock service; then the lock;
# then the same checks again, authoritatively, because the spec's guarantee is
# that the lock is held "from the final checks through the uninstall".
#
# Returns 0 = a teardown was attempted (it handled the marker and the lock);
# 10 = skipped, nothing deleted, any marker untouched; 11 = recover mode only: the
# cluster is conclusively not reapable (pinned, or updated after the reap began),
# so the caller drops the marker; 12 = no dev namespaces exist at all.
#
# The DataSpoke namespace anchors reapability of a FRESH reap: it carries the keep
# pin and hosts the dev-lock service. If it is gone while other dev namespaces
# remain, neither the pin nor the lock can be consulted, so that is a partial
# cluster to remove by hand, not one to delete on a guess.
#
# RECOVER mode has one more case, because the uninstall this retries is the thing
# that made the cluster partial: uninstall.sh deletes the dev-lock service early
# and the DataSpoke namespace late, so an interrupted run can leave a cluster whose
# lock can never be taken again. There the lock is "unholdable" — proven by a clean,
# context-pinned API answer (`--ignore-not-found`, exit 0, empty output: an error
# or timeout is "could not tell", never absence) that the dev-lock Service is absent
# from the DataSpoke namespace, or that the namespace itself is. The retry then
# runs WITHOUT the lock, on the lock-free `since` check alone: no Helm release
# updated after the marker (none at all passes), no still-present dev namespace
# created after the marker (install.sh creates namespaces before releases, and
# dummy-data has no release — see _reap_namespaces_not_recreated), and no keep pin
# on a still-present DataSpoke namespace. A human reinstall recreates dev-lock and refreshes Helm, and
# either signal aborts this path (drop on a conclusive answer, keep on "could not
# tell"). While the dev-lock Service exists the token-bound lock is still required.
_reap_engine() {
  local mode="$1" arg="$2" fmode="idle" tool owner lock_code out dsabsent=""
  local rc=0 verdict=0 lockless=false
  local timeout_secs="${PRAUTO_DEV_ENV_NS_PROBE_TIMEOUT_SECS:-20}"
  [[ "$timeout_secs" =~ ^[1-9][0-9]*$ ]] || timeout_secs=20
  [[ "$mode" == recover ]] && fmode="since"

  for tool in kubectl helm jq python3 curl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      info "Skipping idle reap: ${tool} is not available."; return 10
    fi
  done
  declare -F run_with_timeout >/dev/null 2>&1 || { info "Skipping idle reap: no bounded runner is available."; return 10; }
  out=$(mktemp) || { info "Skipping idle reap: could not create a scratch file."; return 10; }

  # Which dev namespaces exist? (One call; a missing one is simply not listed.)
  _reap_run "$timeout_secs" "$out" \
    kubectl --context "$DEV_ENV_CLUSTER" get namespace "${DEV_ENV_NAMESPACES[@]}" --ignore-not-found -o name \
    --request-timeout="${PRAUTO_KUBECTL_REQUEST_TIMEOUT:-3s}" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    rm -f "$out"; info "Skipping idle reap: the dev cluster did not answer."; return 10
  fi
  if [[ -z "$(cat "$out" 2>/dev/null || true)" ]]; then
    rm -f "$out"; return 12
  fi
  if ! grep -Fxq "namespace/${DEV_ENV_DATASPOKE_NS}" "$out"; then
    if [[ "$mode" != recover ]]; then
      rm -f "$out"
      warn "Found a partial dev cluster without its DataSpoke namespace; not reaping — remove manually."
      return 10
    fi
    dsabsent="ds_absent_ok"
  fi

  # Advisory pass, lock-free.
  _reap_final_checks_pass "$DEV_ENV_CLUSTER" "$DEV_ENV_DATASPOKE_NS" "$fmode" "$arg" "$timeout_secs" "$out" "$dsabsent" || verdict=$?
  if [[ "$verdict" -ne 0 ]]; then
    rm -f "$out"
    [[ "$verdict" -eq 1 && "$mode" == recover ]] && return 11
    return 10
  fi

  # Recover mode: is the lock holdable at all? Asked AFTER the Helm check, so a
  # reinstall in between shows up as a live dev-lock Service (token-bound path
  # below, whose under-lock re-check then sees the newer update).
  if [[ "$mode" == recover ]]; then
    if [[ -n "$dsabsent" ]]; then
      lockless=true
    else
      rc=0
      _reap_run "$timeout_secs" "$out" \
        kubectl --context "$DEV_ENV_CLUSTER" get service dev-lock -n "$DEV_ENV_DATASPOKE_NS" --ignore-not-found -o name \
        --request-timeout="${PRAUTO_KUBECTL_REQUEST_TIMEOUT:-3s}" || rc=$?
      if [[ "$rc" -ne 0 ]]; then
        rm -f "$out"; info "Skipping idle reap: could not tell whether the dev-lock service exists."; return 10
      fi
      [[ -z "$(cat "$out" 2>/dev/null || true)" ]] && lockless=true
    fi
  fi

  if [[ "$lockless" == true ]]; then
    # No lock exists to take, so the guard closest to the uninstall is these checks
    # once more, immediately before it. The namespace-creation check runs on both
    # sides of the Helm re-check: once on entering the lockless branch, and again as
    # the very last probe before the uninstall.
    verdict=0
    _reap_namespaces_not_recreated "$DEV_ENV_CLUSTER" "$arg" "$timeout_secs" "$out" || verdict=$?
    if [[ "$verdict" -eq 0 ]]; then
      _reap_final_checks_pass "$DEV_ENV_CLUSTER" "$DEV_ENV_DATASPOKE_NS" "$fmode" "$arg" "$timeout_secs" "$out" "$dsabsent" || verdict=$?
    fi
    if [[ "$verdict" -eq 0 ]]; then
      _reap_namespaces_not_recreated "$DEV_ENV_CLUSTER" "$arg" "$timeout_secs" "$out" || verdict=$?
    fi
    rm -f "$out"
    if [[ "$verdict" -ne 0 ]]; then
      [[ "$verdict" -eq 1 ]] && return 11
      return 10
    fi
    warn "Retrying an interrupted idle reap without the lock (its dev-lock service is already gone)..."
  else
    # The lock, held from here through the uninstall. An unreachable lock service
    # is not evidence that nobody holds it; and no stale-token reclaim here — a 409
    # means someone is using the cluster.
    if ! dev_lock_endpoint_reachable; then
      rm -f "$out"; info "Skipping idle reap: the dev-env lock service is unreachable."; return 10
    fi
    owner="prauto-${PRAUTO_WORKER_ID}"
    lock_code=$(dev_lock_acquire "$owner" "prauto idle reap")
    if [[ "$lock_code" != 200 ]]; then
      rm -f "$out"; info "Skipping idle reap: the dev-env lock was not acquired (HTTP ${lock_code})."; return 10
    fi
    # Register the lock before anything that can fail, so release_required_dev_lock
    # can surrender it on every skip path below. dev_lock_acquire ran in a
    # subshell; its token reaches this shell only through the token file.
    REQUIRED_LOCK_OWNER="$owner"
    dev_lock_adopt_token "$owner" || true
    # Same fail-closed rule as the stage acquires: an uninstall that outlives an
    # unrenewable lease would run without the lock it is relying on.
    if ! dev_lock_start_renewer "$owner"; then
      release_required_dev_lock
      rm -f "$out"; info "Skipping idle reap: the dev-env lock cannot be renewed."; return 10
    fi
    if ! _reap_lock_bound_to_cluster "$DEV_ENV_CLUSTER" "$DEV_ENV_DATASPOKE_NS" "$owner" "$timeout_secs" "$out"; then
      release_required_dev_lock
      rm -f "$out"; info "Skipping idle reap: the lock held is not confirmed to be the target cluster's lock."; return 10
    fi

    # Authoritative checks, under the lock.
    verdict=0
    _reap_final_checks_pass "$DEV_ENV_CLUSTER" "$DEV_ENV_DATASPOKE_NS" "$fmode" "$arg" "$timeout_secs" "$out" || verdict=$?
    rm -f "$out"
    if [[ "$verdict" -ne 0 ]]; then
      release_required_dev_lock
      [[ "$verdict" -eq 1 && "$mode" == recover ]] && return 11
      return 10
    fi
    [[ "$mode" == recover ]] && warn "Retrying an interrupted idle reap (nothing changed since it began)..."
  fi

  if [[ "$mode" == reap ]]; then
    # Reap. The marker goes down BEFORE the uninstall — the same evidence
    # provisioning records, tagged `reap` — so a heartbeat killed mid-uninstall is
    # finished by the next one's marker recovery (which re-checks, never deletes
    # blindly). If it cannot be persisted, do nothing: a half-deleted cluster with
    # no marker is the one outcome nothing recovers.
    warn "Reaping ownerless idle dev cluster (newest Helm update ${REAP_NEWEST_ISO}, threshold ${arg}s)..."
    if ! write_dev_env_state_marker "$DEV_ENV_FILE" reap; then
      release_required_dev_lock
      info "Skipping idle reap: could not persist the dev-env marker."; return 10
    fi
  fi
  DEV_ENV_PROVISIONED=true
  DEV_ENV_PROVISIONED_ENV_FILE="$DEV_ENV_FILE"
  DEV_ENV_TEARDOWN_ATTEMPTED=false
  DEV_ENV_TEARDOWN_REASON="reap"
  teardown_provisioned_dev_env || true

  # A lockless retry holds nothing to settle.
  if [[ "$lockless" == true ]]; then
    return 0
  fi
  if [[ ! -e "$DEV_ENV_STATE_FILE" ]]; then
    # Deletion confirmed. The lock service lived in the deleted cluster, so a
    # release call would only talk to nothing: stop the renewer and discard the
    # local claim instead.
    dev_lock_stop_renewer
    REQUIRED_LOCK_OWNER=""
    dev_lock_clear_token
  else
    # Not confirmed gone: the marker stays for a later heartbeat's recovery, and
    # the service may still exist, so give the lock back (best effort).
    release_required_dev_lock
  fi
  return 0
}

# recover_reap_marker <marked_at_iso>
# Finish — or deliberately abandon — a reap that an earlier heartbeat began and did
# not complete. Called by recover_orphaned_dev_env for a `reap` marker, with
# DEV_ENV_FILE already resolved and sanity-checked. The marker proves only that an
# idleness verdict was once true; the cluster may since have been reinstalled,
# pinned or started by someone else, so every reap gate runs again before the
# uninstall is retried:
#   * file-level refusal (not provably a dev file): drop the marker, never delete;
#   * namespaces already gone: confirm, then clear;
#   * keep pin now in effect, or any Helm release updated after the reap began:
#     drop the marker without deleting;
#   * dev-lock service (or the whole DataSpoke namespace) cleanly absent — the state
#     a partly-run uninstall.sh leaves — and nothing updated since the reap began:
#     retry the uninstall WITHOUT the lock (see _reap_engine);
#   * anything undetermined — a lock conflict or token-proof failure, an unreachable
#     service or cluster, a probe that errors: KEEP the marker and retry on a later
#     heartbeat.
# Dropping loses a retry but destroys nothing; the asymmetry is the point.
recover_reap_marker() {
  local marked_at="${1:-}" marked_epoch rc=0
  if ! marked_epoch=$(_reap_iso_utc_to_epoch "$marked_at"); then
    warn "Reap marker has no readable timestamp; dropping it without a teardown."
    rm -f "$DEV_ENV_STATE_FILE"; return 0
  fi
  if ! dev_env_reap_gate_env; then
    warn "Reap marker's env file no longer passes the dev-cluster gate; dropping it without a teardown."
    rm -f "$DEV_ENV_STATE_FILE"; return 0
  fi
  if [[ -z "${PRAUTO_WORKER_ID:-}" ]]; then
    warn "No worker identity to hold the dev-env lock under; keeping the reap marker."; return 0
  fi
  _reap_engine recover "$marked_epoch" || rc=$?
  case "$rc" in
    0) : ;;
    11) warn "The dev cluster changed since the reap began; dropping the reap marker without deleting."
        rm -f "$DEV_ENV_STATE_FILE" ;;
    12) if dev_env_namespaces_absent "$DEV_ENV_FILE"; then
          rm -f "$DEV_ENV_STATE_FILE"
          info "The reaped dev cluster is already gone; marker cleared."
        else
          warn "Could not confirm the reaped dev cluster is gone; keeping the reap marker."
        fi ;;
    *) warn "Keeping the reap marker; a later heartbeat will retry." ;;
  esac
  return 0
}

# reap_ownerless_idle_dev_env
# Tear down a dev-profile cluster that this worker did NOT provision, but only
# when it is demonstrably idle and provably a dev cluster. Called once from
# heartbeat.sh's EXIT trap, so it NEVER returns non-zero and every failure is a
# logged skip. See _reap_engine for ordering and dev_env_reap_gate_env for what
# makes an env file eligible.
reap_ownerless_idle_dev_env() {
  local threshold="${PRAUTO_DEV_ENV_REAP_IDLE_SECS-7200}" rc=0

  # (a) Threshold. Invalid disables rather than falls back to the default: a typo
  # in a destructive cost knob must not silently arm the destructive behaviour.
  # The length cap keeps the 10# conversion below inside bash's integer range.
  if [[ ! "$threshold" =~ ^[0-9]+$ ]] || (( ${#threshold} > 12 )); then
    warn "Invalid PRAUTO_DEV_ENV_REAP_IDLE_SECS; idle-cluster reaping is disabled."
    return 0
  fi
  threshold=$(( 10#$threshold ))   # 10#: a zero-padded value is not octal
  if (( threshold == 0 )); then
    info "Idle-cluster reaping is disabled (PRAUTO_DEV_ENV_REAP_IDLE_SECS=0)."
    return 0
  fi

  # (b) Ownership and this wake's own use. A marker means marker recovery owns the
  # cluster; DEV_ENV_PROVISIONED means this very wake built it (and its teardown
  # has already run or been retried above); either way it is not "ownerless".
  if [[ -z "${DEV_ENV_STATE_FILE:-}" ]]; then
    info "Skipping idle reap: the dev-env marker path is unavailable."; return 0
  fi
  if [[ -e "$DEV_ENV_STATE_FILE" ]]; then
    info "Skipping idle reap: a dev-env provisioning marker exists."; return 0
  fi
  if [[ "${DEV_ENV_PROVISIONED:-false}" == true ]]; then
    info "Skipping idle reap: this heartbeat provisioned the cluster."; return 0
  fi
  if [[ "${DEV_ENV_TOUCHED_THIS_WAKE:-false}" == true ]]; then
    info "Skipping idle reap: this heartbeat used the dev cluster."; return 0
  fi
  if [[ -z "${PRAUTO_WORKER_ID:-}" ]]; then
    info "Skipping idle reap: no worker identity to hold the lock under."; return 0
  fi

  # (c) The cluster this worker is bound to, and whether it is one we may delete.
  if ! resolve_dev_env; then
    info "Skipping idle reap: the dev env file is unavailable."; return 0
  fi
  dev_env_reap_gate_env || return 0

  _reap_engine reap "$threshold" || rc=$?
  case "$rc" in
    0) : ;;
    12) info "No dev cluster found to reap." ;;
    *) : ;;   # the engine logged its own fixed skip reason
  esac
  return 0
}
