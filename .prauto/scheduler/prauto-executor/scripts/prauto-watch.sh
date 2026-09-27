#!/usr/bin/env bash
# Progress sampler for a live prauto run: lock-driven, tails the current run's
# log section, tracks the worktree + the executor's busiest descendant process,
# stops when the executor exits.
#   usage: bash prauto-watch.sh [seconds]   (default 540)
#
# Repo resolution: PRAUTO_REPO, else the main checkout this script lives in
# (via the shared git dir, so a copy inside a prauto worktree still resolves to
# the checkout that owns .prauto/state).
if [ -z "${PRAUTO_REPO:-}" ]; then
  common=$(git -C "$(dirname "$0")" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
  PRAUTO_REPO="${common%/.git}"
fi
[ -n "$PRAUTO_REPO" ] && [ -d "$PRAUTO_REPO/.prauto" ] \
  || { echo "cannot locate the checkout — set PRAUTO_REPO"; exit 1; }
REPO="$PRAUTO_REPO"
LOG="$REPO/.prauto/state/heartbeat_cron.log"
LOCK="$REPO/.prauto/state/heartbeat.lock"
# Current run's worktree: explicit PRAUTO_WT, else the most recently touched
# dir under .prauto/worktrees (the executor keeps exactly one live).
WT="${PRAUTO_WT:-$(ls -dt "$REPO"/.prauto/worktrees/*/ 2>/dev/null | head -1)}"
WT="${WT%/}"
DEADLINE=$(( $(date +%s) + ${1:-540} ))

EPID=$(cat "$LOCK" 2>/dev/null)
[ -n "$EPID" ] || { echo "no live lock — executor not running"; exit 1; }
[ -n "$WT" ] || echo "warn: no worktree under $REPO/.prauto/worktrees (pre-Stage-3?)"
echo "[$(date '+%H:%M:%S')] watcher start — executor=$EPID wt=${WT:-none}"
hb_lines=$(wc -l < "$LOG")
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ts=$(date '+%H:%M:%S')
  cpid=$(pgrep -P "$EPID" -f 'claude -p' | head -1)
  if [ -n "$cpid" ]; then
    cinfo=$(ps -o etime=,%cpu=,rss= -p "$cpid" 2>/dev/null | tr -s ' ' | sed 's/^ //')
    cstate="claude=$cpid $cinfo"
  else
    # no coding agent: show the executor's busiest DESCENDANT (verification work)
    desc="$EPID"; frontier="$EPID"
    while [ -n "$frontier" ]; do
      next=$(pgrep -P "$(echo $frontier | tr ' ' ',')" 2>/dev/null | tr '\n' ' ')
      [ -z "$next" ] && break
      desc="$desc $next"; frontier="$next"
    done
    busy=$(ps -o pid=,%cpu=,command= -p "$(echo $desc | tr ' ' ',')" 2>/dev/null \
      | grep -vE 'prauto/lib|heartbeat\.sh' | sort -k2 -rn | head -1 | cut -c1-110 | tr -s ' ')
    cstate="claude: none | busy: ${busy:-idle}"
  fi
  alive=$(kill -0 "$EPID" 2>/dev/null && echo up || echo GONE)
  now=$(wc -l < "$LOG")
  new=""
  if [ "$now" -gt "$hb_lines" ]; then
    new=$(tail -n $(( now - hb_lines )) "$LOG" | sed -E 's/\x1b\[[0-9;]*m//g' | grep -vE '^\s*$' | tr '\n' '|')
    hb_lines=$now
  fi
  wlog=$(git -C "$WT" log --oneline -1 2>/dev/null | cut -c1-64)
  dirty=$(git -C "$WT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  echo "[$ts] exec=$alive $cstate | wt: ${wlog:-?} dirty=$dirty"
  [ -n "$new" ] && echo "[$ts] LOG>>> $new"
  if [ "$alive" = "GONE" ]; then echo "[$ts] executor exited"; break; fi
  sleep 20
done
echo "[$(date '+%H:%M:%S')] watcher end"
