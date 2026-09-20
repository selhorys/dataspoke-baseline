#!/usr/bin/env bash
# prauto launch — deterministic detach+verify for one supervisor tick (Hermes binding).
#
# The supervisor (cron agent) runs this ONE script and relays its one-line
# status to Slack. It owns the mechanical envelope: check the executor lock,
# detach the executor when idle, verify it survived, then detach the monitor.
# The executor and monitor run in their own sessions (see daemonize.py), so
# they survive the supervisor turn's process-group teardown.
#
# Exit codes: 0 = launched/already-running/exited-with-report; 1 = launch failed
# (executor failed to detach or died immediately, OR the monitor failed to detach or died
# immediately — a MONITOR_* failure means the executor is running but Slack reporting is not).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRAUTO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$PRAUTO_DIR/state"
EXEC_LOCK="$STATE_DIR/heartbeat.lock"
MONITOR_LOCK="$STATE_DIR/monitor.lock"
EXEC_LOG="$STATE_DIR/heartbeat_cron.log"
MONITOR_LOG="$STATE_DIR/monitor.log"
DAEMONIZE="$SCRIPT_DIR/daemonize.py"

# GUI/cron-launched agents often inherit a minimal PATH; make the CLIs the
# executor needs (gh, git, jq, claude, codex) resolvable regardless.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

mkdir -p "$STATE_DIR"

# 1. Already running? The executor's PID lock is the concurrency gate.
if [[ -f "$EXEC_LOCK" ]]; then
  p=$(cat "$EXEC_LOCK" 2>/dev/null || true)
  if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then
    echo "ALREADY_RUNNING pid=$p"
    exit 0
  fi
  rm -f "$EXEC_LOCK"
fi

# 2. Detach the executor (own session; survives this turn's teardown).
exec_pid=$(python3 "$DAEMONIZE" "$EXEC_LOG" -- bash "$PRAUTO_DIR/heartbeat.sh")
if [[ -z "$exec_pid" || ! "$exec_pid" =~ ^[0-9]+$ ]]; then
  echo "LAUNCH_FAILED daemonize_returned=$exec_pid"
  exit 1
fi

# 3. Verify it survived its first seconds.
#
# A wake with nothing to do finishes in well under this window — no claimed
# issue is pending, so the executor logs "Heartbeat complete." and exits 0. That
# is the single most common outcome, and reporting it as EXITED_IMMEDIATELY
# trains an operator to ignore the one status line that would matter if the
# executor really had died on startup. Distinguish the two by what the executor
# itself logged, not by how quickly it was gone.
sleep 5
if ! kill -0 "$exec_pid" 2>/dev/null; then
  tail_text=$(tail -20 "$EXEC_LOG" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' || true)
  if grep -q 'Heartbeat complete\.' <<< "$tail_text"; then
    echo "COMPLETED_NO_WORK pid=$exec_pid"
    exit 0
  fi
  echo "EXITED_IMMEDIATELY pid=$exec_pid"
  printf '%s\n' "$tail_text"
  exit 0
fi

# 4. Detach the monitor (it re-reads the executor PID from the lock, but we
#    pass it explicitly to avoid a startup race).
if [[ -f "$MONITOR_LOCK" ]]; then
  mp=$(cat "$MONITOR_LOCK" 2>/dev/null || true)
  if [[ -n "$mp" ]] && kill -0 "$mp" 2>/dev/null; then
    echo "STARTED pid=$exec_pid monitor_already_running=$mp"
    exit 0
  fi
fi
# daemonize.py opens the log O_APPEND, so it accumulates across runs. Remember where this
# monitor's output begins and read only from there — an earlier run's `monitor:` line must
# never be reported as this run's failure reason.
# Bound the log and mark where this run begins. daemonize.py opens it O_APPEND,
# so it otherwise grows forever and reads as one undated stream: an operator
# opening the file sees a months-old `monitor: slack send failed` and concludes
# reporting is broken right now. The banner makes each run's lines attributable;
# the trim keeps the file to its most recent runs.
MONITOR_LOG_MAX_BYTES="${PRAUTO_MONITOR_LOG_MAX_BYTES:-262144}"
if [[ -f "$MONITOR_LOG" ]]; then
  monitor_log_bytes=$(wc -c < "$MONITOR_LOG" 2>/dev/null || printf 0)
  monitor_log_bytes=${monitor_log_bytes//[[:space:]]/}
  if [[ "${monitor_log_bytes:-0}" -gt "$MONITOR_LOG_MAX_BYTES" ]]; then
    tail -c "$(( MONITOR_LOG_MAX_BYTES / 2 ))" "$MONITOR_LOG" > "${MONITOR_LOG}.trim" 2>/dev/null \
      && mv -f "${MONITOR_LOG}.trim" "$MONITOR_LOG" 2>/dev/null || rm -f "${MONITOR_LOG}.trim"
  fi
fi
printf -- '--- monitor run %s (executor pid %s) ---\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$exec_pid" >> "$MONITOR_LOG" 2>/dev/null || true

monitor_log_start=0
[[ -f "$MONITOR_LOG" ]] && monitor_log_start=$(wc -c < "$MONITOR_LOG") && monitor_log_start=${monitor_log_start//[[:space:]]/}
monitor_log_new() { tail -c +$(( ${monitor_log_start:-0} + 1 )) "$MONITOR_LOG" 2>/dev/null || true; }
monitor_pid=$(python3 "$DAEMONIZE" "$MONITOR_LOG" -- bash "$SCRIPT_DIR/monitor.sh" "$exec_pid")
if [[ -z "$monitor_pid" || ! "$monitor_pid" =~ ^[0-9]+$ ]]; then
  echo "MONITOR_FAILED pid=$exec_pid monitor_pid=${monitor_pid:-unknown}"
  monitor_log_new | tail -5 | sed -E 's/\x1b\[[0-9;]*m//g' || true
  exit 1
fi
# 5. Verify the monitor survived its first seconds too. daemonize.py prints a numeric PID even
#    when the command it is asked to exec is missing or dies immediately, so a non-numeric PID
#    (caught above) is not the only failure: a monitor that detaches but exits right away must not
#    be reported as a successful launch, or the run silently loses all Slack progress/final
#    reporting while the supervisor is told it succeeded.
sleep 3
if ! kill -0 "$monitor_pid" 2>/dev/null; then
  # Surface WHY the monitor died. The supervisor relays this status line to Slack and
  # cannot read the monitor log, so a bare MONITOR_EXITED_IMMEDIATELY hides the one
  # thing worth acting on — e.g. an unresolved Slack target, which is what a monitor
  # run under the wrong HERMES_HOME reports (see monitor.sh's preflight).
  reason=$(monitor_log_new | grep -m1 -E '^monitor:' || true)
  if [[ -n "$reason" ]]; then
    echo "MONITOR_EXITED_IMMEDIATELY pid=$exec_pid monitor_pid=$monitor_pid reason=$reason"
  else
    echo "MONITOR_EXITED_IMMEDIATELY pid=$exec_pid monitor_pid=$monitor_pid"
  fi
  monitor_log_new | tail -5 | sed -E 's/\x1b\[[0-9;]*m//g' || true
  exit 1
fi
echo "STARTED pid=$exec_pid monitor_pid=$monitor_pid"
exit 0
