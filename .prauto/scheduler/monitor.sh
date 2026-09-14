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
# Slack credentials resolve from the Hermes profile; inherit the supervisor's
# HERMES_HOME, falling back to the default home (which also carries the bot token).
: "${HERMES_HOME:=$HOME/.hermes}"
export HERMES_HOME

if [[ -f "$PRAUTO_DIR/config.env" ]]; then
  # shellcheck source=../config.env
  source "$PRAUTO_DIR/config.env"
fi
if [[ -f "$PRAUTO_DIR/config.local.env" ]]; then
  # shellcheck source=../config.local.env
  source "$PRAUTO_DIR/config.local.env"
fi

SLACK_TARGET="${PRAUTO_SLACK_TARGET:-slack}"
CHECK_SECS="${PRAUTO_MONITOR_CHECK_SECS:-60}"
INTERVAL_SECS="${PRAUTO_MONITOR_INTERVAL_SECS:-600}"
WORKER="${PRAUTO_WORKER_ID:-prauto01}"

# scrub <text> — redact credentials before anything leaves this machine. The
# executor log can contain the worker's GH_TOKEN / ANTHROPIC_API_KEY (and, in a
# failing test, DATASPOKE_DEV_* / JWT / dsk_ tokens), so every Slack message is
# scrubbed at the single `say` choke point.
scrub() {
  local text="$1"
  text=$(printf '%s' "$text" | sed -E \
    -e 's#://[^:@/[:space:]]+:[^@/[:space:]]+@#://***REDACTED***@#g' \
    -e 's/(gho|ghp|ghs|ghu|github_pat)_[A-Za-z0-9]+/***REDACTED***/g' \
    -e 's/sk-[A-Za-z0-9]+/***REDACTED***/g' \
    -e 's/dsk_[A-Za-z0-9_-]+/***REDACTED***/g' \
    -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/***REDACTED***/g')
  [[ -n "${GH_TOKEN:-}" ]] && text="${text//${GH_TOKEN}/***REDACTED***}"
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && text="${text//${ANTHROPIC_API_KEY}/***REDACTED***}"
  printf '%s' "$text"
}

# say <message> — scrub, then post to the Slack target; print on dry-run.
say() {
  local msg
  msg=$(scrub "$1")
  if [[ "${PRAUTO_MONITOR_DRY_RUN:-0}" == "1" ]]; then
    printf '[dry-run -> %s]\n%s\n' "$SLACK_TARGET" "$msg"
    return 0
  fi
  if ! hermes send --to "$SLACK_TARGET" "$msg" >/dev/null 2>&1; then
    printf 'monitor: slack send failed\n' >&2
    return 1
  fi
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

# --- at most one monitor per checkout -----------------------------------------
if [[ -f "$MONITOR_LOCK" ]]; then
  local_pid=$(cat "$MONITOR_LOCK" 2>/dev/null || true)
  if [[ -n "$local_pid" ]] && is_alive "$local_pid"; then
    exit 0   # another monitor is already reporting this checkout
  fi
  rm -f "$MONITOR_LOCK"
fi
printf '%s' "$$" > "$MONITOR_LOCK"
trap 'rm -f "$MONITOR_LOCK"' EXIT

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
