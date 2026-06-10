#!/usr/bin/env bash
# Stop hook (frontend agent): block completion while `pnpm typecheck` fails
# so the agent fixes type errors before reporting done (exit 2 → stderr is
# fed back to the agent). `stop_hook_active` guards against an infinite
# block loop — if the agent already continued once because of this hook and
# typecheck still fails, surface a warning instead of blocking again.

set -u

input=$(cat)
stop_active=$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)

project_root=${CLAUDE_PROJECT_DIR:-$(pwd)}
frontend_dir="$project_root/src/frontend"

command -v pnpm >/dev/null 2>&1 || exit 0
[[ -f "$frontend_dir/package.json" ]] || exit 0

output=$(pnpm -C "$frontend_dir" typecheck 2>&1)
rc=$?

if (( rc == 0 )); then
  exit 0
fi

if [[ "$stop_active" == "true" ]]; then
  echo "frontend typecheck still failing after a fix attempt — report the remaining errors in your completion report instead of fixing further." >&2
  exit 0
fi

printf 'frontend typecheck failed — fix the errors before finishing:\n%s\n' "$output" >&2
exit 2
