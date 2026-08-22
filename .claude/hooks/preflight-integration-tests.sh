#!/usr/bin/env bash
# PreToolUse hook: run helm-charts/bin/health-check.sh before integration tests.
# Blocks (exit 2) if health-check fails so pytest does not waste minutes
# against a broken dev-env. Rate-limited to one real check per 60 seconds.
#
# Three outcomes, all of them blocking except the first: healthy (0, silent),
# unhealthy (1, reinstall table), could-not-be-set-up (2, local configuration
# fault — never a reinstall table), plus a self-imposed deadline that blocks
# rather than letting the harness kill this hook, which would fail OPEN.

set -u

MAX_AGE=60

# Parse the hook event JSON on stdin and extract the Bash command.
# settings.json gates this hook with `"if": "Bash(uv run pytest tests/integration*)"`;
# the in-script gate below is defense-in-depth and applies the stricter anchored regex.
input=$(cat)
tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)

if [[ "$tool_name" != "Bash" ]]; then
  exit 0
fi

# Anchor at start-of-command (after optional leading whitespace and env prefix).
# Supported prefix: DATASPOKE_DEV_LOCK_PREACQUIRED=1.
# Match only literal pytest invocations; commands that merely mention the string
# (e.g., echo or grep arguments) should not trigger.
pytest_re='^[[:space:]]*(DATASPOKE_DEV_LOCK_PREACQUIRED=1[[:space:]]+)?uv run pytest[[:space:]]+tests/integration'
if ! printf '%s' "$cmd" | grep -qE "$pytest_re"; then
  exit 0
fi

project_root=${CLAUDE_PROJECT_DIR:-$(pwd)}
health_check="$project_root/helm-charts/bin/health-check.sh"

if [[ ! -x "$health_check" ]]; then
  # health-check missing — don't fight the user, let pytest proceed
  exit 0
fi

# The rate-limit marker is a BYPASS TOKEN: its mtime is what lets a pytest run
# through without a check. It therefore lives under the project's own .claude
# directory rather than in world-writable /tmp, where any local process could
# `touch` it to buy 60s of ungated runs, or pre-plant a symlink that this
# hook's own `touch` would then follow.
MARKER_DIR="$project_root/.claude/.cache"
MARKER="$MARKER_DIR/healthcheck-ok.mtime"
mkdir -p "$MARKER_DIR" 2>/dev/null || true

# Trusted only when it is a REGULAR file owned by this user: a symlink or a
# file someone else owns is not this hook's marker, whatever its mtime says.
if [[ -f "$MARKER" && ! -L "$MARKER" && -O "$MARKER" ]]; then
  now=$(date +%s)
  last=$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER" 2>/dev/null || echo 0)
  if (( now - last < MAX_AGE )); then
    exit 0
  fi
elif [[ -e "$MARKER" || -L "$MARKER" ]]; then
  # Present but not ours — remove it rather than let it linger as something the
  # `touch` below would write through.
  rm -f "$MARKER" 2>/dev/null || true
fi

# --keep-lock ONLY for a command that carries this session's own pre-acquired
# lock. That prefix is the whole difference between "the lock this run already
# owns" and "somebody else's lock", and passing --keep-lock unconditionally
# collapses them: the check reports a foreign or stale lock as [INFO], exits 0,
# and pytest then SKIPS every integration test and exits 0 too — a green run
# that measured nothing (memory project_integration_lock_stale_skip). Without
# the flag, the held-lock branch prompts; stdin is closed below, the read hits
# EOF, and the answer falls through to a counted failure, which blocks.
hc_args=()
if printf '%s' "$cmd" | grep -qE '^[[:space:]]*DATASPOKE_DEV_LOCK_PREACQUIRED=1[[:space:]]'; then
  hc_args+=(--keep-lock)
fi

# Self-bounded, because a hook that is killed at ITS timeout is reported as a
# non-blocking error and pytest proceeds — so the gate would fail open in
# exactly the state it exists to catch, a cluster that blackholes. The check's
# own probes are individually bounded, but the wall clock of the whole run is
# what the harness kills on, so the deadline is enforced here and turned into
# our own blocking exit 2. Keep HC_TIMEOUT plus the 2s SIGTERM grace below the
# hook timeout configured in .claude/settings.json (60s by default) — 45+2=47
# leaves 13s of margin.
#
# 45s is derived from a measurement, not asserted. A fully-blackholed cluster
# with an instant (stubbed) kubectl finishes in 25s — every TCP connect and
# every curl hits its own bound and no protocol probe is ever reached — and one
# non-memoized kubectl read against an API server that drops SYNs adds ~13s
# (kubectl retries discovery, so --request-timeout=3s measures roughly 4x). The
# fully-down case is therefore ~38s, 7s inside this deadline. Re-measure
# whenever a probe or a non-memoized kubectl read is added; a PARTIALLY
# responsive cluster is the case that can still overrun, since its endpoints
# accept and then stall, and each stalled protocol probe costs the check's own
# PROTOCOL_PROBE_TIMEOUT_SECS.
HC_TIMEOUT=45

# A private TMPDIR for the child, removed here however this hook ends. It is
# not tidiness: health-check.sh calls use_context, which writes `kubectl config
# view --raw` — every context's client certificate, key and token — to a
# mode-600 temp file it removes in its own EXIT trap. The deadline path below
# can end that process before the trap runs, so the file has to be reclaimable
# from out here. It lands in here because the scripts build their temp paths
# from $TMPDIR by hand; `mktemp -t` would not, since macOS resolves that to the
# per-user Darwin temp directory and ignores $TMPDIR. mktemp failing means this
# gate cannot run at all, which BLOCKS rather than waves pytest through against
# an unverified cluster.
hc_tmpdir=$(mktemp -d -t dataspoke-healthcheck.XXXXXX) || {
  cat >&2 <<'EOF'
Dev-env health-check could not be started: mktemp -d failed, so this pre-flight gate has no
scratch directory to run it in (a full or unwritable $TMPDIR is the usual cause).
Nothing was probed. Free up $TMPDIR (or set it to a writable directory), then re-run the pytest command.
EOF
  exit 2
}
trap 'rm -rf "$hc_tmpdir"' EXIT
out_file="$hc_tmpdir/output"

# `set -m` gives the check its own process group, so the deadline below can
# kill the group and take its in-flight curl/kubectl children with it rather
# than orphaning them. ${hc_args[@]+...}: bash 3.2 (stock macOS) treats an
# empty array as unset under `set -u`.
set -m
TMPDIR="$hc_tmpdir" "$health_check" ${hc_args[@]+"${hc_args[@]}"} </dev/null >"$out_file" 2>&1 &
hc_pid=$!
set +m

# Measured against the clock, not by counting 0.1s ticks: each tick also forks
# `sleep`, ~18ms of it, so 450 ticks measure ~53s rather than 45 — a deadline
# that drifts PAST the harness timeout it exists to stay inside.
hc_deadline=$(( $(date +%s) + HC_TIMEOUT ))
while kill -0 "$hc_pid" 2>/dev/null; do
  if (( $(date +%s) >= hc_deadline )); then
    # TERM first, and only then SIGKILL. bash runs a script's EXIT trap on its
    # way out of a fatal SIGTERM, which is how the check's own kubeconfig copy
    # gets removed; SIGKILL skips it. The private TMPDIR above is the backstop
    # for the child that ignores TERM and has to be killed anyway.
    kill -TERM -"$hc_pid" 2>/dev/null || kill -TERM "$hc_pid" 2>/dev/null || true
    grace=0
    while kill -0 "$hc_pid" 2>/dev/null && (( grace < 20 )); do
      sleep 0.1
      grace=$(( grace + 1 ))
    done
    if kill -0 "$hc_pid" 2>/dev/null; then
      kill -9 -"$hc_pid" 2>/dev/null || kill -9 "$hc_pid" 2>/dev/null || true
    fi
    # stderr silenced: bash announces a signalled job when it reaps one, and
    # that notice is not part of the message this hook hands back.
    wait "$hc_pid" 2>/dev/null || true
    cat >&2 <<EOF
Dev-env health-check did not finish within ${HC_TIMEOUT}s — treating that as unhealthy.
Every individual probe in it is bounded, so an overrun means the cluster is answering slowly or
not at all: an unreachable API server, a LoadBalancer dropping SYNs, a VPN that is down, or
endpoints that accept the connection and then never speak (a stale port-forward with no pod
behind it), which is the one case that can outlast this deadline even though each probe is bounded.
The partial output below shows how far the run got.

partial health-check output:
$(cat "$out_file")

Bring the dev-env back up (./helm-charts/bin/install.sh --profile dev), or run
./helm-charts/bin/health-check.sh yourself to see where it stalls, then re-run the pytest command.
EOF
    exit 2
  fi
  sleep 0.1
done

rc=0
wait "$hc_pid" || rc=$?
output=$(cat "$out_file")

if [[ $rc -eq 0 ]]; then
  touch "$MARKER"
  exit 0
fi

# Exit 2 means the check never probed anything: it could not be set up on THIS
# machine (missing kubectl, an unset/unresolvable DATASPOKE_KUBE_CLUSTER, a
# missing env file). Printing the reinstall table for that would send the
# session to rebuild a cluster that may be perfectly healthy.
if [[ $rc -eq 2 ]]; then
  cat >&2 <<EOF
Dev-env health-check could not run (exit 2) — a LOCAL CONFIGURATION fault, not a verdict on the cluster.
Nothing was probed, so this says nothing about whether the deployment is healthy.

health-check output:
$output

Usual causes: kubectl not on PATH, DATASPOKE_KUBE_CLUSTER unset in helm-charts/.env.dev,
a context name that is not in your kubeconfig, or a missing helm-charts/.env.dev.
Fix the local configuration, then re-run the pytest command.
EOF
  exit 2
fi

cat >&2 <<EOF
Dev-env health-check failed (exit $rc). Integration tests will fail misleadingly against a broken cluster.

health-check output:
$output

Reinstall the failing subsystem (per AGENTS.md §Integration Test Protocol):
  airflow / postgres / redis → ./helm-charts/bin/install.sh --profile dev --components dataspoke-infra
  datahub-gms / kafka        → ./helm-charts/bin/install.sh --profile dev --components datahub
  example-postgres/kafka     → ./helm-charts/bin/install.sh --profile dev --components dummy-data
  lock-service               → ./helm-charts/bin/install.sh --profile dev --components dev-lock

If the output shows a dev-env lock held by another owner, nothing is broken — that lock makes the
whole integration suite SKIP and exit 0, which is why it blocks here. Release it once you know the
other session is done: ./helm-charts/bin/health-check.sh --force-release

Fix the failing component, then re-run the pytest command.
EOF
exit 2
