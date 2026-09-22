#!/usr/bin/env bash
# prauto monitor — detached Slack reporter for one executor run (Hermes scheduler binding).
#
# Spawned by the supervisor (the Hermes cron agent) AFTER it detaches
# .prauto/heartbeat.sh. This is a plain sleep-loop with NO LLM and NO issue work:
#   * polls the executor PID every PRAUTO_MONITOR_CHECK_SECS (default 60)
#   * posts a brief Slack note every PRAUTO_MONITOR_INTERVAL_SECS (default 600) while running
#   * posts a final result (done / no-agent / waiting-approval / quota-paused / crashed)
#     and exits as soon as the executor process is gone
#   * holds a monitor.lock so a re-fired cron tick never double-reports
#   * scrubs credentials from every message before it reaches Slack
#
# Usage: bash .prauto/scheduler/monitor.sh [executor_pid]
#   executor_pid defaults to the executor's PID lock (.prauto/state/heartbeat.lock).
# Set PRAUTO_MONITOR_DRY_RUN=1 to print the Slack messages instead of sending them.
#
# Slack delivery is via `hermes send` (no LLM, no running gateway required for the
# bot-token path). This script is part of the Hermes scheduler binding; other
# scheduler bindings supply their own reporting and do not use this file.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRAUTO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$PRAUTO_DIR/state"
LOG="$STATE_DIR/heartbeat_cron.log"
EXEC_LOCK="$STATE_DIR/heartbeat.lock"
MONITOR_LOCK="$STATE_DIR/monitor.lock"

# GUI/cron-launched processes often inherit a minimal PATH; make `hermes` and the
# log tooling resolvable regardless of how the supervisor was launched.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

if [[ -f "$PRAUTO_DIR/config.env" ]]; then
  # shellcheck source=../config.env
  source "$PRAUTO_DIR/config.env"
fi
if [[ -f "$PRAUTO_DIR/config.local.env" ]]; then
  # shellcheck source=../config.local.env
  source "$PRAUTO_DIR/config.local.env"
fi

# Slack credentials — and the channel directory that resolves the target name —
# come from ONE Hermes profile: the one running this job. The inherited
# HERMES_HOME is right for a cron-launched supervisor, but a monitor started from
# a plain shell (or any launcher that dropped the variable) would silently fall
# back to the default home, where the target does not exist and every send fails.
# Resolution order: the instance override (PRAUTO_SCHEDULER_HERMES_HOME in
# config.local.env), the inherited HERMES_HOME, then the default home. A home that
# cannot resolve the target is caught by the preflight below and fails loudly.
HERMES_HOME="${PRAUTO_SCHEDULER_HERMES_HOME:-${HERMES_HOME:-$HOME/.hermes}}"
export HERMES_HOME

SLACK_TARGET="${PRAUTO_SLACK_TARGET:-slack}"
CHECK_SECS="${PRAUTO_MONITOR_CHECK_SECS:-60}"
INTERVAL_SECS="${PRAUTO_MONITOR_INTERVAL_SECS:-600}"
SEND_RETRY_SECS="${PRAUTO_MONITOR_SEND_RETRY_SECS:-5}"
SEND_ATTEMPTS="${PRAUTO_MONITOR_SEND_ATTEMPTS:-4}"
[[ "$SEND_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || SEND_ATTEMPTS=4
# SEND_RETRY_SECS now feeds arithmetic (the backoff doubles it), so it is
# validated too: a non-numeric value there kills the monitor mid-run and the
# detached run loses all reporting — the outcome this whole section prevents.
[[ "$SEND_RETRY_SECS" =~ ^[0-9]+$ ]] || SEND_RETRY_SECS=5
SEND_RETRY_MAX_SECS="${PRAUTO_MONITOR_SEND_RETRY_MAX_SECS:-120}"
[[ "$SEND_RETRY_MAX_SECS" =~ ^[1-9][0-9]*$ ]] || SEND_RETRY_MAX_SECS=120
SEND_FAILURES=0
WORKER="${PRAUTO_WORKER_ID:-prauto01}"
UNRESOLVED_MARKER="$STATE_DIR/monitor-slack-unresolved"
UNDELIVERED_LOG="$STATE_DIR/monitor-undelivered.log"
UNDELIVERED_MAX_BYTES=262144

# scrub_secret_values — emit one credential value per line, read from the dev
# env file by name. Never sources the file: a value is data, not code.
# Cached after the first read; the file does not change mid-run.
SCRUB_SECRET_VALUES_CACHE=""
SCRUB_SECRET_VALUES_LOADED=false
scrub_secret_values() {
  if [[ "$SCRUB_SECRET_VALUES_LOADED" != true ]]; then
    SCRUB_SECRET_VALUES_LOADED=true
    local env_file="${PRAUTO_DEV_ENV_FILE:-}"
    [[ -n "$env_file" ]] || env_file="${PRAUTO_REPO_DIR:-$(cd "$PRAUTO_DIR/.." && pwd)}/helm-charts/.env.dev"
    if [[ -r "$env_file" ]]; then
      SCRUB_SECRET_VALUES_CACHE=$(grep -E '^(DATASPOKE|POSTGRES|REDIS|AIRFLOW|LANGFUSE|DATAHUB)_[A-Z0-9_]*(PASSWORD|TOKEN|SECRET|KEY|PAT)=' "$env_file" 2>/dev/null \
        | sed -E 's/^[^=]+=//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//' || true)
    fi
  fi
  [[ -n "$SCRUB_SECRET_VALUES_CACHE" ]] && printf '%s\n' "$SCRUB_SECRET_VALUES_CACHE"
  return 0
}

# scrub <text> — redact credentials before anything leaves this machine. The
# executor log can contain the worker's GH_TOKEN / ANTHROPIC_API_KEY (and, in a
# failing test, DATASPOKE_* / JWT / dsk_ tokens), so every Slack message is
# scrubbed at the single `say` choke point.
#
# Two layers, because neither alone is enough: pattern rules for token shapes and
# for `NAME=value` as it appears in a log line, then an exact-value pass over any
# credential-shaped variable this process carries, which catches a value printed
# on its own with no name beside it.
scrub() {
  local text="$1"
  text=$(printf '%s' "$text" | sed -E \
    -e 's#://[^:@/[:space:]]+:[^@/[:space:]]+@#://***REDACTED***@#g' \
    -e 's/(gho|ghp|ghs|ghu|github_pat)_[A-Za-z0-9]+/***REDACTED***/g' \
    -e 's/sk-[A-Za-z0-9]+/***REDACTED***/g' \
    -e 's/dsk_[A-Za-z0-9_-]+/***REDACTED***/g' \
    -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/***REDACTED***/g' \
    -e 's/(DATASPOKE|POSTGRES|REDIS|AIRFLOW|LANGFUSE|DATAHUB)_[A-Z0-9_]*(PASSWORD|TOKEN|SECRET|KEY|PAT)=[^[:space:]]+/\1_***REDACTED***/g')
  [[ -n "${GH_TOKEN:-}" ]] && text="${text//${GH_TOKEN}/***REDACTED***}"
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && text="${text//${ANTHROPIC_API_KEY}/***REDACTED***}"
  # Value-scrub the credentials this run actually uses. The pattern rules above
  # only catch `NAME=value` as it appears in a log line; a bare value printed on
  # its own (a traceback, a curl error, a failing assertion) matches nothing —
  # and the final report embeds 12 lines of executor log, which is exactly where
  # such a value shows up.
  #
  # The values are read FROM THE DEV ENV FILE, not from this process's
  # environment: monitor.sh is a separate process from the executor and sources
  # only config.env/config.local.env, neither of which defines any credential,
  # so an environment sweep here matches nothing at all. The env file is read
  # line-wise rather than sourced, so nothing it contains is executed.
  local val
  while IFS= read -r val; do
    [[ "${#val}" -ge 8 ]] || continue
    text="${text//${val}/***REDACTED***}"
  done < <(scrub_secret_values)
  printf '%s' "$text"
}

# resolve_slack_target — verify (without sending) that `hermes send` can resolve
# SLACK_TARGET under the resolved HERMES_HOME. `hermes send --list <platform>` reads
# the profile's channel directory locally: no network, no message posted.
resolve_slack_target() {
  local platform="${SLACK_TARGET%%:*}" listing
  if ! listing=$(hermes send --list "$platform" 2>&1); then
    printf '%s\n' "$listing" >&2
    return 1
  fi
  [[ -n "$listing" ]] || return 1
  # A bare platform name ("slack") is accepted on a non-empty listing: --list shows the
  # platform is configured but not that a home channel exists, so a wrong bare target
  # surfaces at send time (and lands in the undelivered log). A qualified target must be
  # a whole entry name — followed by end-of-line or whitespace — so "slack:hermes-dev"
  # does not match "slack:hermes-dev-2".
  if [[ "$SLACK_TARGET" == *:* ]]; then
    awk -v t="$SLACK_TARGET" '
      { sub(/^[ \t]+/, "") }
      index($0, t) == 1 { rest = substr($0, length(t) + 1); if (rest == "" || rest ~ /^[ \t]/) found = 1 }
      END { exit !found }' <<< "$listing" || return 1
  fi
  return 0
}

# say <message> — scrub, then post to the Slack target; print on dry-run.
say() {
  local msg
  msg=$(scrub "$1")
  if [[ "${PRAUTO_MONITOR_DRY_RUN:-0}" == "1" ]]; then
    printf '[dry-run -> %s]\n%s\n' "$SLACK_TARGET" "$msg"
    return 0
  fi
  # Retries back off, because the outages that take Slack out are minutes long,
  # not seconds: a flat pair of tries 5s apart covers a gateway blip and nothing
  # else. If the message still cannot leave the machine, record it locally —
  # Slack is the only human surface for a detached run, so an undelivered report
  # must leave a trace someone can find, and a later flush can still deliver it.
  local attempt delay="$SEND_RETRY_SECS"
  for (( attempt = 1; attempt <= SEND_ATTEMPTS; attempt++ )); do
    if hermes send --to "$SLACK_TARGET" "$msg" >/dev/null 2>&1; then
      return 0
    fi
    if (( attempt < SEND_ATTEMPTS )); then
      sleep "$delay"
      delay=$(( delay * 2 ))
      (( delay > SEND_RETRY_MAX_SECS )) && delay="$SEND_RETRY_MAX_SECS"
    fi
  done
  SEND_FAILURES=$(( SEND_FAILURES + 1 ))
  printf 'monitor: slack send failed\n' >&2
  record_undelivered "$msg"
  return 1
}

# record_undelivered <message> — the single append path for an undelivered note.
# One function, so a partial or failed write is itself reported instead of
# leaving a silent hole in the only local record of what Slack never received.
#
# One RECORD per message, not one line: the messages that matter most are
# multi-line (the final report is a summary plus a fenced 12-line log excerpt).
# A line-based file would flush that back as a dozen separate Slack posts with
# the fence characters posted on their own. The body is base64'd onto a single
# physical line so a record is unambiguous, and the size bound below drops whole
# oldest records rather than cutting one in half.
record_undelivered() {
  local msg="$1" stamp encoded
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  encoded=$(printf '%s' "$msg" | base64 | tr -d '\n') || return 1
  # The file carries message bodies, which is scrubbed output but still the
  # run's own reporting. Created restricted before anything is written to it.
  if [[ ! -f "$UNDELIVERED_LOG" ]]; then
    (umask 077; : >> "$UNDELIVERED_LOG") 2>/dev/null || true
    chmod 600 "$UNDELIVERED_LOG" 2>/dev/null || true
  fi
  if ! printf '%s\t%s\n' "$stamp" "$encoded" >> "$UNDELIVERED_LOG" 2>/dev/null; then
    printf 'monitor: could not append to %s\n' "$UNDELIVERED_LOG" >&2
    return 1
  fi
  # Bound the file by whole records: past the cap, keep the newest half's records.
  local size
  size=$(wc -c < "$UNDELIVERED_LOG" 2>/dev/null || printf 0)
  if [[ "$size" -gt "$UNDELIVERED_MAX_BYTES" ]]; then
    local keep
    keep=$(( $(wc -l < "$UNDELIVERED_LOG" 2>/dev/null || printf 2) / 2 ))
    [[ "$keep" -ge 1 ]] || keep=1
    if tail -n "$keep" "$UNDELIVERED_LOG" > "$UNDELIVERED_LOG.tmp" 2>/dev/null; then
      chmod 600 "$UNDELIVERED_LOG.tmp" 2>/dev/null || true
      mv -f "$UNDELIVERED_LOG.tmp" "$UNDELIVERED_LOG" 2>/dev/null || rm -f "$UNDELIVERED_LOG.tmp"
    fi
  fi
  return 0
}

# flush_undelivered — one attempt, at monitor exit, to deliver what the outage
# swallowed. An outage that ends before the run does otherwise leaves the backlog
# on disk forever: nothing else reads this file.
flush_undelivered() {
  [[ "${PRAUTO_MONITOR_DRY_RUN:-0}" == "1" ]] && return 0
  [[ -s "$UNDELIVERED_LOG" ]] || return 0
  # No liveness probe: a synthetic "are you up?" message would itself be posted
  # to the channel. The first real record is the probe — once it fails, the
  # channel is still down, so the rest is carried over untouched.
  local line stamp encoded body delivered=0 down=false remaining="${UNDELIVERED_LOG}.remaining"
  (umask 077; : > "$remaining") 2>/dev/null || return 0
  while IFS=$'\t' read -r stamp encoded; do
    [[ -n "$encoded" ]] || continue
    body=$(printf '%s' "$encoded" | base64 --decode 2>/dev/null) || body=""
    [[ -n "$body" ]] || continue
    # Re-scrubbed on the way out: a redaction rule added after the write must
    # still apply to a record written before it.
    if [[ "$down" == false ]] && hermes send --to "$SLACK_TARGET" "$(scrub "$body")" >/dev/null 2>&1; then
      delivered=$(( delivered + 1 ))
    else
      down=true
      printf '%s\t%s\n' "$stamp" "$encoded" >> "$remaining"
    fi
  done < "$UNDELIVERED_LOG"
  mv -f "$remaining" "$UNDELIVERED_LOG" 2>/dev/null || rm -f "$remaining"
  [[ -s "$UNDELIVERED_LOG" ]] || rm -f "$UNDELIVERED_LOG"
  (( delivered > 0 )) && printf 'monitor: flushed %d undelivered message(s)\n' "$delivered" >&2
  return 0
}

# report_delivery_health — promote accumulated send failures to an [ERROR] line
# in the executor log. The launcher reports "started, monitor attached" whether
# or not anything reached Slack; without this, a run whose entire report stream
# was lost reads as healthy.
report_delivery_health() {
  (( SEND_FAILURES > 0 )) || return 0
  printf '[ERROR] monitor(%s): reporting degraded — %d Slack message(s) undelivered (see %s).\n' \
    "$WORKER" "$SEND_FAILURES" "$UNDELIVERED_LOG" >> "$LOG" 2>/dev/null || true
  return 0
}

is_alive() { kill -0 "$1" 2>/dev/null; }

# run_text — the executor log from the LAST "=== prauto heartbeat —" header to
# EOF, ANSI colour codes stripped. Every wake opens a new header, so this is
# exactly the current run's output — classify() and the final note must read
# only this, never a prior run's markers.
run_text() {
  local header_line
  header_line=$(grep -n '^=== prauto heartbeat' "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
  if [[ -n "$header_line" ]]; then
    tail -n "+${header_line}" "$LOG" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g'
  else
    # No header yet (executor died before writing one): use the whole log.
    sed -E 's/\x1b\[[0-9;]*m//g' "$LOG" 2>/dev/null
  fi
}

# classify — one-word outcome from the CURRENT run's final log state.
# ORDER MATTERS: a quota-paused run still ends with "Heartbeat complete", so the
# quota marker must be matched BEFORE the completion marker.
classify() {
  local run
  run=$(run_text)
  if grep -q "quota/session limit\|Quota-pause marker posted" <<< "$run"; then
    echo "quota-paused"; return 0
  fi
  if grep -q "No coding agent available" <<< "$run"; then echo "no-agent"; return 0; fi
  if grep -q "waiting for plan approval" <<< "$run"; then echo "waiting-approval"; return 0; fi
  if grep -q "Heartbeat complete" <<< "$run"; then echo "done"; return 0; fi
  echo "unknown"
}

# --- preflight: the reporting channel must actually resolve ---------------------
# Slack is the only human surface for a detached run, so a monitor that cannot post
# is worse than a monitor that fails: it reports progress into a void while the
# launcher believes reporting is healthy. Resolve the target up front and exit
# nonzero — launch.sh turns an immediately-dead monitor into
# MONITOR_EXITED_IMMEDIATELY (exit 1) with this reason, so the supervisor reports
# degraded reporting instead of a successful launch.
if [[ "${PRAUTO_MONITOR_DRY_RUN:-0}" != "1" ]]; then
  if ! resolve_slack_target; then
    reason="monitor: cannot resolve Slack target '${SLACK_TARGET}' under HERMES_HOME=${HERMES_HOME} — a detached run would have no reporting. Set PRAUTO_SCHEDULER_HERMES_HOME in config.local.env to the profile home that owns the target (see config.local.env.example)."
    printf '%s\n' "$reason" >&2
    printf '%s\n' "$reason" > "$UNRESOLVED_MARKER"
    exit 2
  fi
  # Preflight passed — delete an earlier run's breadcrumb so it cannot outlive the fix.
  # A dry run never reaches this line: it verifies nothing, so it must leave the marker
  # alone rather than report a clean state it never established.
  rm -f "$UNRESOLVED_MARKER"
fi

# --- at most one monitor per checkout -----------------------------------------
if [[ -f "$MONITOR_LOCK" ]]; then
  local_pid=$(cat "$MONITOR_LOCK" 2>/dev/null || true)
  if [[ -n "$local_pid" ]] && is_alive "$local_pid"; then
    exit 0   # another monitor is already reporting this checkout
  fi
  rm -f "$MONITOR_LOCK"
fi
printf '%s' "$$" > "$MONITOR_LOCK"
# Both exit-time behaviours belong in the trap, not at the bottom of the script:
# the "no live executor" early return and a supervisor kill both bypass the tail,
# and those are exactly the runs whose lost reporting would otherwise be silent.
monitor_exit() {
  flush_undelivered
  report_delivery_health
  rm -f "$MONITOR_LOCK"
}
trap monitor_exit EXIT

# --- resolve the executor PID to watch ----------------------------------------
exec_pid="${1:-}"
if [[ -z "$exec_pid" ]] && [[ -f "$EXEC_LOCK" ]]; then
  exec_pid=$(cat "$EXEC_LOCK" 2>/dev/null || true)
fi
if [[ -z "$exec_pid" ]] || ! is_alive "$exec_pid"; then
  say "🤖 prauto(${WORKER}) — monitor attached but no live executor. Final state: $(classify)"
  exit 0
fi

start_epoch=$(date +%s)
last_report=$start_epoch

# latest_phase — the most recent "Dispatching issue #N (phase: X, ...)" line in
# the CURRENT run (scoped by run_text, so a prior run's dispatch can't leak in).
latest_phase() {
  run_text | grep -oE 'Dispatching issue #[0-9]+ \(phase: [a-z-]+[^)]*\)' 2>/dev/null \
    | tail -1 || true
}

# Poll at CHECK_SECS granularity; report to Slack only at INTERVAL_SECS boundaries.
while is_alive "$exec_pid"; do
  sleep "$CHECK_SECS"
  is_alive "$exec_pid" || break
  now=$(date +%s)
  if (( now - last_report >= INTERVAL_SECS )); then
    phase=$(latest_phase)
    elapsed=$(( (now - start_epoch) / 60 ))
    detail="elapsed ${elapsed}m"
    [[ -n "$phase" ]] && detail="${phase} · ${detail}"
    say "⏳ prauto(${WORKER}) still running — ${detail}"
    last_report=$now
  fi
done

# --- executor exited: report the final result once, then exit -----------------
final_state=$(classify)
case "$final_state" in
  done)             summary="✅ task finished successfully" ;;
  no-agent)         summary="⚠️ no coding agent available (auth/quota)" ;;
  waiting-approval) summary="ℹ️ waiting for plan approval (human action)" ;;
  quota-paused)     summary="⏸️ task finished due to quota limit — auto-resumes on the next quota window" ;;
  *)                summary="⚠️ task exited unexpectedly" ;;
esac
tail_text=$(run_text | tail -n 12)
say "🏁 prauto(${WORKER}) cron run — ${summary}
\`\`\`
${tail_text}
\`\`\`"
exit 0
