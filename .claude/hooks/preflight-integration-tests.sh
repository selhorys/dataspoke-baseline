#!/usr/bin/env bash
# PreToolUse hook: run helm-charts/bin/health-check.sh before integration tests.
# Blocks (exit 2) if health-check fails so pytest does not waste minutes
# against a broken dev-env. Rate-limited to one real check per 60 seconds.

set -u

MARKER=/tmp/dataspoke-healthcheck-ok.mtime
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
# Supported prefix: DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1.
# Match only literal pytest invocations; commands that merely mention the string
# (e.g., echo or grep arguments) should not trigger.
pytest_re='^[[:space:]]*(DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1[[:space:]]+)?uv run pytest[[:space:]]+tests/integration'
if ! printf '%s' "$cmd" | grep -qE "$pytest_re"; then
  exit 0
fi

project_root=${CLAUDE_PROJECT_DIR:-$(pwd)}
health_check="$project_root/helm-charts/bin/health-check.sh"

if [[ ! -x "$health_check" ]]; then
  # health-check missing — don't fight the user, let pytest proceed
  exit 0
fi

if [[ -f "$MARKER" ]]; then
  now=$(date +%s)
  last=$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER" 2>/dev/null || echo 0)
  if (( now - last < MAX_AGE )); then
    exit 0
  fi
fi

output=$("$health_check" --quick 2>&1)
rc=$?

if [[ $rc -eq 0 ]]; then
  touch "$MARKER"
  exit 0
fi

cat >&2 <<EOF
Dev-env health-check failed (exit $rc). Integration tests will fail misleadingly against a broken cluster.

health-check output:
$output

Reinstall the failing subsystem (per CLAUDE.md §Integration Test Protocol):
  airflow / postgres / redis → ./helm-charts/bin/install.sh --profile dev --components dataspoke-infra
  datahub-gms / kafka        → ./helm-charts/bin/install.sh --profile dev --components datahub
  example-postgres/kafka     → ./helm-charts/bin/install.sh --profile dev --components dummy-data
  lock-service               → ./helm-charts/bin/install.sh --profile dev --components dev-lock

Fix the failing component, then re-run the pytest command.
EOF
exit 2
