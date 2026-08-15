#!/usr/bin/env bash
# Health check for DataSpoke services used by integration tests.
#
# Verifies each ingress-exposed service is reachable AND responding at the
# application layer — not just that the TCP port is open.
#
# Usage:
#   ./helm-charts/bin/health-check.sh                          # Check all; prompt to release held lock
#   ./helm-charts/bin/health-check.sh --profile {dev|prod}     # Probe that profile's deployment
#   ./helm-charts/bin/health-check.sh --keep-lock              # Don't touch an existing lock
#   ./helm-charts/bin/health-check.sh --force-release          # Release held lock without prompting
#   ./helm-charts/bin/health-check.sh --env-file <path>        # Use a specific env file
#   ./helm-charts/bin/health-check.sh --help                   # Print this usage message
#
# --profile resolves helm-charts/.env.<profile> the way install.sh does, with
# --env-file overriding it. The run is pinned to the kube context that file
# names (DATASPOKE_KUBE_CLUSTER) rather than reading the operator's ambient
# one, and the env file, the context and the ingress domain are all printed
# before the first probe: a confident verdict read off the wrong deployment is
# the failure this reporting exists to prevent.
#
# Every service is judged at the application layer — no service is reported
# healthy on an open TCP port alone. The event-consumer and langfuse-worker
# serve no HTTP surface and are judged on their Deployment's ready-replica
# count instead, which is read whether or not the ingress is up.
#
# Wall clock is a contract here, not an incidental: this script runs inside a
# blocking PreToolUse hook (.claude/hooks/preflight-integration-tests.sh) that
# is killed at its own timeout, and a killed hook fails OPEN. So every probe
# carries a bound, and each kind carries its own: TCP connects by
# TCP_CONNECT_TIMEOUT_SECS, HTTP by curl's --max-time, control-plane reads by
# --request-timeout, and the redis-cli and `uv run python` probes — neither of
# which has an option that bounds a peer which accepts the connection and then
# never speaks, the ordinary shape of a stale `kubectl port-forward` — by
# _bounded's background-and-kill. The two probes that would otherwise repeat
# against a dead endpoint (the ingress connect, the kubectl dial) are answered
# once and memoized. The unsupervised consumer (.prauto's dev_env_healthy) adds
# an outer deadline of its own as a backstop.
#
# The DataHub, dummy-data and dev-lock sections have no prod counterpart — they
# are dev-only peripherals — so they report unreachable against a prod
# deployment rather than being skipped.
#
# Exit codes:
#   0  every probe passed
#   1  probes ran and one or more services are unhealthy
#   2  the run could not be SET UP, so nothing was probed: an invalid
#      invocation, a missing/unreadable/unloadable env file, a missing kubectl
#      or curl (the two every section depends on; redis-cli, uv, pg_isready and
#      jq are optional and their probes skip by name when absent), an
#      unset DATASPOKE_KUBE_CLUSTER, an unresolvable
#      context or unreadable kubeconfig, or missing ingress coordinates. A 2 is
#      a fault on this machine and is NOT evidence about the deployment —
#      consumers that heal a cluster when this check fails (.prauto's
#      dev_env_healthy) must act on 1 only.
#
# EVERY abort before the first probe reports 2 — that is the contract, and it
# is why the region carries its own guards rather than leaning on `set -e`,
# which would end those runs on bash's status 1 and make them indistinguishable
# from the unhealthy verdict.
set -euo pipefail

# These run before error() exists, so they carry the exit-2 contract
# themselves: under `set -e` a bare failure here would end the run on bash's
# own status 1 — the "deployment is unhealthy" verdict — for what is a broken
# checkout.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 2

_HELPERS="$SCRIPT_DIR/lib/helpers.sh"

# Readability is asserted BEFORE the source, because `source` cannot report an
# unopenable file through an `||` handler: measured on bash 3.2.57 (stock
# macOS, what `#!/usr/bin/env bash` resolves to), both a missing helpers.sh and
# one at mode 000 make `source … || { …; exit 2; }` end the run on bash's own
# status 1 with the handler never reaching the terminal. That 1 is the
# "deployment is unhealthy" verdict, and .prauto's dev_env_healthy answers it
# by launching an unsupervised `install.sh --profile dev` — a broken checkout
# would provision a cluster. So the two openability faults are decided here,
# and the source below runs with errexit lifted so a failing command INSIDE
# helpers.sh is inspected rather than killing the run at status 1 too.
if [[ ! -e "$_HELPERS" ]]; then
  printf '[ERROR] Missing %s — the helper library this script needs; check out helm-charts/bin/lib/.\n' \
    "$_HELPERS" >&2
  exit 2
fi
if [[ ! -r "$_HELPERS" ]]; then
  printf '[ERROR] %s is not readable by %s — check its permissions.\n' \
    "$_HELPERS" "$(id -un 2>/dev/null || echo "this user")" >&2
  exit 2
fi
# `+eu`, not `+e`: an unbound reference at the helpers' top level is fatal to a
# non-interactive shell whether or not errexit is set, and would end the run on
# bash's own status 1 — the "deployment is unhealthy" verdict — for what is a
# broken checkout. Matches the env-file source below.
set +eu
# shellcheck source=lib/helpers.sh
source "$_HELPERS"
_helpers_rc=$?
set -eu
if (( _helpers_rc != 0 )); then
  printf '[ERROR] Could not load %s (exited %s) — check that it is valid shell.\n' \
    "$_HELPERS" "$_helpers_rc" >&2
  exit 2
fi
# Belt-and-braces: a file that loaded with status 0 but defines none of what is
# called below (truncated, or replaced by something else entirely) must still be
# a setup fault, not a "command not found" cascade through the probes. The list
# is every helper the SETUP region calls, not just the well-known ones: a
# helpers.sh truncated after sanitize_remote_text would otherwise pass this
# assertion and then die at 127 under `set -e` — bash's own status, not 2.
for _fn in error info warn require_tools use_context sanitize_remote_text \
           ingress_mode ingress_scheme datahub_gms_host; do
  if ! declare -F "$_fn" >/dev/null 2>&1; then
    printf '[ERROR] %s loaded but does not define %s() — the helper library is incomplete.\n' \
      "$_HELPERS" "$_fn" >&2
    exit 2
  fi
done
unset _fn

# Everything from here to the first probe is setup, not measurement, so every
# abort in that region reports 2 (see the exit-code table above). error()
# reads this variable and defaults to 1, which is what puts the abort sites
# that live inside the helpers — require_tools, all three use_context failure
# paths, ingress_scheme, datahub_gms_host — on 2 without duplicating their
# validation here. Deliberately NOT exported: no child process inherits it,
# and it is unset immediately before the first probe so any later error() is a
# plain exit 1 again.
DATASPOKE_ERROR_EXIT_CODE=2

HELM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)" \
  || error "Could not resolve the helm-charts directory from ${SCRIPT_DIR}."

# _require_option_value <flag> <remaining_argc> asserts a value-taking flag
# actually got one before the shift, matching the pre-flight's own guard: a
# flag given last (e.g. an empty CI variable expanding `--profile` to
# nothing) would otherwise make `shift 2` fail under `set -e` and end the run
# at exit 1 with no output. <remaining_argc> is the CALLER's own `$#`, taken
# as a parameter rather than read here — this function's own argument count
# is always 2.
_require_option_value() {
  (( $2 >= 2 )) || error "$1 requires a value (use --help)."
}

KEEP_LOCK=false
FORCE_RELEASE=false
ENV_FILE_ARG=""
ENV_FILE_GIVEN=false
PROFILE=""
# Whether --profile was GIVEN, not whether its value is non-empty: --profile
# '' must be reported as an invalid profile rather than silently falling
# through to the dev default, which is what a bare non-emptiness check on
# PROFILE would do.
PROFILE_GIVEN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-lock) KEEP_LOCK=true; shift ;;
    --force-release) FORCE_RELEASE=true; shift ;;
    --env-file) _require_option_value "$1" $#; ENV_FILE_ARG="$2"; ENV_FILE_GIVEN=true; shift 2 ;;
    --profile) _require_option_value "$1" $#; PROFILE="$2"; PROFILE_GIVEN=true; shift 2 ;;
    --help|-h)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0
      ;;
    *)
      error "Unknown option: $1 (use --help)"
      ;;
  esac
done

if $PROFILE_GIVEN && [[ "$PROFILE" != "dev" && "$PROFILE" != "prod" ]]; then
  error "Invalid --profile '${PROFILE}'. Must be 'dev' or 'prod'."
fi

# Whether --env-file was GIVEN, not whether its value is non-empty — the same
# distinction PROFILE_GIVEN draws one line up. An empty CI variable expanding
# `--env-file "$F"` to an empty path would otherwise fall through to the dev
# default, and the runbooks present --env-file as the equivalent of --profile,
# so that silence hands a prod operator a dev verdict.
if $ENV_FILE_GIVEN && [[ -z "$ENV_FILE_ARG" ]]; then
  error "--env-file requires a non-empty path."
fi

# Explicit --env-file wins, then --profile's own file, then the inherited
# ENV_FILE an install exports for its child scripts, then the dev default. The
# first two mirror install.sh; the last two are what keeps a bare invocation
# behaving as it always has.
if [[ -n "$ENV_FILE_ARG" ]]; then
  ENV_FILE="$ENV_FILE_ARG"
elif $PROFILE_GIVEN; then
  ENV_FILE="${HELM_DIR}/.env.${PROFILE}"
else
  ENV_FILE="${ENV_FILE:-${HELM_DIR}/.env.dev}"
fi

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy the matching helm-charts/.env.<profile>.example and edit it."
fi
# Readability is checked separately from existence: these files are written
# mode 600, so one authored under another uid exists and cannot be read, and
# `-f` alone would wave it through to the source below.
if [[ ! -r "$ENV_FILE" ]]; then
  error "Env file at $ENV_FILE is not readable by $(id -un 2>/dev/null || echo "this user") — check its permissions."
fi
# errexit AND nounset are both lifted around the source, not just an `||`
# appended to it. Under `set -e` a failing command INSIDE a sourced file ends
# the whole run with bash's own status 1 — the "probes ran and something is
# unhealthy" verdict, for a run that probed nothing — and `source … || …` does
# not suppress that (measured on bash 3.2: the shell exits at the failing line,
# before the `||` branch is ever considered). Lifting errexit for the duration
# is what lets the status be inspected here instead.
#
# `set -u` has to go with it, and for a stricter reason: an unbound-variable
# reference is fatal to a non-interactive shell REGARDLESS of errexit, so an
# operator env file containing `FOO="${SOMETHING_NOT_SET}"` — an ordinary typo
# in a hand-edited file — would end the run at status 1 with _env_file_rc never
# captured and the error() below never reached (measured on bash 3.2.57). The
# env file is operator data, so an unset reference in it expands empty and the
# required-variable checks further down decide what is actually missing.
set +eu
# shellcheck disable=SC1090  # path is resolved at runtime, by design
source "$ENV_FILE"
_env_file_rc=$?
set -eu
if (( _env_file_rc != 0 )); then
  # Re-stated: the file may have assigned to the override before failing.
  DATASPOKE_ERROR_EXIT_CODE=2
  error "Could not load ${ENV_FILE} — check that it is valid shell and that every line in it succeeds."
fi
# The env file is operator-authored data, not part of this script's exit
# contract. Re-assert the pre-probe code so a DATASPOKE_ERROR_EXIT_CODE set
# inside it cannot rewrite what a later abort means to the consumers that
# branch on it (.prauto's dev_env_healthy, the preflight hook).
# shellcheck disable=SC2034  # read by error() in lib/helpers.sh
DATASPOKE_ERROR_EXIT_CODE=2

# ---------------------------------------------------------------------------
# Pin the cluster this run reads
# ---------------------------------------------------------------------------
# Four of the checks below issue kubectl reads. Without this pin they resolve
# whatever context is current in the ambient kubeconfig while the banner names
# the env file — so a verdict can be assembled from two different deployments
# and read as one. Ordered exactly as port-forward.sh does it, and through the
# same use_context helper: it copies the kubeconfig to a private mode-600
# temporary file, exports KUBECONFIG to that copy and removes it on exit, so
# the caller's own current-context is left untouched.
#
# kubectl and curl are gated HERE, because every section depends on them and a
# binary that exits 127 would make probes produce individually plausible
# failure lines — "/health did not return 2xx", "PING did not return PONG" —
# with nothing pointing at the real cause. That report exits 1, and .prauto
# answers a 1 by provisioning a cluster.
#
# Only the two every section depends on are hard requirements. redis-cli and uv
# back a single probe each, so their absence is reported by that probe as a
# skip naming the missing binary: a tool missing from this workstation is a
# local condition, not a verdict on the deployment. Failing would exit 1 and
# have .prauto provision a cluster over it; aborting would deny a prod operator
# the `DataSpoke Infra` section the runbook tells them to read. A skip states
# exactly what was not measured and leaves the other verdicts intact.
#
# pg_isready and jq are deliberately NOT here: each has a working fallback in
# the code below (a uv/asyncpg connect, a backslash-escaping JSON encoder), so
# their absence changes nothing an operator needs to fix.
require_tools kubectl curl

DATASPOKE_KUBE_CLUSTER="${DATASPOKE_KUBE_CLUSTER:-}"
if [[ -z "$DATASPOKE_KUBE_CLUSTER" ]]; then
  error "DATASPOKE_KUBE_CLUSTER must be set in ${ENV_FILE}."
fi
use_context "${DATASPOKE_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Ingress coordinates
# ---------------------------------------------------------------------------
INGRESS_MODE="$(ingress_mode)"
INGRESS_IP="${DATASPOKE_KUBE_INGRESS_IP:-}"
DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"

SCHEME="$(ingress_scheme)"

if [[ "$INGRESS_MODE" == "shared" ]]; then
  # Shared mode: virtual hosts ride the pre-set domain over $SCHEME; TCP
  # services are reached on 127.0.0.1 via `kubectl port-forward`
  # (bin/port-forward.sh). No ingress IP.
  if [[ -z "$DOMAIN" ]]; then
    error "DATASPOKE_KUBE_INGRESS_DOMAIN must be set in ${ENV_FILE} (shared ingress mode)."
  fi
  TCP_HOST="127.0.0.1"
else
  if [[ -z "$INGRESS_IP" || -z "$DOMAIN" ]]; then
    error "DATASPOKE_KUBE_INGRESS_IP and DATASPOKE_KUBE_INGRESS_DOMAIN must be set in ${ENV_FILE}.
       Run helm-charts/bin/dev-peripherals/nginx-ingress.sh first."
  fi
  TCP_HOST="${INGRESS_IP}"
fi

# ---------------------------------------------------------------------------
# Tier A: HTTP services via ingress hostname
# ---------------------------------------------------------------------------
DS_API_URL="${SCHEME}://api.${DOMAIN}"
DH_GMS_URL="${SCHEME}://$(datahub_gms_host)"
AIRFLOW_URL="${SCHEME}://airflow.${DOMAIN}"
# No Airflow credentials are read here: /api/v2/monitor/health is unauthenticated,
# and this script can run against a prod env file, so a credential it does not
# need is exposure it does not need either.

# ---------------------------------------------------------------------------
# Tier B: TCP services — managed: ingress IP; shared: 127.0.0.1 via port-forward.
# Each prefers its DATASPOKE_DEV_* host if one was written to .env.
# ---------------------------------------------------------------------------
DS_PG_HOST="${DATASPOKE_DEV_POSTGRES_HOST:-${TCP_HOST}}"
DS_PG_PORT="${DATASPOKE_DEV_POSTGRES_PORT:-9201}"
DS_PG_USER="${DATASPOKE_DEV_POSTGRES_USER:-dataspoke}"
DS_PG_DB="${DATASPOKE_DEV_POSTGRES_DB:-dataspoke}"

DS_REDIS_HOST="${DATASPOKE_DEV_REDIS_HOST:-${TCP_HOST}}"
DS_REDIS_PORT="${DATASPOKE_DEV_REDIS_PORT:-9202}"
DS_REDIS_PASSWORD="${DATASPOKE_DEV_REDIS_PASSWORD:-}"

DH_KAFKA_BROKERS="${DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS:-${TCP_HOST}:9005}"
DH_KAFKA_HOST="${DH_KAFKA_BROKERS%%:*}"
DH_KAFKA_PORT="${DH_KAFKA_BROKERS##*:}"

DD_PG_HOST="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST:-${TCP_HOST}}"
DD_PG_PORT="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PORT:-9102}"
DD_PG_USER="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER:-postgres}"
DD_PG_DB="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB:-example_db}"

_DD_KAFKA_BROKERS="${DATASPOKE_DEV_DUMMY_DATA_KAFKA_BROKERS:-${TCP_HOST}:9104}"
DD_KAFKA_HOST="${_DD_KAFKA_BROKERS%%:*}"
DD_KAFKA_PORT="${_DD_KAFKA_BROKERS##*:}"

LOCK_HOST="${TCP_HOST}"
LOCK_PORT=9221

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
# The verdict text is a printf ARGUMENT, never part of the format string:
# several call sites below interpolate text that came back from a probed
# service, and `echo -e` would expand a remote `\033[...]` in it into a real
# terminal escape. sanitize_remote_text (lib/helpers.sh) covers the other half
# — a raw ESC byte, an embedded newline forging a second verdict line, and an
# unbounded body — and is applied at each of those call sites.
_pass() { printf '  \033[0;32m[PASS]\033[0m %s\n' "$*"; }
_fail() { printf '  \033[0;31m[FAIL]\033[0m %s\n' "$*"; }
_skip() { printf '  \033[0;33m[SKIP]\033[0m %s\n' "$*"; }
_info() { printf '  \033[0;36m[INFO]\033[0m %s\n' "$*"; }

FAILURES=0

# ---------------------------------------------------------------------------
# Check primitives
# ---------------------------------------------------------------------------

# Connect budget for the raw TCP probes, in whole seconds — the same 3s the
# curl-based probes below spend on --connect-timeout, so no endpoint gets a
# more generous dial from one probe type than the other.
#
# The poll adds its own fork overhead, ~20ms per 0.1s tick, so a probe that
# times out measures ~3.6s rather than 3.0s (measured against a blackholed
# 192.0.2.1:80). That measured number is what the run's worst-case wall clock
# is built from: ~6 dead TCP endpoints plus one memoized ingress probe and one
# memoized control-plane dial keeps a fully-down cluster inside the deadline
# the preflight hook enforces.
TCP_CONNECT_TIMEOUT_SECS=3

# Per-request budget for the control-plane reads. kubectl retries its API
# discovery, so a value of N measures roughly 4N against a server that does not
# answer at all; 3s is chosen for that multiplier, and _deployment_state
# memoizes an unreachable server so at most one read ever pays it.
KUBE_REQUEST_TIMEOUT=3s

# _tcp_check <host> <port>
# Succeed when a TCP connection to host:port can be established within
# TCP_CONNECT_TIMEOUT_SECS.
#
# The timeout is the point. Bash's /dev/tcp has no connect timeout of its own,
# so against a host that DROPS SYNs rather than refusing them — a LoadBalancer
# with no backends behind it, which is exactly what a scaled-to-zero dev
# cluster presents — the connect blocks for the kernel's whole retry budget,
# minutes per probe. This function runs first in nearly every check and gates
# the blocking integration-test preflight hook, where that stalls the run with
# no output at all.
#
# Bounded here rather than with `timeout(1)`, which stock macOS does not ship,
# and without adding a dependency: the connect runs in a background subshell
# that is killed once the deadline passes. `exec 3<>` opens the socket and
# writes NOTHING to it — the older `echo >/dev/tcp/...` sent a newline, and
# these ports are Postgres, Redis and Kafka, which log a stray byte as a
# protocol error. `wait` then reports the subshell's own status, so a refused
# connection is still a plain failure rather than a timeout.
_tcp_check() {
  local host="$1" port="$2"
  local pid ticks=0
  local max_ticks=$(( TCP_CONNECT_TIMEOUT_SECS * 10 ))

  ( exec 3<>/dev/tcp/"$host"/"$port" ) 2>/dev/null &
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    if (( ticks >= max_ticks )); then
      kill -9 "$pid" 2>/dev/null || true
      # stderr silenced on both waits: bash reports a signalled background job
      # ("Killed") when it reaps one, and that notice is not a verdict line.
      wait "$pid" 2>/dev/null || true
      return 1
    fi
    sleep 0.1
    ticks=$(( ticks + 1 ))
  done

  wait "$pid" 2>/dev/null
}

# Wall-clock budget for the probes that speak a real protocol after the connect
# — redis-cli PING, and the `uv run python` asyncpg/Kafka clients. Larger than
# TCP_CONNECT_TIMEOUT_SECS because it has to cover an interpreter start (`uv
# run` resolves the environment before the first line of Python executes) on
# top of the client's own 3-5s socket timeouts, and because it is only ever
# paid by an endpoint whose TCP connect already SUCCEEDED — a fully-down
# cluster fails at _tcp_check and never reaches one of these.
PROTOCOL_PROBE_TIMEOUT_SECS=20

# _bounded <secs> <command> [args...]
# Run <command> with a wall-clock bound, leaving its stdout in _BOUNDED_STDOUT.
# Returns the command's own status, or 124 if the bound was hit.
#
# The bound is the point, and it is not the same bound as _tcp_check's. Neither
# redis-cli nor a `uv run python` client offers an option that covers the case
# this has to survive: a port that ACCEPTS the connection and then never speaks
# — a stale `kubectl port-forward` whose upstream pod is gone, which is the
# ordinary shared-mode posture the README describes. redis-cli's own -t bounds
# the connect only, and the Python clients' socket timeouts do not bound `uv`
# resolving the environment before them. Unbounded, one such endpoint hangs
# this script forever, which is precisely the blocking pre-flight the preflight
# hook and .prauto wait on.
#
# Same background-and-kill idiom as _tcp_check, for the same reason: stock
# macOS ships no timeout(1). <command> may be a shell function, so a probe that
# needs a heredoc on stdin or an environment prefix wraps itself in one.
_BOUNDED_STDOUT=""
_bounded() {
  local secs="$1"; shift
  local out pid ticks=0 rc=0
  local max_ticks=$(( secs * 10 ))

  _BOUNDED_STDOUT=""
  # Built from $TMPDIR explicitly, like the kubeconfig copy in lib/helpers.sh:
  # macOS `mktemp -t` ignores $TMPDIR, and a caller that has to kill this run
  # (the preflight hook's deadline) reclaims what it can see.
  out="$(mktemp "${TMPDIR:-/tmp}/dataspoke-probe.XXXXXX")" || return 1

  # Job control on for the launch, so the child becomes a process-group leader
  # and the whole group can be signalled. Killing $pid alone would reach only
  # the immediate child: `uv run python` execs a python grandchild that would
  # survive its parent, and a probe's environment carries PGPASSWORD or
  # REDISCLI_AUTH for as long as the process lives. An interactive run leaks up
  # to five such orphans; the hook and .prauto only avoid it by killing the
  # whole script's group from outside.
  set -m
  "$@" >"$out" 2>/dev/null &
  pid=$!
  set +m

  while kill -0 "$pid" 2>/dev/null; do
    if (( ticks >= max_ticks )); then
      # Negative pid addresses the group; fall back to the bare pid if the
      # group is already gone.
      kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      sleep 0.2
      kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
      # stderr silenced: bash announces a signalled job when it reaps one.
      wait "$pid" 2>/dev/null || true
      rm -f "$out"
      return 124
    fi
    sleep 0.1
    ticks=$(( ticks + 1 ))
  done

  wait "$pid" 2>/dev/null || rc=$?
  _BOUNDED_STDOUT="$(cat "$out" 2>/dev/null || true)"
  rm -f "$out"
  return "$rc"
}

_http_ok() {
  local url="$1"
  shift
  local code
  code=$(curl -sf -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" =~ ^2 ]]
}

# "Something answered HTTP" — a real status line, whatever it says. A THREE-DIGIT
# code is required rather than merely "not 000": curl writes nothing at all when
# it cannot run, and an empty string is not 000, so a bare inequality reports a
# curl that never spoke to anything as a live server with a bad health body.
_http_alive() {
  local url="$1"
  shift
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" =~ ^[0-9][0-9][0-9]$ && "$code" != "000" ]]
}

# Pass condition for services whose root redirects rather than returning 200.
# Accepts 2xx/3xx only: an undeployed component reaches the ingress default
# backend (404) and an unready pod returns 502/503, neither of which is healthy.
_http_reachable() {
  local url="$1"
  shift
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$@" "$url" 2>/dev/null) || true
  [[ "$code" =~ ^[23] ]]
}

# Gate for HTTP-service checks. In managed mode a quick probe to the ingress
# IP:80 confirms the LoadBalancer is up before the deep HTTP check. In shared
# mode there is no single ingress IP to probe (it may be an internal LB behind
# a hostname), so skip the probe and let the HTTP check itself decide.
#
# Memoized: all five HTTP checks probe the same INGRESS_IP:80, so five calls
# ask one question — and against a LoadBalancer that drops SYNs each repeat
# costs the full TCP_CONNECT_TIMEOUT_SECS, four times over, inside a blocking
# hook whose whole budget is under a minute.
_INGRESS_PORT_STATE=""
_ingress_port_open() {
  if [[ "$INGRESS_MODE" == "shared" ]]; then return 0; fi
  if [[ -z "$_INGRESS_PORT_STATE" ]]; then
    if _tcp_check "${INGRESS_IP}" 80; then
      _INGRESS_PORT_STATE="open"
    else
      _INGRESS_PORT_STATE="closed"
    fi
  fi
  [[ "$_INGRESS_PORT_STATE" == "open" ]]
}

# _deployment_state <name> <namespace>
# Set DEPLOYMENT_STATE to `present`, `absent` or `unknown` for a Deployment.
#
# Answers through a global rather than stdout so the unreachable-server memo
# below survives the call: `$(...)` would run the whole function in a subshell
# and throw the memo away with it.
#
# Absence is decided here, from the release, and never from an HTTP status: an
# undeployed component reaches the ingress default backend (404) and a deployed
# but unready one returns 502/503, so the response cannot tell the two apart —
# which is how a dead API used to be reported as "may not be deployed".
#
# The other answers are what make this safe. A control-plane read can fail
# without saying anything about the workload (API server unreachable,
# credentials expired, RBAC denied), and reporting that as absence would turn
# every unreadable cluster green. Absence therefore requires kubectl's own
# NotFound status, and only NotFound. Its message names the resource that was
# missing, and the two possibilities are told apart rather than merged:
#
#   deployments.apps "x" not found  ->  absent            (the workload)
#   namespaces "y" not found        ->  namespace-absent  (its whole namespace)
#
# They are different facts and the callers decide them differently: a
# peripheral's own namespace being gone is that peripheral not being installed,
# while the shared dataspoke namespace being gone is a misconfiguration that
# must not be reported as "this optional component was simply not deployed".
# The resource word is matched immediately after `(NotFound): ` rather than
# anywhere in the message, so a NotFound about some other object cannot be read
# as this workload's absence. Anything that is not a NotFound is `unknown`,
# which callers count as a failure.
DEPLOYMENT_STATE=""
# Set once the API server has proved unreachable, so the remaining reads answer
# `unknown` without paying the dial again — see KUBE_REQUEST_TIMEOUT.
_KUBE_UNREACHABLE=false
_deployment_state() {
  local name="$1" ns="$2"
  local err

  if $_KUBE_UNREACHABLE; then
    DEPLOYMENT_STATE="unknown"
    return
  fi

  # stderr is captured (`2>&1 >/dev/null`, in that order) so the NotFound
  # message can be told apart from any other kubectl failure; stdout is
  # discarded because only the outcome matters.
  if err="$(kubectl get "deployment/${name}" -n "${ns}" --request-timeout="${KUBE_REQUEST_TIMEOUT}" 2>&1 >/dev/null)"; then
    DEPLOYMENT_STATE="present"
    return
  fi

  case "$err" in
    *"Unable to connect to the server"* | *"context deadline exceeded"* | \
    *"i/o timeout"* | *"connection refused"* | *"no such host"* | \
    *"TLS handshake timeout"* | *"EOF"*)
      _KUBE_UNREACHABLE=true
      DEPLOYMENT_STATE="unknown"
      return
      ;;
  esac

  # `deployment` / `namespace` without the plural suffix so both the singular
  # and plural spellings of the resource token match.
  case "$err" in
    *"(NotFound): deployment"*) DEPLOYMENT_STATE="absent" ;;
    *"(NotFound): namespace"*)  DEPLOYMENT_STATE="namespace-absent" ;;
    *)                          DEPLOYMENT_STATE="unknown" ;;
  esac
}

# _namespace_absence_is_component_absence <namespace>
# True when a missing <namespace> means the component was never installed.
#
# It does for a peripheral with a namespace of its own — a dev environment that
# never installed Langfuse has no langfuse-01 namespace at all, while
# .env.dev.example still names one, so counting that as a failure would redden a
# deployment nobody chose to make. It does NOT for the dataspoke namespace,
# which also holds the REQUIRED workloads (api, postgresql, redis): if that
# namespace is missing, the deployment is not partially absent, it is
# misconfigured or gone — and reporting `[SKIP] … not deployed
# (event-consumer.enabled=false)` for a component that IS enabled, on a run that
# then exits 0, is the false green this distinction exists to prevent.
_namespace_absence_is_component_absence() {
  [[ "$1" != "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-}" ]]
}

# _check_ready_replicas <label> <deployment> <namespace> <absent-verdict>
# Verdict for a workload with no HTTP surface, read from its Deployment's
# ready-replica count — evidence that the workload started, not that it is
# doing its job, which is the most this can observe for a consumer. Both
# callers are optional components, so <absent-verdict> is the [SKIP] text used
# when the release simply does not contain them.
_check_ready_replicas() {
  local label="$1" deployment="$2" ns="$3" absent_verdict="$4"

  _deployment_state "$deployment" "$ns"
  case "$DEPLOYMENT_STATE" in
    absent)
      _skip "${label} — ${absent_verdict}"
      return
      ;;
    namespace-absent)
      if _namespace_absence_is_component_absence "$ns"; then
        _skip "${label} — ${absent_verdict}"
      else
        _fail "${label} — namespace '${ns}' does not exist in this cluster; ${ENV_FILE} names it as the DataSpoke namespace, so this is a configuration fault, not an omitted component"
        FAILURES=$(( FAILURES + 1 ))
      fi
      return
      ;;
    unknown)
      _fail "${label} — could not read deployment/${deployment} in namespace '${ns}'"
      FAILURES=$(( FAILURES + 1 ))
      return
      ;;
    present) ;;  # fall through to the ready-replica read below
  esac

  # This is a SECOND round trip, and its status is kept rather than collapsed
  # into an empty string. Defaulting a failed read to 0 would report "0
  # replicas ready" — a statement about the workload — for a read that never
  # reached the API server, which is the exact conflation _deployment_state's
  # three-way answer exists to prevent. It is also the read most exposed to
  # KUBE_REQUEST_TIMEOUT: the _KUBE_UNREACHABLE memo was set from the call
  # above and cannot cover a server that dies between the two. Only a
  # SUCCESSFUL read of an empty field means zero.
  local ready ready_rc=0
  ready=$(kubectl get "deployment/${deployment}" -n "$ns" \
    --request-timeout="${KUBE_REQUEST_TIMEOUT}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null) || ready_rc=$?
  if (( ready_rc != 0 )); then
    _fail "${label} — deployment/${deployment} exists in '${ns}' but its readyReplicas could not be read (kubectl exited ${ready_rc}); this is a control-plane read failure, not a stopped workload"
    FAILURES=$(( FAILURES + 1 ))
    return
  fi
  ready="${ready:-0}"

  # Checked against ^[0-9]+$ BEFORE it reaches an arithmetic context. Bash
  # evaluates the operands of `(( ))` and of `-ge` as arithmetic expressions,
  # and an arithmetic expression expands command substitutions inside it — so
  # `[[ "$ready" -ge 1 ]]` on an unvalidated API-server response is a command
  # sink that executes silently and still returns false.
  if [[ ! "$ready" =~ ^[0-9]+$ ]]; then
    _fail "${label} — readyReplicas is not a number: '$(sanitize_remote_text "$ready" 60)'"
    FAILURES=$(( FAILURES + 1 ))
  elif (( ready >= 1 )); then
    _pass "${label} (${ready} replica(s) ready)"
  else
    _fail "${label} — 0 replicas ready"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

# _optional_probe_failed <label> <deployment> <namespace> <reinstall-hint>
# Verdict for an OPTIONAL component whose application-layer probe has already
# failed — the frontend (`--frontend none` is the dev default) and Langfuse.
# A component the release never deployed must not turn a dev environment red,
# so absence is a skip carrying its reinstall hint; a component that IS
# deployed and does not answer is a counted failure, and so is a namespace
# that cannot be read, since neither is evidence of a deliberate omission.
#
# Required components (api, postgresql, redis, airflow, datahub-gms) never
# come here: for them a failed probe is a failure whether or not the workload
# exists, and their own checks say so directly.
_optional_probe_failed() {
  local label="$1" deployment="$2" ns="$3" hint="$4"

  if [[ -z "$ns" ]]; then
    _fail "${label} — probe failed, and no namespace is set in ${ENV_FILE} to tell 'not deployed' from 'deployed but broken'"
    FAILURES=$(( FAILURES + 1 ))
    return
  fi

  _deployment_state "$deployment" "$ns"
  case "$DEPLOYMENT_STATE" in
    absent)
      _skip "${label} — not deployed (run: ${hint})"
      ;;
    namespace-absent)
      if _namespace_absence_is_component_absence "$ns"; then
        _skip "${label} — not deployed (run: ${hint})"
      else
        _fail "${label} — not responding, and namespace '${ns}' does not exist in this cluster; ${ENV_FILE} names it as the DataSpoke namespace, so this is a configuration fault, not an omitted component"
        FAILURES=$(( FAILURES + 1 ))
      fi
      ;;
    present)
      _fail "${label} — deployed in '${ns}' but not responding (pod may be starting; check its logs)"
      FAILURES=$(( FAILURES + 1 ))
      ;;
    *)
      _fail "${label} — not responding, and deployment/${deployment} in '${ns}' could not be read to tell absence from failure"
      FAILURES=$(( FAILURES + 1 ))
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Per-service checks
# ---------------------------------------------------------------------------

# _pg_probe <host> <port> <user> <db> <password>
# One connect-and-SELECT-1 against a Postgres, for the machines without
# pg_isready. Run through _bounded, so it is a function rather than an inline
# heredoc: `uv run` resolves the project environment before Python starts and
# neither that nor asyncpg's own timeout bounds a peer that accepts and stalls.
#
# The password arrives as a function argument, which lives in this shell and
# never in any process's argv, and reaches the client through the child's
# ENVIRONMENT rather than a `-c` string. Both halves matter: argv is
# world-readable through `ps auxww` / /proc/<pid>/cmdline for the life of the
# process, and a `-c` program built by splicing the password in is a
# Python-injection sink for a `'` in it (it closes the literal early).
_pg_probe() {
  PGHOST="$1" PGPORT="$2" PGUSER="$3" PGDATABASE="$4" PGPASSWORD="$5" \
    uv run python - <<'PYEOF'
import asyncio, asyncpg, os, sys
async def check():
    try:
        conn = await asyncpg.connect(host=os.environ['PGHOST'], port=int(os.environ['PGPORT']),
                                     user=os.environ['PGUSER'], database=os.environ['PGDATABASE'],
                                     password=os.environ.get('PGPASSWORD') or None, timeout=3)
        await conn.execute('SELECT 1')
        await conn.close()
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
asyncio.run(check())
PYEOF
}

# _check_postgres <label> <host> <port> <user> <db> <password> <fail-hint>
# pg_isready when it is installed, the asyncpg probe otherwise. Both are
# bounded; a bound that is HIT is reported as its own verdict, because "the
# port answered and then nothing spoke for 20s" and "the connection was
# refused" are different faults with different fixes.
_check_postgres() {
  local label="$1" host="$2" port="$3" user="$4" db="$5" password="$6" hint="$7"

  if ! _tcp_check "$host" "$port"; then
    _fail "$label — port not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi

  # Neither probe is available: skip naming both, rather than run the asyncpg
  # branch without uv and report "cannot connect" for a database that is
  # answering. Same reasoning as the redis-cli and uv skips above — the tool is
  # missing from this workstation, which is not a verdict on the deployment,
  # and a counted failure here would have .prauto provision a cluster over it.
  if ! command -v pg_isready >/dev/null 2>&1 && ! command -v uv >/dev/null 2>&1; then
    _skip "$label — port open, but neither pg_isready nor uv is installed so no connection was attempted"
    return
  fi

  if command -v pg_isready &>/dev/null; then
    # -t bounds the wait explicitly rather than leaning on the build default,
    # and PGPASSWORD goes in the child's environment, never argv.
    if PGPASSWORD="$password" pg_isready -h "$host" -p "$port" -U "$user" -d "$db" \
       -t "$TCP_CONNECT_TIMEOUT_SECS" -q 2>/dev/null; then
      _pass "$label"
    else
      _fail "$label — pg_isready failed${hint:+ ($hint)}"
      FAILURES=$(( FAILURES + 1 ))
    fi
    return
  fi

  local rc=0
  _bounded "$PROTOCOL_PROBE_TIMEOUT_SECS" _pg_probe "$host" "$port" "$user" "$db" "$password" || rc=$?
  if (( rc == 0 )); then
    _pass "$label"
  elif (( rc == 124 )); then
    _fail "$label — port accepted the connection but no Postgres answer within ${PROTOCOL_PROBE_TIMEOUT_SECS}s (a stale port-forward with no pod behind it looks exactly like this)"
    FAILURES=$(( FAILURES + 1 ))
  else
    _fail "$label — cannot connect${hint:+ ($hint)}"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

check_dataspoke_postgresql() {
  _check_postgres "dataspoke-postgresql (${DS_PG_HOST}:${DS_PG_PORT})" \
    "$DS_PG_HOST" "$DS_PG_PORT" "$DS_PG_USER" "$DS_PG_DB" \
    "${DATASPOKE_DEV_POSTGRES_PASSWORD:-}" "pod may be restarting"
}

check_example_postgres() {
  _check_postgres "example-postgres (${DD_PG_HOST}:${DD_PG_PORT})" \
    "$DD_PG_HOST" "$DD_PG_PORT" "$DD_PG_USER" "$DD_PG_DB" \
    "${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-}" ""
}

# REDISCLI_AUTH in the child's environment rather than `-a` in argv, which is
# world-readable through `ps auxww` / /proc/<pid>/cmdline for the life of the
# process — the same reasoning as PGPASSWORD above. The password arrives as a
# function argument, which no process can see at all.
_redis_probe() {
  REDISCLI_AUTH="$1" redis-cli -h "$2" -p "$3" PING
}

check_dataspoke_redis() {
  local label="dataspoke-redis (${DS_REDIS_HOST}:${DS_REDIS_PORT})"
  if ! _tcp_check "$DS_REDIS_HOST" "$DS_REDIS_PORT"; then
    _fail "$label — port not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi
  if ! command -v redis-cli >/dev/null 2>&1; then
    _skip "$label — port open, but redis-cli is not installed so PING was not sent"
    return
  fi

  # Bounded: redis-cli's own -t covers the connect, not a peer that accepts and
  # then never answers, and this is the only Redis probe in the run.
  local rc=0
  _bounded "$PROTOCOL_PROBE_TIMEOUT_SECS" _redis_probe \
    "$DS_REDIS_PASSWORD" "$DS_REDIS_HOST" "$DS_REDIS_PORT" || rc=$?

  if (( rc == 0 )) && [[ "$_BOUNDED_STDOUT" == "PONG" ]]; then
    _pass "$label"
  elif (( rc == 124 )); then
    _fail "$label — port accepted the connection but PING went unanswered for ${PROTOCOL_PROBE_TIMEOUT_SECS}s (a stale port-forward with no pod behind it looks exactly like this)"
    FAILURES=$(( FAILURES + 1 ))
  else
    _fail "$label — PING did not return PONG"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

check_dataspoke_airflow() {
  local label="dataspoke-airflow (${AIRFLOW_URL})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi

  local health_url="${AIRFLOW_URL}/api/v2/monitor/health"
  local health_body
  health_body=$(curl -s -o - -w "" --connect-timeout 3 --max-time 5 \
    "${health_url}" 2>/dev/null) || true

  if echo "$health_body" | grep -q '"status": *"healthy"'; then
    _pass "$label"
  elif _http_alive "${health_url}"; then
    _fail "$label — HTTP alive but health endpoint reports unhealthy: $(sanitize_remote_text "$health_body")"
    FAILURES=$(( FAILURES + 1 ))
  else
    _fail "$label — no HTTP response (pod may be starting)"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

check_dataspoke_api() {
  local label="dataspoke-api (${DS_API_URL})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi

  # The API is a required component of every profile, so a failed probe is a
  # failure whether or not the workload exists — there is no deployment in
  # which its absence is a legitimate configuration choice, and a run that
  # reported "may not be deployed" here is exactly how a dead API used to pass
  # this gate and hand a green light to an integration suite.
  if _http_ok "${DS_API_URL}/health"; then
    _pass "$label"
  else
    _fail "$label — /health did not return 2xx (run: install.sh --profile dev --components api)"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

# The event-consumer serves no HTTP surface, so readiness is read from the
# Deployment rather than probed over the ingress — same approach as the
# langfuse-worker check below. A pod stuck in CrashLoopBackOff is otherwise
# invisible to this pre-flight, which gates integration-test runs.
check_dataspoke_event_consumer() {
  local ns="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-}"
  local label="dataspoke-event-consumer"
  # A skip, where _optional_probe_failed counts the same unset variable as a
  # failure — the two are not the same question. Here nothing has been probed
  # yet: with no namespace there is simply no observation to make, and the
  # component is disabled by default outside dev. There, a probe has ALREADY
  # failed, and the namespace is the only thing that could still excuse it; an
  # unset one leaves a known failure unexplained, so it stays a failure.
  if [[ -z "$ns" ]]; then
    _skip "$label — DATASPOKE_KUBE_DATASPOKE_NAMESPACE unset in ${ENV_FILE}"
    return
  fi

  # Disabled by default outside dev, so absence is a configuration choice.
  _check_ready_replicas "$label" dataspoke-event-consumer "$ns" \
    "not deployed (event-consumer.enabled=false)"
}

check_dataspoke_frontend() {
  local fe_url="${SCHEME}://app.${DOMAIN}"
  local label="dataspoke-frontend (${fe_url})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi

  # The UI root redirects (307 → /login), so accept 3xx alongside 2xx.
  # `--frontend none` is the dev default (developers run host `pnpm dev`), so a
  # frontend the release never deployed is a skip — but one that IS deployed
  # and does not answer is a failure, which the response code alone cannot say.
  if _http_reachable "${fe_url}/"; then
    _pass "$label"
  else
    _optional_probe_failed "$label" dataspoke-frontend "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-}" \
      "install.sh --profile dev --components frontend"
  fi
}

check_dataspoke_langfuse() {
  local lf_url="${SCHEME}://langfuse.${DOMAIN}"
  local label="langfuse-web (${lf_url})"

  # Langfuse is an optional dev-only peripheral. Its namespace variable is the
  # only signal this script has for whether a deployment claims Langfuse at
  # all: a prod env file carries no DATASPOKE_DEV_* names by design, so an
  # unset one is absence by configuration and both halves below skip on it
  # rather than reddening a healthy prod run.
  local worker_ns="${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE:-}"

  # The web half is gated on the ingress; the worker half below is NOT, and the
  # two are deliberately sequential rather than nested. An early return here
  # would drop the langfuse-worker line out of the report entirely whenever the
  # ingress is down — a workload judged on a control-plane read, made invisible
  # by an HTTP path it never uses.
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    FAILURES=$(( FAILURES + 1 ))
  elif _http_reachable "${lf_url}/"; then
    _pass "$label"
  elif [[ -z "$worker_ns" ]]; then
    _skip "$label — not configured (no DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE in ${ENV_FILE})"
  else
    _optional_probe_failed "$label" langfuse-web "$worker_ns" \
      "install.sh --profile dev --components langfuse"
  fi

  if [[ -z "$worker_ns" ]]; then
    _skip "langfuse-worker — not configured (no DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE in ${ENV_FILE})"
  else
    _check_ready_replicas "langfuse-worker" langfuse-worker "$worker_ns" "not deployed"
  fi
}

check_datahub_gms() {
  local label="datahub-gms (${DH_GMS_URL})"
  if ! _ingress_port_open; then
    _fail "$label — ingress not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi

  if _http_ok "${DH_GMS_URL}/health"; then
    _pass "$label"
  else
    _fail "$label — /health did not return 2xx"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

# _kafka_probe <host> <port>
# Broker coordinates reach the client through the child's ENVIRONMENT rather
# than a `-c` string — the same pattern, and the same reason, as _pg_probe
# above: a `-c` program built by splicing the host and port in is a
# Python-injection sink for a `'` in either (it closes the literal early), and
# these values come out of an operator env file whose broker list is free text.
_kafka_probe() {
  KAFKA_HOST="$1" KAFKA_PORT="$2" uv run python - <<'PYEOF'
from confluent_kafka.admin import AdminClient
import os, sys
try:
    a = AdminClient({'bootstrap.servers': '%s:%s' % (os.environ['KAFKA_HOST'], os.environ['KAFKA_PORT']),
                     'socket.timeout.ms': '5000',
                     'request.timeout.ms': '5000'})
    md = a.list_topics(timeout=5)
    print(f'{len(md.topics)} topics', file=sys.stderr)
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
PYEOF
}

check_kafka() {
  local label="$1" host="$2" port="$3"
  if ! _tcp_check "$host" "$port"; then
    _fail "$label — port not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi
  if ! command -v uv >/dev/null 2>&1; then
    _skip "$label — port open, but uv is not installed so no metadata request was made"
    return
  fi

  # Bounded: librdkafka's socket/request timeouts do not cover `uv` resolving
  # the project environment before Python starts, and a broker that accepts the
  # connection without completing a metadata exchange would otherwise stall the
  # whole run.
  local rc=0
  _bounded "$PROTOCOL_PROBE_TIMEOUT_SECS" _kafka_probe "$host" "$port" || rc=$?
  if (( rc == 0 )); then
    _pass "$label"
  elif (( rc == 124 )); then
    _fail "$label — port accepted the connection but no metadata response within ${PROTOCOL_PROBE_TIMEOUT_SECS}s (a stale port-forward with no broker behind it looks exactly like this)"
    FAILURES=$(( FAILURES + 1 ))
  else
    _fail "$label — broker not responding to metadata request"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

# The owner and message come from another session's lock claim — remote text
# on this script's own terminal — so they are sanitized wherever they are
# PRINTED. The value sent back to the lock service stays the raw one: it is
# matched against the stored owner there, and a display transformation must not
# change which lock this releases.
_release_lock() {
  local owner="$1" message="$2"
  local safe_owner safe_message
  safe_owner="$(sanitize_remote_text "$owner" 80)"
  safe_message="$(sanitize_remote_text "$message" 120)"

  # ENCODED, not concatenated. The value goes back on the wire verbatim by
  # design, but building the document by splicing it into a literal makes the
  # remote string decide the document's shape — a trailing backslash escapes
  # the closing quote and the request stops being JSON. jq when it is present
  # (it is a hard dependency of the preflight hook that calls this script);
  # otherwise a backslash-escaping fallback, since the `"` is already excluded
  # by the extraction below.
  local body
  if command -v jq >/dev/null 2>&1; then
    # `|| fallback`, because this runs in the probe region where errexit is
    # live: jq exits 2 on a usage error, which .prauto would read as "the run
    # could not be set up" and answer by provisioning a cluster.
    body="$(jq -n --arg owner "$owner" '{owner: $owner}')" \
      || body="{\"owner\": \"${owner//\\/\\\\}\"}"
  else
    body="{\"owner\": \"${owner//\\/\\\\}\"}"
  fi

  local release_resp
  release_resp=$(curl -sf -X POST --connect-timeout 3 --max-time 5 \
    -H 'Content-Type: application/json' \
    -d "$body" \
    "http://${LOCK_HOST}:${LOCK_PORT}/lock/release" 2>/dev/null) || true
  if echo "$release_resp" | grep -q '"locked" *: *false'; then
    _info "released lock from '${safe_owner}' (${safe_message})"
  else
    _fail "dev-env lock held by '${safe_owner}' — failed to release"
    FAILURES=$(( FAILURES + 1 ))
  fi
}

check_lock_service() {
  local label="lock-service (${LOCK_HOST}:${LOCK_PORT})"
  if ! _tcp_check "$LOCK_HOST" "$LOCK_PORT"; then
    _fail "$label — port not reachable"
    FAILURES=$(( FAILURES + 1 )); return
  fi

  if _http_ok "http://${LOCK_HOST}:${LOCK_PORT}/health"; then
    _pass "$label"
  else
    _fail "$label — /health did not return 2xx"
    FAILURES=$(( FAILURES + 1 ))
    return
  fi

  local lock_json
  lock_json=$(curl -sf --connect-timeout 3 --max-time 5 "http://${LOCK_HOST}:${LOCK_PORT}/lock" 2>/dev/null) || true
  if [[ -n "$lock_json" ]]; then
    local locked owner message safe_owner safe_message
    locked=$(echo "$lock_json" | grep -o '"locked" *: *true' || true)
    if [[ -n "$locked" ]]; then
      owner=$(echo "$lock_json" | sed -n 's/.*"owner" *: *"\([^"]*\)".*/\1/p')
      message=$(echo "$lock_json" | sed -n 's/.*"message" *: *"\([^"]*\)".*/\1/p')
      safe_owner="$(sanitize_remote_text "$owner" 80)"
      safe_message="$(sanitize_remote_text "$message" 120)"
      if $KEEP_LOCK; then
        _info "dev-env lock held by '${safe_owner}' (${safe_message}) — kept (--keep-lock)"
      elif $FORCE_RELEASE; then
        _release_lock "$owner" "$message"
      else
        echo ""
        _info "dev-env lock held by '${safe_owner}' (${safe_message})"
        printf "  Release this lock? [y/N] "
        local answer
        read -r answer || answer=""
        if [[ "$answer" =~ ^[Yy]$ ]]; then
          _release_lock "$owner" "$message"
        else
          _fail "dev-env lock held by '${safe_owner}' — integration tests will skip"
          FAILURES=$(( FAILURES + 1 ))
        fi
      fi
    else
      _info "dev-env lock is free"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------
echo ""
echo "DataSpoke health check"
echo "======================"
# Named before the first probe, always: every URL and host below comes out of
# this file, so an operator who meant one deployment and resolved another sees
# it here rather than reading its verdict as their own.
echo "  Env file:       ${ENV_FILE}"
echo "  Kube context:   ${DATASPOKE_KUBE_CLUSTER}"
if [[ "$INGRESS_MODE" == "shared" ]]; then
  echo "  Ingress mode:   shared (TCP via 127.0.0.1 port-forward — run bin/port-forward.sh)"
else
  echo "  Ingress IP:     ${INGRESS_IP}"
fi
echo "  Ingress domain: ${DOMAIN}"
echo ""

# Setup is over; everything below is measurement, and its verdict is carried by
# the summary's own exit 0/1. Dropping the override here keeps exit 2 meaning
# exactly "nothing was probed" — see the exit-code table at the top of the file.
#
# INVARIANT for everything below this line: `set -e` is still in force, so a
# probe-region command that happens to exit 2 would reach the consumers as "the
# run could not be set up" and stop .prauto from acting on a genuinely sick
# cluster. Every probe-region command therefore ends in `|| true`, sits in an
# `if`/`case` condition, or captures its status with `|| rc=$?` — new probe code
# must keep doing one of those three, including the command substitutions that
# feed a variable assignment (an assignment carries the substitution's status).
unset DATASPOKE_ERROR_EXIT_CODE

echo "DataSpoke Infra:"
check_dataspoke_postgresql
check_dataspoke_redis
check_dataspoke_airflow
check_dataspoke_api
check_dataspoke_event_consumer
check_dataspoke_frontend
check_dataspoke_langfuse

echo ""
echo "DataHub:"
check_datahub_gms
check_kafka "datahub-kafka (${DH_KAFKA_HOST}:${DH_KAFKA_PORT})" "$DH_KAFKA_HOST" "$DH_KAFKA_PORT"

echo ""
echo "Dummy Data:"
check_example_postgres
check_kafka "example-kafka (${DD_KAFKA_HOST}:${DD_KAFKA_PORT})" "$DD_KAFKA_HOST" "$DD_KAFKA_PORT"

echo ""
echo "Dev Coordination:"
check_lock_service

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ $FAILURES -eq 0 ]]; then
  printf '\033[0;32mAll services healthy.\033[0m Ready to run integration tests.\n'
  echo ""
  exit 0
fi

printf '\033[0;31m%s service(s) unhealthy.\033[0m\n' "$FAILURES"
echo "Fix failing services before running integration tests."
echo "Reinstall hint: ./helm-charts/bin/install.sh --profile dev --components <name>"
echo "Troubleshooting: see spec/feature/HELM_CHART.md §Troubleshooting"
echo ""

# 1, never 2: the probes ran, so this IS a verdict on the deployment.
exit 1
