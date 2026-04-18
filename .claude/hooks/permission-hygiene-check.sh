#!/usr/bin/env bash
# Stop hook: warn when .claude/settings.local.json has accumulated too many
# ad-hoc permission entries. The /fewer-permission-prompts skill can
# consolidate them.

set -u

THRESHOLD=40
project_root=${CLAUDE_PROJECT_DIR:-$(pwd)}
settings="$project_root/.claude/settings.local.json"

if [[ ! -f "$settings" ]]; then
  exit 0
fi

count=$(jq '(.permissions.allow // []) | length' "$settings" 2>/dev/null || echo 0)

if (( count > THRESHOLD )); then
  echo >&2 "settings.local.json has $count permission entries (> $THRESHOLD). Run /fewer-permission-prompts to consolidate."
fi

exit 0
