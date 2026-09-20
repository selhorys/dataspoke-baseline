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

# 3. Verify it survived its first seconds; if it died, report why.
sleep 5
if ! kill -0 "$exec_pid" 2>/dev/null; then
  tail_text=$(tail -20 "$EXEC_LOG" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' || true)
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
monitor_pid=$(python3 "$DAEMONIZE" "$MONITOR_LOG" -- bash "$SCRIPT_DIR/monitor.sh" "$exec_pid")
if [[ -z "$monitor_pid" || ! "$monitor_pid" =~ ^[0-9]+$ ]]; then
  echo "MONITOR_FAILED pid=$exec_pid monitor_pid=${monitor_pid:-unknown}"
  tail -5 "$MONITOR_LOG" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' || true
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
  reason=$(grep -m1 -E '^monitor:' "$MONITOR_LOG" 2>/dev/null || true)
  if [[ -n "$reason" ]]; then
    echo "MONITOR_EXITED_IMMEDIATELY pid=$exec_pid monitor_pid=$monitor_pid reason=$reason"
  else
    echo "MONITOR_EXITED_IMMEDIATELY pid=$exec_pid monitor_pid=$monitor_pid"
  fi
  tail -5 "$MONITOR_LOG" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' || true
  exit 1
fi
echo "STARTED pid=$exec_pid monitor_pid=$monitor_pid"
exit 0
