#!/usr/bin/env bash
# PreToolUse hook: route every `git commit` invocation through the
# permission prompt so the user's explicit click is the approval
# signal — prevents Claude from committing autonomously even when
# auto-mode or a broad Bash allow rule would otherwise pass.
#
# Also surfaces the CLAUDE.md commit convention (max 15 body lines,
# max 100 chars per line) and a quick stats line so the user can
# spot violations before clicking approve.
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

# Match `git commit` anywhere in the command, but only when preceded by
# a command separator (start-of-string, `;`, `&`, `|`, `(`) or whitespace
# immediately after one. This catches chained forms like
# `git diff && git commit ...` or `git add -A; git commit ...` while still
# rejecting accidental substring mentions like `echo "git commit"` or
# `grep "git commit"` where the preceding char is a quote.
commit_re='(^|[;&|()])[[:space:]]*git[[:space:]]+commit([[:space:]]|$)'
if ! printf '%s' "$cmd" | grep -qE "$commit_re"; then
  exit 0
fi

# Extract message body. Two common forms Claude writes:
#  1. heredoc: git commit -m "$(cat <<'EOF' ... EOF )"
#  2. inline:  git commit -m "subject"
msg=""
if printf '%s' "$cmd" | grep -qE "<<'?EOF'?"; then
  msg=$(printf '%s' "$cmd" | awk '
    /<<'\''?EOF'\''?/ { capture=1; next }
    capture && /^[[:space:]]*EOF[[:space:]]*$/ { capture=0; next }
    capture { print }
  ')
fi
if [[ -z "$msg" ]]; then
  msg=$(printf '%s' "$cmd" | sed -nE 's/.*-m[[:space:]]+"([^"]+)".*/\1/p' | head -1)
fi
if [[ -z "$msg" ]]; then
  msg=$(printf '%s' "$cmd" | sed -nE "s/.*-m[[:space:]]+'([^']+)'.*/\1/p" | head -1)
fi

stats=""
if [[ -n "$msg" ]]; then
  total_lines=$(printf '%s\n' "$msg" | grep -c '^')
  body_lines=$(( total_lines > 2 ? total_lines - 2 : 0 ))
  max_len=$(printf '%s\n' "$msg" | awk '{ if (length > m) m = length } END { print m+0 }')
  long_lines=$(printf '%s\n' "$msg" | awk 'length > 100 { print NR }' | paste -sd, -)
  stats="Parsed message: ${total_lines} line(s) total (~${body_lines} body), longest ${max_len} chars."
  warn=""
  [[ -n "$long_lines" ]] && warn+=" lines >100 chars: ${long_lines};"
  (( body_lines > 15 )) && warn+=" body exceeds 15 lines;"
  [[ -n "$warn" ]] && stats="${stats} WARN${warn}"
fi

reason="git commit requires explicit user approval. Refusing autonomous commits — user must confirm via the permission prompt.

CLAUDE.md commit convention:
- Conventional Commits: <type>: <subject>
- Body optional; max 15 lines, max 100 chars per line
- Base message on actual git diff output"

if [[ -n "$stats" ]]; then
  reason="${reason}

${stats}"
fi

jq -n --arg reason "$reason" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: $reason
  }
}'
