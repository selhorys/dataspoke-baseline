#!/usr/bin/env bash
# Open kubectl port-forwards for DataSpoke TCP services on 127.0.0.1.
#
# In shared ingress mode the cluster's ingress controller exposes only HTTP
# (80/443), so the TCP services (Postgres, Redis, Kafka, dev-lock) are not
# reachable through it. This script forwards them to localhost on the exact
# ports that integration tests and helm-charts/.env.dev expect (the DATASPOKE_DEV_*
# ports), so laptop-side tests and health-check.sh work against 127.0.0.1.
#
# Runs in the foreground and holds all forwards open; Ctrl-C tears them all down.
#
# The forwards are supervised. A forward counts as active only once kubectl
# itself confirms the bind ("Forwarding from 127.0.0.1:<port>"), so a port that
# something else already holds is reported as FAILED instead of counted. A
# forward that later dies or goes stale (its pod was replaced) is logged and
# respawned with backoff. If none comes up the script exits non-zero. Each
# forward writes kubectl's output to pf-<port>.log in a private temp directory
# (named in the banner, kept on exit) or under --log-dir.
#
# Tunables (environment): PORT_FORWARD_POLL_SECS (3), PORT_FORWARD_START_TIMEOUT_SECS (15).
#
# Usage:
#   ./helm-charts/bin/port-forward.sh                   # forward all TCP services
#   ./helm-charts/bin/port-forward.sh --env-file <path> # use a specific env file
#   ./helm-charts/bin/port-forward.sh --log-dir <dir>   # keep per-forward logs in <dir>
#   ./helm-charts/bin/port-forward.sh --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

ENV_FILE_ARG=""
LOG_DIR_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --log-dir)
      [[ -n "${2:-}" ]] || error "--log-dir needs a directory argument (use --help)"
      LOG_DIR_ARG="$2"; shift 2 ;;
    --help|-h) print_usage "$0"; exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

ENV_FILE="${ENV_FILE_ARG:-${ENV_FILE:-$(cd "$SCRIPT_DIR/.." && pwd)/.env.dev}}"

if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy the matching helm-charts/.env.<profile>.example and edit it."
fi
source "$ENV_FILE"

require_tools kubectl

DATASPOKE_KUBE_CLUSTER="${DATASPOKE_KUBE_CLUSTER:-}"
if [[ -z "$DATASPOKE_KUBE_CLUSTER" ]]; then
  error "DATASPOKE_KUBE_CLUSTER must be set in ${ENV_FILE}."
fi
use_context "${DATASPOKE_KUBE_CLUSTER}"

# This script is the TCP surface for shared ingress mode. In managed mode the
# same services are already reachable on the LoadBalancer IP, so the forwards
# only shadow localhost — warn, but proceed (harmless).
if [[ "$(ingress_mode)" != "shared" ]]; then
  warn "Ingress mode is '$(ingress_mode)', not 'shared'. TCP services are already on the LoadBalancer IP; port-forward is normally only needed in shared mode."
fi

DS_NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-}"
if [[ -z "$DS_NS" ]]; then
  error "DATASPOKE_KUBE_DATASPOKE_NAMESPACE must be set in ${ENV_FILE}."
fi
DH_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE:-}"
DD_NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE:-}"

# dev-lock is a dev-only peripheral (install.sh's DEV_ALL list; the prod
# branch never installs it) that happens to live in the same namespace as the
# three core services (DS_NS, set in both profiles), so it can't be told
# apart from them by namespace alone. Key its namespace on the profile the
# env file itself declares (seed_profile) instead: blank under a non-dev env
# file routes it through the same empty-namespace skip as DH_NS/DD_NS below,
# with no change to the PF_SPECS spec format.
DL_NS=""
if [[ "$(seed_profile "$ENV_FILE")" == "dev" ]]; then
  DL_NS="${DS_NS}"
fi

# Each spec: "<local-port>:<namespace>/<service>:<remote-port>".
# Mirrors the Tier-B map in dev-peripherals/nginx-ingress/values.yaml — the
# local ports are the canonical DATASPOKE_DEV_* ports.
PF_SPECS=(
  "9201:${DS_NS}/dataspoke-postgresql:5432"
  "9202:${DS_NS}/dataspoke-redis-master:6379"
  "9221:${DL_NS}/dev-lock:8080"
  "9005:${DH_NS}/datahub-kafka-external:9095"
  "9102:${DD_NS}/example-postgres:5432"
  "9104:${DD_NS}/example-kafka:9094"
)


# --- Supervision ---------------------------------------------------------
#
# A `kubectl port-forward` is not durable: it exits when its pod is replaced or
# its connection drops, and a `svc/` forward can stay up pinned to a pod that no
# longer exists. Each forward therefore gets a small state machine, kept in
# parallel indexed arrays (macOS ships bash 3.2: no associative arrays, no
# `wait -n`, and an empty array must never be expanded bare under `set -u`).
#
#   starting  spawned, waiting for kubectl to confirm the bind
#   up        verified
#   failed    dead or never bound; respawned once PF_NEXT (a $SECONDS value) passes
#
# Every `kill -0`, `grep` and `(( ))` below that may legitimately fail sits in
# an `if` or behind `|| true`: under `set -e` an unguarded failure would abort
# the supervisor the first time a child dies, which is the one event it exists for.

PF_LPORT=()    # local port
PF_NS=()       # namespace
PF_SVC=()      # service
PF_REMOTE=()   # remote port
PF_PID=()      # pid of the current kubectl ("" while failed)
PF_STATE=()    # starting | up | failed
PF_SPAWNED=()  # $SECONDS at the current spawn
PF_NEXT=()     # $SECONDS at which a failed forward may be respawned
PF_FAILS=()    # consecutive failures, drives the backoff
PF_QUIET=()    # 1 once a failure has been reported and not yet recovered from
PF_REASON=()   # sanitized reason for the current failure
PF_ACTIVE=0    # count set by _report_startup
SLEEP_PID=""   # the interruptible sleep in _nap, killed by cleanup

POLL_SECS="${PORT_FORWARD_POLL_SECS:-3}"
START_TIMEOUT_SECS="${PORT_FORWARD_START_TIMEOUT_SECS:-15}"
BACKOFF_MIN_SECS=3
BACKOFF_MAX_SECS=30

# kubectl's own human-readable output. These strings can change between kubectl
# versions; _forward_check has a permissive fallback for a missing readiness line.
PF_BIND_RE='Unable to listen on port|unable to listen on any of the requested ports|address already in use'
# Connection-loss signatures. Deliberately NOT "Handling connection for <port>",
# which kubectl logs for every ordinary client connection.
PF_STALE_RE='lost connection to pod|error forwarding port|an error occurred forwarding'

# A malformed tunable would make the arithmetic below abort mid-supervision.
for _pf_knob in POLL_SECS START_TIMEOUT_SECS; do
  if [[ ! "${!_pf_knob}" =~ ^[1-9][0-9]*$ ]]; then
    # The rejected value is echoed through sanitize_remote_text: it is an
    # arbitrary environment string and may carry control characters.
    error "Invalid PORT_FORWARD_${_pf_knob} '$(sanitize_remote_text "${!_pf_knob}" 40)'. Must be a positive integer number of seconds."
  fi
done
unset _pf_knob

# _ts — wall-clock stamp for the lines that report a change after startup.
_ts() { date '+%H:%M:%S'; }

# _pf_log <i> — the log file of forward <i> (pf-<local-port>.log).
_pf_log() { printf '%s/pf-%s.log' "$LOG_DIR" "${PF_LPORT[$1]}"; }

# _pf_last_line <i> — the last non-empty log line, sanitized (it is kubectl
# output) and bounded to one line; a placeholder when kubectl wrote nothing.
_pf_last_line() {
  local line=""
  line="$(grep -v '^[[:space:]]*$' "$(_pf_log "$1")" 2>/dev/null | tail -n 1 || true)"
  line="$(sanitize_remote_text "$line")"
  printf '%s' "${line:-no output from kubectl}"
}

# _tcp_open <port> — succeed when 127.0.0.1:<port> accepts a connection.
# Loopback, so the connect is answered or refused at once and needs no timeout
# machinery. `exec 3<>` opens the socket and writes NOTHING: these ports front
# Postgres, Redis and Kafka, which log a stray byte as a protocol error (same
# idiom as health-check.sh's _tcp_check).
_tcp_open() { ( exec 3<>/dev/tcp/127.0.0.1/"$1" ) 2>/dev/null; }

# _backoff_secs <consecutive-failures> — 3s, doubling, capped at 30s.
_backoff_secs() {
  local fails="$1" delay="$BACKOFF_MIN_SECS" n=1
  while [[ "$n" -lt "$fails" && "$delay" -lt "$BACKOFF_MAX_SECS" ]]; do
    delay=$(( delay * 2 ))
    n=$(( n + 1 ))
  done
  if [[ "$delay" -gt "$BACKOFF_MAX_SECS" ]]; then delay="$BACKOFF_MAX_SECS"; fi
  printf '%s' "$delay"
}

# _pf_register <local-port> <namespace> <service> <remote-port>
_pf_register() {
  local i="${#PF_LPORT[@]}"
  PF_LPORT[$i]="$1"; PF_NS[$i]="$2"; PF_SVC[$i]="$3"; PF_REMOTE[$i]="$4"
  PF_PID[$i]=""; PF_STATE[$i]="failed"; PF_SPAWNED[$i]=0; PF_NEXT[$i]=0
  PF_FAILS[$i]=0; PF_QUIET[$i]=0; PF_REASON[$i]=""
}

# _spawn_forward <i> — start (or restart) forward <i> with a fresh log.
# The previous log is rotated to .prev so a stale "Forwarding from" line can
# never satisfy the readiness check of the new process, and so growth stays
# bounded at two generations (kubectl logs a line per client connection).
# The subshell sets umask 077 before kubectl opens its log, then exec's it, so
# $! is kubectl's own pid.
_spawn_forward() {
  local i="$1" log pid
  log="$(_pf_log "$i")"
  if [[ -f "$log" ]]; then mv -f "$log" "${log}.prev" 2>/dev/null || true; fi
  ( umask 077
    exec kubectl port-forward -n "${PF_NS[$i]}" "svc/${PF_SVC[$i]}" \
      "${PF_LPORT[$i]}:${PF_REMOTE[$i]}" --address 127.0.0.1 >"$log" 2>&1 ) &
  pid=$!
  PF_PID[$i]="$pid"
  PF_STATE[$i]="starting"
  PF_SPAWNED[$i]="$SECONDS"
}

# _forward_ready <i> — the forward's process is alive AND kubectl itself printed
# its bind confirmation. A TCP connect alone proves nothing: a leftover process
# already holding the port accepts it while our kubectl has exited with
# "address already in use". The trailing space keeps :9201 from matching :92010.
_forward_ready() {
  local i="$1"
  kill -0 "${PF_PID[$i]:-0}" 2>/dev/null || return 1
  grep -Fq "Forwarding from 127.0.0.1:${PF_LPORT[$i]} " "$(_pf_log "$i")" 2>/dev/null
}

# _forward_fail <i> <reason> — record the failure and schedule the respawn.
# The pid is forgotten: cleanup must not signal a pid the OS may have reused.
_forward_fail() {
  local i="$1" delay
  PF_FAILS[$i]=$(( ${PF_FAILS[$i]} + 1 ))
  delay="$(_backoff_secs "${PF_FAILS[$i]}")"
  PF_STATE[$i]="failed"
  PF_PID[$i]=""
  PF_REASON[$i]="$2"
  PF_NEXT[$i]=$(( SECONDS + delay ))
}

# _forward_kill <i> — stop forward <i>'s kubectl and reap it. stderr silenced:
# bash prints a "Terminated" notice when it reaps a signalled background job.
_forward_kill() {
  local pid="${PF_PID[$1]:-}"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}

# _forward_check <i> — advance a starting/up forward's state from what the
# process and its log show now. Prints nothing; callers decide what to report.
_forward_check() {
  local i="$1" pid log line
  pid="${PF_PID[$i]:-}"
  log="$(_pf_log "$i")"

  # Dead: the common case on current kubectl ("lost connection to pod" exits).
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
    _forward_fail "$i" "kubectl exited: $(_pf_last_line "$i")"
    return 0
  fi

  # Stale: alive, but the log says connections are failing (older kubectl, or a
  # svc/ forward pinned to a replaced pod). Kill it so it is respawned.
  if grep -Eq "$PF_STALE_RE" "$log" 2>/dev/null; then
    line="$(grep -E "$PF_STALE_RE" "$log" 2>/dev/null | tail -n 1 || true)"
    line="$(sanitize_remote_text "$line")"
    _forward_kill "$i"
    _forward_fail "$i" "stale forward: ${line}"
    return 0
  fi

  if [[ "${PF_STATE[$i]}" == "up" ]]; then return 0; fi

  # starting
  if _forward_ready "$i"; then
    PF_STATE[$i]="up"
    PF_FAILS[$i]=0
    return 0
  fi
  if grep -Eiq "$PF_BIND_RE" "$log" 2>/dev/null; then
    line="$(_pf_last_line "$i")"
    _forward_kill "$i"
    _forward_fail "$i" "$line"
    return 0
  fi
  if [[ $(( SECONDS - ${PF_SPAWNED[$i]} )) -ge "$START_TIMEOUT_SECS" ]]; then
    # No readiness line within the budget and no bind error in the log. A kubectl
    # that words its output differently would otherwise be flagged failed while
    # working, so accept a live process whose port answers. Applies to every
    # spawn, respawns included, so such a kubectl does not loop through backoff.
    if _tcp_open "${PF_LPORT[$i]}"; then
      PF_STATE[$i]="up"
      PF_FAILS[$i]=0
    else
      line="$(_pf_last_line "$i")"
      _forward_kill "$i"
      _forward_fail "$i" "not ready after ${START_TIMEOUT_SECS}s: ${line}"
    fi
  fi
  return 0
}

# _wait_for_startup — poll until no forward is still `starting`. Always ends:
# _forward_check resolves a forward to up or failed once its start budget is spent.
_wait_for_startup() {
  local i pending
  while :; do
    pending=0
    i=0
    while [[ "$i" -lt "${#PF_LPORT[@]}" ]]; do
      if [[ "${PF_STATE[$i]}" == "starting" ]]; then
        _forward_check "$i"
        if [[ "${PF_STATE[$i]}" == "starting" ]]; then pending=1; fi
      fi
      i=$(( i + 1 ))
    done
    if [[ "$pending" -eq 0 ]]; then break; fi
    sleep 0.2
  done
}

# _report_startup — one line per forward, then "N of M port-forward(s) active".
# Sets PF_ACTIVE. A failed forward is marked quiet: it has been reported, so the
# supervisor retries it silently instead of repeating the line every poll.
_report_startup() {
  local i total="${#PF_LPORT[@]}"
  PF_ACTIVE=0
  i=0
  while [[ "$i" -lt "$total" ]]; do
    if [[ "${PF_STATE[$i]}" == "up" ]]; then
      PF_ACTIVE=$(( PF_ACTIVE + 1 ))
      info "  127.0.0.1:${PF_LPORT[$i]} -> ${PF_NS[$i]}/${PF_SVC[$i]}:${PF_REMOTE[$i]} active (pid ${PF_PID[$i]})"
    else
      PF_QUIET[$i]=1
      warn "  127.0.0.1:${PF_LPORT[$i]} -> ${PF_NS[$i]}/${PF_SVC[$i]} FAILED: ${PF_REASON[$i]} (log: $(_pf_log "$i"))"
    fi
    i=$(( i + 1 ))
  done
  echo ""
  if [[ "$PF_ACTIVE" -eq "$total" ]]; then
    info "${PF_ACTIVE} of ${total} port-forward(s) active."
  else
    warn "${PF_ACTIVE} of ${total} port-forward(s) active."
  fi
}

# _forward_poll <i> — one supervision step for forward <i>: respawn a failed
# forward once its backoff has passed, otherwise re-check it and report a change.
# A failure is logged once; retries stay silent until a recovery logs one line.
_forward_poll() {
  local i="$1" prev delay
  prev="${PF_STATE[$i]}"

  if [[ "$prev" == "failed" ]]; then
    if [[ "$SECONDS" -ge "${PF_NEXT[$i]}" ]]; then _spawn_forward "$i"; fi
    return 0
  fi

  _forward_check "$i"

  if [[ "${PF_STATE[$i]}" == "failed" ]]; then
    if [[ "${PF_QUIET[$i]}" -eq 0 ]]; then
      PF_QUIET[$i]=1
      delay=$(( ${PF_NEXT[$i]} - SECONDS ))
      warn "$(_ts) 127.0.0.1:${PF_LPORT[$i]} -> ${PF_NS[$i]}/${PF_SVC[$i]} LOST: ${PF_REASON[$i]}; respawning in ${delay}s, further retries are silent until it recovers (log: $(_pf_log "$i"))"
    fi
  elif [[ "${PF_STATE[$i]}" == "up" && "$prev" == "starting" && "${PF_QUIET[$i]}" -eq 1 ]]; then
    PF_QUIET[$i]=0
    info "$(_ts) 127.0.0.1:${PF_LPORT[$i]} -> ${PF_NS[$i]}/${PF_SVC[$i]} recovered (pid ${PF_PID[$i]})"
  fi
  return 0
}

# _nap <secs> — sleep as a background job and `wait` on it. A trapped signal
# interrupts `wait` at once; a foreground `sleep` would delay the trap until it
# finished. cleanup kills SLEEP_PID so an interrupted nap does not outlive us.
_nap() {
  sleep "$1" &
  SLEEP_PID=$!
  wait "$SLEEP_PID" 2>/dev/null || true
  SLEEP_PID=""
}

# _supervise — poll every forward every POLL_SECS until signalled.
_supervise() {
  local i
  while :; do
    i=0
    while [[ "$i" -lt "${#PF_LPORT[@]}" ]]; do
      _forward_poll "$i"
      i=$(( i + 1 ))
    done
    _nap "$POLL_SECS"
  done
}

# cleanup — kill the forwards running NOW (respawns replace pids, so a
# spawn-time list would miss them) and the pending nap. Children of a
# non-interactive shell ignore SIGINT, so the explicit kill is what stops them.
# Waits per pid, never bare `wait`, which would also block on anything else we
# started. Logs are kept for post-mortem; only an empty mktemp dir is removed.
cleanup() {
  local pid i=0
  trap - EXIT
  if [[ -n "$SLEEP_PID" ]]; then kill "$SLEEP_PID" 2>/dev/null || true; fi
  while [[ "$i" -lt "${#PF_PID[@]}" ]]; do
    pid="${PF_PID[$i]}"
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    i=$(( i + 1 ))
  done
  i=0
  while [[ "$i" -lt "${#PF_PID[@]}" ]]; do
    pid="${PF_PID[$i]}"
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
    i=$(( i + 1 ))
  done
  if [[ -n "$LOG_DIR" ]]; then
    if [[ "$LOG_DIR_OWNED" -eq 1 ]] && rmdir "$LOG_DIR" 2>/dev/null; then
      :
    else
      info "Port-forward logs kept in ${LOG_DIR}"
    fi
  fi
}

# Log directory: a fresh 0700 `mktemp -d` (no predictable /tmp path to
# pre-create as a symlink), or the caller's --log-dir. Built from $TMPDIR
# explicitly for the reason use_context documents (macOS `mktemp -t` ignores it).
LOG_DIR=""
LOG_DIR_OWNED=0
if [[ -n "$LOG_DIR_ARG" ]]; then
  mkdir -p "$LOG_DIR_ARG" 2>/dev/null && [[ -w "$LOG_DIR_ARG" ]] \
    || error "Cannot create or write the log directory ${LOG_DIR_ARG}."
  LOG_DIR="$(cd "$LOG_DIR_ARG" && pwd)"
else
  LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dataspoke-port-forward.XXXXXX")" \
    || error "Could not create a log directory under ${TMPDIR:-/tmp}."
  LOG_DIR_OWNED=1
fi

# Signals: the loop below never ends on its own, so INT/TERM must be turned into
# an exit (which runs the EXIT trap); a trap that merely returned would resume
# the loop. 130/143 are the conventional 128+signal statuses.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo ""
echo "DataSpoke port-forward"
echo "======================"
# Named before the first forward, matching health-check.sh's convention: this
# script binds fixed localhost ports that the dev stack and the integration
# suites treat as canonical, so an operator who meant one deployment and
# resolved another sees it here rather than reading its forwards as their own.
echo "  Env file:  ${ENV_FILE}"
echo "  Cluster:   ${DATASPOKE_KUBE_CLUSTER}"
echo "  Namespace: ${DS_NS}"
echo "  Logs:      ${LOG_DIR}"
echo ""
info "Opening port-forwards on 127.0.0.1 (Ctrl-C to stop)..."
echo ""
for spec in "${PF_SPECS[@]}"; do
  local_port="${spec%%:*}"
  rest="${spec#*:}"
  ns="${rest%%/*}"
  svc_remote="${rest#*/}"
  svc="${svc_remote%%:*}"
  remote="${svc_remote##*:}"

  if [[ -z "$ns" ]]; then
    warn "  skip 127.0.0.1:${local_port} — no namespace resolved for ${svc} in this env file (either a dev-only peripheral this profile doesn't install, or the namespace variable is present but left blank)"
    continue
  fi

  if ! kubectl get "svc/${svc}" -n "${ns}" >/dev/null 2>&1; then
    warn "  skip 127.0.0.1:${local_port} — service ${ns}/${svc} not found (not installed?)"
    continue
  fi
  _pf_register "$local_port" "$ns" "$svc" "$remote"
  _spawn_forward $(( ${#PF_LPORT[@]} - 1 ))
done

# Skipped specs are not registered, so this still means "nothing to forward".
if [[ ${#PF_LPORT[@]} -eq 0 ]]; then
  error "No services found to forward. Check ${ENV_FILE} — DATASPOKE_KUBE_DATASPOKE_NAMESPACE and any DATASPOKE_DEV_KUBE_*_NAMESPACE vars it declares must name namespaces that actually exist on the cluster."
fi

# Wait for kubectl to confirm each bind (or fail) before reporting anything as active.
_wait_for_startup
_report_startup

# Nothing bound: holding open would only look like a working forward.
if [[ "$PF_ACTIVE" -eq 0 ]]; then
  error "No port-forward came up (see the FAILED lines above). Per-forward logs are in ${LOG_DIR}."
fi

info "Leave this running while you test; failed or lost forwards are retried automatically."
# The api_wired suite truncates data (tests/integration/util/__main__.py
# --reset-all) — only point an operator at it against a dev env file.
if [[ "$(seed_profile "$ENV_FILE")" == "dev" ]]; then
  info "Run integration tests in another shell:"
  echo "  set -a && source ${ENV_FILE} && set +a && uv run pytest tests/integration/api_wired/ -v"
fi
echo ""
_supervise
