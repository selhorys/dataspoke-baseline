#!/usr/bin/env bash
# PreToolUse hook: route every `git commit` invocation through the
# permission prompt so the user's explicit click is the approval
# signal — prevents Claude from committing autonomously even when
# auto-mode or a broad Bash allow rule would otherwise pass.
#
# settings.json `if` clauses are not honored by Claude Code's hook
# schema, so filter inside the script — otherwise this hook would
# emit "ask" for every Bash call.
#
# Output contract: stdout is JSON consumed by Claude Code. Silent
# exit (no stdout) passes the tool call through unchanged.

set -u

input=$(cat)
tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)

if [[ "$tool_name" != "Bash" ]]; then
  exit 0
fi

# Anchor at start-of-command (after optional leading whitespace).
# Match only literal `git commit` invocations; commands that merely mention
# the string (e.g., `echo "git commit"`, `grep "git commit"`) should not trigger.
commit_re='^[[:space:]]*git[[:space:]]+commit([[:space:]]|$)'
if ! printf '%s' "$cmd" | grep -qE "$commit_re"; then
  exit 0
fi

cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "git commit requires explicit user approval. Refusing autonomous commits — user must confirm via the permission prompt."
  }
}
JSON
