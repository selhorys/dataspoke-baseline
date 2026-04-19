#!/usr/bin/env bash
# UserPromptSubmit hook: soft reminder to enter Plan mode when the prompt
# looks like a non-trivial implementation request.
#
# Heuristic — NOT a hard block. CLAUDE.md §Implementation Workflow is
# authoritative; this hook just surfaces it at the right moment.

set -u

# stdin is the hook event JSON; we only need `prompt`.
input=$(cat)
prompt=$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null)

if [[ -z "$prompt" ]]; then
  exit 0
fi

verb_re='(implement|build|write|create|add|refactor|introduce|develop)'
noun_re='(endpoint|route|router|DAG|dag|table|migration|schema|page|component|feature|workflow|service|collection|agent|hook)'

if printf '%s' "$prompt" | grep -qiE "$verb_re.*$noun_re"; then
  cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Plan-gate reminder: CLAUDE.md §Implementation Workflow requires Plan mode as the start of the plan → approve → generate → evaluate workflow, which should be applied to changes that touch >3 files, >60 lines, or introduce a new API endpoint, DB table/column, pgvector collection, or Airflow DAG. Enter Plan mode before writing code unless all skip-plan criteria are met."
  }
}
JSON
fi

exit 0
