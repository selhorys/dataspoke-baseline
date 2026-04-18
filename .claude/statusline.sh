#!/usr/bin/env bash
# Statusline: model · cwd · git-branch · ccusage 5-hour block
#
# Claude Code feeds session JSON on stdin. We compose a single line of text
# to stdout. Keep it fast — the statusline is re-rendered frequently.

set -u

input=$(cat)

model=$(printf '%s' "$input" | jq -r '.model.display_name // empty')
cwd=$(printf '%s' "$input" | jq -r '.workspace.current_dir // empty')

# effortLevel from Claude Code settings (not exposed on stdin).
# Check project → user scope; first match wins.
effort=""
for f in \
  "$cwd/.claude/settings.local.json" \
  "$cwd/.claude/settings.json" \
  "$HOME/.claude/settings.json"; do
  if [[ -f "$f" ]]; then
    v=$(jq -r '.effortLevel // empty' "$f" 2>/dev/null)
    if [[ -n "$v" ]]; then effort="$v"; break; fi
  fi
done

# ccusage 5-hour block usage — compact "usage X% · reset in Xh Ym".
# TOKEN_LIMIT is a fixed ceiling calibrated against claude.ai's plan-usage %;
# retune if it drifts. Percentage is ccusage's projected-usage / TOKEN_LIMIT.
# Silent on any failure so the statusline keeps rendering.
# Install once: `npm i -g ccusage` (needs node >= 18) or `brew install bun && bun add -g ccusage`.
TOKEN_LIMIT=200000000
usage=""
blocks_json=""
if command -v ccusage >/dev/null 2>&1; then
  blocks_json=$(ccusage blocks --active --json --token-limit "$TOKEN_LIMIT" 2>/dev/null || true)
elif command -v bunx >/dev/null 2>&1; then
  blocks_json=$(bunx ccusage@latest blocks --active --json --token-limit "$TOKEN_LIMIT" 2>/dev/null || true)
fi

if [[ -n "$blocks_json" ]]; then
  pct=$(printf '%s' "$blocks_json" | jq -r '.blocks[0].tokenLimitStatus.percentUsed // empty' 2>/dev/null)
  rem=$(printf '%s' "$blocks_json" | jq -r '.blocks[0].projection.remainingMinutes // empty' 2>/dev/null)
  if [[ -n "$pct" && -n "$rem" ]]; then
    pct_int=$(printf '%.0f' "$pct")
    h=$(( rem / 60 ))
    m=$(( rem % 60 ))
    if (( h > 0 )); then
      reset="${h}h ${m}m"
    else
      reset="${m}m"
    fi
    usage="usage ${pct_int}% · reset in ${reset}"
  fi
fi

segments=()

[[ -n "$model" ]] && segments+=("$model")

[[ -n "$effort" ]] && segments+=("$effort")

[[ -n "$cwd" ]] && segments+=("$(basename "$cwd")")

if [[ -n "$cwd" ]]; then
  branch=$(git -C "$cwd" branch --show-current 2>/dev/null)
  [[ -n "$branch" ]] && segments+=("$branch")
fi

[[ -n "$usage" ]] && segments+=("$usage")

# join with · and wrap the whole line in teal (256-color 30)
TEAL=$'\033[38;5;30m'
RESET=$'\033[0m'
out=""
for i in "${!segments[@]}"; do
  if (( i == 0 )); then
    out="${segments[$i]}"
  else
    out="$out · ${segments[$i]}"
  fi
done

printf '%s%s%s\n' "$TEAL" "$out" "$RESET"
