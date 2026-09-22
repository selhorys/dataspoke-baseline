# Job state management for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, git available.

STATE_DIR="${PRAUTO_DIR}/state"
LOCK_FILE="${STATE_DIR}/heartbeat.lock"
SESSIONS_DIR="${STATE_DIR}/sessions"
# Persistent, local-only anchors for agent-native sessions. Unlike SESSIONS_DIR,
# this directory survives reset_ephemeral_state so a later heartbeat can verify
# a GitHub pause marker before asking Codex to resume it.
NATIVE_SESSIONS_DIR="${STATE_DIR}/native-sessions"

# Current issue session directory (set by init_issue_session).
CUR_SESSION_DIR=""

# Ensure the state/worktree directories exist.
ensure_state_dirs() {
  mkdir -p "$STATE_DIR" "$SESSIONS_DIR" "$NATIVE_SESSIONS_DIR" "${PRAUTO_DIR}/worktrees"
  # STATE_DIR holds the dev-lock token (a capability), the provisioning marker
  # whose env_file path aims a --delete-all teardown, and undelivered report
  # bodies. All three are decisions this worker acts on, so the directory is the
  # worker's own, not the umask's.
  chmod 700 "$STATE_DIR" 2>/dev/null || true
  chmod 700 "$NATIVE_SESSIONS_DIR" 2>/dev/null || true
}

# is_strict_uuid <value>
# Codex accepts either a UUID or a thread name. PRauto persists and resumes only
# canonical UUIDs so a GitHub comment cannot turn a session field into a name or
# an option-like argument.
is_strict_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

# native_session_anchor_file <issue_number>
native_session_anchor_file() {
  local issue_number="$1"
  [[ "$issue_number" =~ ^[0-9]+$ ]] || return 1
  printf '%s/issue-%s.json' "$NATIVE_SESSIONS_DIR" "$issue_number"
}

# record_codex_native_session <issue> <ready_timestamp> <thread_id>
# Persist the exact local proof only after Codex emitted thread.started. The
# ready timestamp binds a reused issue number to its current GitHub lifecycle.
record_codex_native_session() {
  local issue_number="$1" ready_timestamp="$2" thread_id="$3"
  [[ -n "$ready_timestamp" ]] || return 1
  is_strict_uuid "$thread_id" || return 1
  local anchor_file tmp_file
  anchor_file=$(native_session_anchor_file "$issue_number") || return 1
  mkdir -p "$NATIVE_SESSIONS_DIR"
  chmod 700 "$NATIVE_SESSIONS_DIR" 2>/dev/null || true
  tmp_file=$(mktemp "${anchor_file}.tmp.XXXXXX") || return 1
  if ! jq -n \
      --arg issue_number "$issue_number" \
      --arg ready_timestamp "$ready_timestamp" \
      --arg agent "codex" \
      --arg thread_id "$thread_id" \
      '{issue_number: $issue_number, ready_timestamp: $ready_timestamp, agent: $agent, thread_id: $thread_id}' \
      > "$tmp_file"; then
    rm -f "$tmp_file"
    return 1
  fi
  chmod 600 "$tmp_file" 2>/dev/null || true
  mv -f "$tmp_file" "$anchor_file"
}

# codex_native_session_anchor_matches <issue> <ready_timestamp> <thread_id>
# Validate a local anchor exactly. GitHub remains phase SSOT; this proves only
# that the session id in its marker was created by this executor in this issue's
# current lifecycle.
codex_native_session_anchor_matches() {
  local issue_number="$1" ready_timestamp="$2" thread_id="$3"
  [[ -n "$ready_timestamp" ]] || return 1
  is_strict_uuid "$thread_id" || return 1
  local anchor_file
  anchor_file=$(native_session_anchor_file "$issue_number") || return 1
  [[ -f "$anchor_file" ]] || return 1
  jq -e \
    --arg issue_number "$issue_number" \
    --arg ready_timestamp "$ready_timestamp" \
    --arg thread_id "$thread_id" '
      .issue_number == $issue_number
      and .ready_timestamp == $ready_timestamp
      and .agent == "codex"
      and .thread_id == $thread_id
    ' "$anchor_file" >/dev/null 2>&1
}

# Initialize a per-issue session directory.
# Creates .prauto/state/sessions/issue-<n>/<ts>-<uuid8>/ and sets CUR_SESSION_DIR.
# Usage: init_issue_session <issue_number>
init_issue_session() {
  local issue_number="$1"
  local ts uuid
  ts=$(date -u '+%Y%m%d-%H%M%S')
  uuid=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s-$$)
  uuid=$(printf '%s' "$uuid" | tr '[:upper:]' '[:lower:]')
  local session_id="${ts}-${uuid:0:8}"
  CUR_SESSION_DIR="${SESSIONS_DIR}/issue-${issue_number}/${session_id}"
  mkdir -p "$CUR_SESSION_DIR"
  info "Session dir: ${CUR_SESSION_DIR}"
}

# Acquire the PID lock. Returns 0 on success, 1 if another heartbeat holds it.
#
# The lock is the concurrency gate for the whole wake: a second heartbeat must
# not run a worker against a worktree another one is mid-flight on. The lock
# holds the holder's PID; `kill -0` distinguishes a live holder from a stale
# lock left by a crashed run (which is removed and re-acquired).
acquire_lock() {
  ensure_state_dirs

  if [[ -f "$LOCK_FILE" ]]; then
    local existing_pid
    existing_pid=$(cat "$LOCK_FILE" 2>/dev/null || printf '')
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      warn "Another heartbeat is running (PID $existing_pid). Exiting."
      return 1
    fi
    warn "Removing stale lock file (PID $existing_pid no longer running)."
    rm -f "$LOCK_FILE"
  fi

  printf '%s' "$$" > "$LOCK_FILE"
  return 0
}

# Release the lock. Safe to call when it is already absent.
release_lock() {
  if [[ -f "$LOCK_FILE" ]]; then
    local lock_pid
    lock_pid=$(cat "$LOCK_FILE" 2>/dev/null || printf '')
    [[ "$lock_pid" == "$$" ]] && rm -f "$LOCK_FILE"
  fi
}

# Reset ephemeral state at wake start. GitHub is the single source of truth, so
# each wake begins from a clean local slate; any work a prior (crashed) run left
# uncommitted in a worktree is invisible to resume and must not leak into the
# next run's fresh checkout. This sweeps orphaned worktrees so a dead worker
# cannot leave a dirty tree that the next tick would re-edit.
reset_ephemeral_state() {
  # Remove the rendered system prompt (regenerated each invocation).
  rm -f "${STATE_DIR}/.system-append-rendered.md"

  if [[ -d "${PRAUTO_DIR}/worktrees" ]]; then
    local wt
    for wt in "${PRAUTO_DIR}/worktrees"/*/; do
      [[ -d "$wt" ]] || continue
      warn "Removing orphaned worktree: $wt"
      git -C "$REPO_DIR" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
    done
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
  fi
}

# Abandon a job after max retries or a workflow escalation.
# Writes an abandon record, swaps labels (wip/plan-review -> failed), posts an
# idempotent "Abandoning" comment.
# Usage: abandon_job_github <issue_number> <retry_count> [reason]
abandon_job_github() {
  local issue_number="$1" retry_count="$2" reason="${3:-}"

  local history_file
  if [[ -n "$CUR_SESSION_DIR" ]] && [[ -d "$CUR_SESSION_DIR" ]]; then
    history_file="${CUR_SESSION_DIR}/abandon.json"
  else
    history_file="${SESSIONS_DIR}/$(date +%Y%m%d)_abandon_I-${issue_number}.json"
  fi
  jq -n \
    --argjson issue_number "$issue_number" \
    --argjson retry_count "$retry_count" \
    --arg abandoned_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg reason "$reason" \
    '{issue_number: $issue_number, abandoned_at: $abandoned_at, retry_count: $retry_count, reason: $reason}' \
    > "$history_file"
  info "Job for issue #${issue_number} abandoned -> ${history_file}"

  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --add-label "$PRAUTO_GITHUB_LABEL_FAILED" 2>/dev/null \
    || warn "Failed to update labels on issue #${issue_number}"
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || true

  if ! comment_exists "issue" "$issue_number" "Abandoning"; then
    local body="Abandoning after ${retry_count} retries. Manual intervention needed."
    [[ -n "$reason" ]] && body="${body}
Blocked by: ${reason}"
    prauto_issue_comment "$issue_number" "$body" \
      "Failed to post abandonment comment on issue #${issue_number}"
  fi

  # The lifecycle is over; a re-queue must not inherit these notes.
  clear_blocked_reasons "$issue_number"
}

# Record job completion to history.
# Usage: complete_job <issue_number>
complete_job() {
  local issue_number="$1"
  local history_file
  if [[ -n "$CUR_SESSION_DIR" ]] && [[ -d "$CUR_SESSION_DIR" ]]; then
    history_file="${CUR_SESSION_DIR}/complete.json"
  else
    history_file="${SESSIONS_DIR}/$(date +%Y%m%d)_complete_I-${issue_number}.json"
  fi
  jq -n \
    --argjson issue_number "$issue_number" \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issue_number: $issue_number, completed_at: $completed_at}' \
    > "$history_file"
  info "Job for issue #${issue_number} completed -> ${history_file}"
  clear_blocked_reasons "$issue_number"
}

# ---- Retry counter (local state, not GitHub comments) ------------------------
# Quota-pause cycles do not reach the normal dispatch path, so a state-file
# counter correctly tracks only genuine attempt starts. The counter is checked
# and incremented in heartbeat.sh's normal dispatch; a resume is a continuation,
# not a new attempt. Records are scoped to the current prauto:ready label event:
# re-queueing an issue creates a fresh retry lifecycle even though its number is
# unchanged.

# retry_count_file <issue_number> — path to the counter state file.
retry_count_file() {
  local issue_number="$1"
  [[ "$issue_number" =~ ^[0-9]+$ ]] || return 1
  printf '%s/retry-count-%s.json' "$STATE_DIR" "$issue_number"
}

# Infrastructure blocks are recorded beside the counter, under the same
# ready-label lifecycle anchor, because the abandonment that needs them happens in
# a LATER heartbeat process: a block recorded on attempt 3 must still be readable
# when attempt 5 finds the budget exhausted, and a shell variable cannot cross
# that gap. Without this, an issue the infrastructure never let start leaves a
# record indistinguishable from one whose code genuinely failed.

# blocked_reasons_file <issue_number>
blocked_reasons_file() {
  local issue_number="$1"
  [[ "$issue_number" =~ ^[0-9]+$ ]] || return 1
  printf '%s/blocked-reasons-%s.json' "$STATE_DIR" "$issue_number"
}

# record_blocked_reason <issue_number> <reason>
# Append one reason for the current lifecycle, de-duplicated and capped. Failure
# to persist is never fatal: losing a diagnostic note must not block a run.
record_blocked_reason() {
  local issue_number="$1" reason="$2" bf ready_ts existing tmp_file
  # Validated before it reaches jq, like PRAUTO_MAX_REFUNDS_PER_JOB below. A
  # non-numeric value makes the write fail silently and every reason is dropped —
  # the F7 state this record exists to fix. Zero is worse than a no-op: jq's
  # `.[-0:]` is `.[0:]`, which disables the cap instead of applying it.
  local cap="${PRAUTO_MAX_BLOCKED_REASONS:-10}"
  if [[ ! "$cap" =~ ^[1-9][0-9]*$ ]]; then
    warn "Invalid PRAUTO_MAX_BLOCKED_REASONS='${cap}'; using the default of 10."
    cap=10
  fi
  [[ -n "$reason" ]] || return 0
  bf=$(blocked_reasons_file "$issue_number") || return 0
  ready_ts="${READY_LABEL_TIMESTAMP:-}"
  [[ -n "$ready_ts" ]] || return 0
  existing=$(read_blocked_reasons_json "$issue_number")
  tmp_file=$(mktemp "${bf}.tmp.XXXXXX") || return 0
  if jq -n \
    --argjson issue_number "$issue_number" \
    --argjson reasons "$existing" \
    --arg reason "$reason" \
    --argjson cap "$cap" \
    --arg ready_label_timestamp "$ready_ts" \
    --arg last_updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issue_number: $issue_number,
      reasons: (($reasons + [$reason])
                | reduce .[] as $r ([]; if index($r) then . else . + [$r] end)
                | .[-$cap:]),
      ready_label_timestamp: $ready_label_timestamp,
      last_updated: $last_updated}' \
    > "$tmp_file" 2>/dev/null; then
    mv -f "$tmp_file" "$bf" 2>/dev/null || rm -f "$tmp_file"
  else
    rm -f "$tmp_file"
  fi
  return 0
}

# read_blocked_reasons_json <issue_number>
# Echo the current lifecycle's reasons as a JSON array; `[]` when missing,
# malformed, or from a different ready-label lifecycle.
read_blocked_reasons_json() {
  local issue_number="$1" bf ready_ts out
  bf=$(blocked_reasons_file "$issue_number") || { printf '[]'; return 0; }
  ready_ts="${READY_LABEL_TIMESTAMP:-}"
  [[ -n "$ready_ts" ]] || { printf '[]'; return 0; }
  out=$(jq -ec \
    --argjson issue_number "$issue_number" \
    --arg ready_label_timestamp "$ready_ts" '
      select(
        (.issue_number | type) == "number"
        and .issue_number == $issue_number
        and (.reasons | type) == "array"
        and (.ready_label_timestamp | type) == "string"
        and .ready_label_timestamp == $ready_label_timestamp
      )
      | [.reasons[] | select(type == "string" and . != "")]
    ' "$bf" 2>/dev/null) || out="[]"
  printf '%s' "${out:-[]}"
}

# read_blocked_reasons <issue_number>
# Echo the reasons as one human-readable line, or nothing.
read_blocked_reasons() {
  local issue_number="$1"
  read_blocked_reasons_json "$issue_number" | jq -r 'join("; ")' 2>/dev/null || true
}

# clear_blocked_reasons <issue_number>
clear_blocked_reasons() {
  local issue_number="$1" bf
  bf=$(blocked_reasons_file "$issue_number") || return 0
  rm -f "$bf"
  return 0
}

# read_retry_count <issue_number>
# Sets RETRY_COUNT. A missing, malformed, or different-ready-lifecycle record
# deliberately yields zero; increment_retry_count will replace it atomically.
read_retry_count() {
  local issue_number="$1" cf ready_ts
  cf=$(retry_count_file "$issue_number") || { RETRY_COUNT=0; return 0; }
  ready_ts="${READY_LABEL_TIMESTAMP:-}"

  # A retry counter without a lifecycle anchor is never safe to reuse.
  if [[ -z "$ready_ts" ]]; then
    RETRY_COUNT=0
    return 0
  fi

  RETRY_COUNT=$(jq -er \
    --argjson issue_number "$issue_number" \
    --arg ready_label_timestamp "$ready_ts" '
      select(
        (.issue_number | type) == "number"
        and .issue_number == $issue_number
        and (.count | type == "number" and . >= 0 and floor == .)
        and (.ready_label_timestamp | type) == "string"
        and (.ready_label_timestamp != "")
        and .ready_label_timestamp == $ready_label_timestamp
      )
      | .count
    ' "$cf" 2>/dev/null) || RETRY_COUNT=0
}

# increment_retry_count <issue_number>
# Increment the current retry count by one, persist it atomically, and set
# RETRY_COUNT to the new value so callers can use it directly.
increment_retry_count() {
  local issue_number="$1"
  read_retry_count "$issue_number"
  read_refund_count "$issue_number"
  write_retry_count "$issue_number" $((RETRY_COUNT + 1)) "$REFUND_COUNT"
}

# refund_retry_count <issue_number>
# Give back the attempt this dispatch consumed, flooring at zero. The counter is
# incremented at dispatch, before the outcome is known, so an attempt that turns
# out not to be the worker's failure — the agent CLI truncating a session that was
# still making progress — must be handed back or a harness limitation burns the
# job's retry budget.
#
# Two bounds keep the refund from defeating the abandonment guarantee:
#
#  - It only refunds what THIS dispatch consumed. RETRY_COUNT_CONSUMED is set by
#    the heartbeat's normal dispatch path, the only path that increments. The
#    plan-approval and quota-resume paths deliberately bypass the counter
#    (spec/AI_PRAUTO.md §Retry tracking), so a refund there would hand the job a
#    free attempt taken from an earlier dispatch's tally.
#  - PRAUTO_MAX_REFUNDS_PER_JOB caps refunds per ready-label lifecycle. Even if
#    some future signature turns out to be reachable by the worker, a job cannot
#    be made unabandonable: past the cap, attempts are consumed normally.
refund_retry_count() {
  local issue_number="$1"

  if [[ "${RETRY_COUNT_CONSUMED:-false}" != true ]]; then
    info "Issue #${issue_number}: this dispatch consumed no retry; nothing to refund."
    return 0
  fi

  read_refund_count "$issue_number"
  # Validate the override before it reaches an arithmetic comparison, the way
  # provision_dev_env validates its timeout. A typo must fall back to the
  # documented default, not silently unbound or disable the cap.
  local max_refunds="${PRAUTO_MAX_REFUNDS_PER_JOB:-2}"
  if [[ ! "$max_refunds" =~ ^[0-9]+$ ]]; then
    warn "Invalid PRAUTO_MAX_REFUNDS_PER_JOB='${max_refunds}'; using the default of 2."
    max_refunds=2
  fi
  if [[ "$REFUND_COUNT" -ge "$max_refunds" ]]; then
    warn "Issue #${issue_number}: refund cap reached (${REFUND_COUNT}/${max_refunds}); this attempt stays counted."
    return 0
  fi

  read_retry_count "$issue_number"
  [[ "$RETRY_COUNT" -gt 0 ]] || return 0
  write_retry_count "$issue_number" $((RETRY_COUNT - 1)) $((REFUND_COUNT + 1))
}

# read_refund_count <issue_number>
# Sets REFUND_COUNT for the issue's current ready-label lifecycle. Same safety as
# read_retry_count, and deliberately the same strictness: this counter is the only
# bound on the abandonment guarantee, so a record carrying a negative or fractional
# value must read as zero rather than flow into an arithmetic comparison — under
# the heartbeat's `set -euo pipefail` a non-integer there kills the wake instead of
# degrading. A missing, malformed, or foreign-lifecycle record yields zero.
read_refund_count() {
  local issue_number="$1" cf
  REFUND_COUNT=0
  [[ -n "${READY_LABEL_TIMESTAMP:-}" ]] || return 0
  cf=$(retry_count_file "$issue_number") || return 0
  [[ -f "$cf" ]] || return 0
  REFUND_COUNT=$(jq -er \
    --argjson issue_number "$issue_number" \
    --arg ready_label_timestamp "$READY_LABEL_TIMESTAMP" '
    if (.issue_number == $issue_number)
       and (.ready_label_timestamp == $ready_label_timestamp)
       and ((.refund_count // 0) | type == "number")
       and ((.refund_count // 0) >= 0)
       and (((.refund_count // 0) | floor) == (.refund_count // 0))
    then (.refund_count // 0) else 0 end
    ' "$cf" 2>/dev/null) || REFUND_COUNT=0
}

# write_retry_count <issue_number> <count> <refund_count>
# Persist <count> and <refund_count> atomically against the current ready-label
# lifecycle, setting RETRY_COUNT and REFUND_COUNT to them. Shared by
# increment_retry_count and refund_retry_count so both write one record shape —
# the refund tally has to ride in the same record, or a refund that raced a
# rewrite could reset its own cap.
write_retry_count() {
  local issue_number="$1" new_count="$2" new_refund_count="${3:-0}" cf ready_ts
  cf=$(retry_count_file "$issue_number") || return 1
  ready_ts="${READY_LABEL_TIMESTAMP:-}"
  [[ -n "$ready_ts" ]] || {
    warn "Cannot write retry count for #${issue_number}: missing ready-label timestamp"
    RETRY_COUNT=0
    return 1
  }
  local tmp_file
  tmp_file=$(mktemp "${cf}.tmp.XXXXXX") || return 1
  if ! jq -n \
    --argjson issue_number "$issue_number" \
    --argjson count "$new_count" \
    --argjson refund_count "$new_refund_count" \
    --arg ready_label_timestamp "$ready_ts" \
    --arg last_updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issue_number: $issue_number, count: $count, refund_count: $refund_count, ready_label_timestamp: $ready_label_timestamp, last_updated: $last_updated}' \
    > "$tmp_file"; then
    rm -f "$tmp_file"
    return 1
  fi
  if ! mv -f "$tmp_file" "$cf"; then
    rm -f "$tmp_file"
    return 1
  fi
  RETRY_COUNT=$new_count
  REFUND_COUNT=$new_refund_count
}
