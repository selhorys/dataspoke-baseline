#!/usr/bin/env bash
# PreToolUse hook: route every `git commit` invocation through the
# permission prompt so the user's explicit click is the approval
# signal — prevents Claude from committing autonomously even when
# auto-mode or a broad Bash allow rule would otherwise pass.
#
# Filter at the hook level (`if: "Bash(git commit *)"`) keeps this
# hook from running on every Bash call — only commits pay the fork.
#
# Output contract: stdout is JSON consumed by Claude Code. Keep this
# script side-effect-free and deterministic.

cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "git commit requires explicit user approval. Refusing autonomous commits — user must confirm via the permission prompt."
  }
}
JSON
