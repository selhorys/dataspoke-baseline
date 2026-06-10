#!/usr/bin/env bash
# PostToolUse hook (backend / airflow-dag / test agents): run ruff on the
# Python file just edited and feed violations back to the agent (exit 2 →
# stderr is shown to the model) so mechanical issues are fixed in-flight
# instead of consuming a reviewer fix pass.
#
# Silent pass-through on non-Python files, missing tooling, or clean checks.

set -u

input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac

[[ -f "$file" ]] || exit 0

project_root=${CLAUDE_PROJECT_DIR:-$(pwd)}
command -v uv >/dev/null 2>&1 || exit 0

output=$(cd "$project_root" && uv run ruff check "$file" 2>&1)
rc=$?

if (( rc != 0 )); then
  printf 'ruff check failed for %s — fix before continuing:\n%s\n' "$file" "$output" >&2
  exit 2
fi

exit 0
