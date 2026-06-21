#!/usr/bin/env bash
# Statusline: model · effort · context-usage · cwd · git-branch · 5-hour usage window
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

# Context usage — tokens in the current window, "ctx Xk (Y%)".
# Native stdin fields (Claude Code >= 2.1.132): total_input_tokens is the live
# context size (input + cache read + cache write); used_percentage is
# pre-computed against context_window_size. Both 0/null before the first API
# response — omit the segment then.
ctx=""
read -r used pct < <(printf '%s' "$input" | jq -r '
  [ (.context_window.total_input_tokens // 0),
    (.context_window.used_percentage   // 0) ] | @tsv')
if [[ -n "${used:-}" && "$used" -gt 0 ]] 2>/dev/null; then
  ctx=$(awk -v u="$used" -v p="$pct" 'BEGIN{
    if (u >= 1000) printf "ctx %.0fk (%d%%)", u/1000, p+0.5;
    else           printf "ctx %d (%d%%)", u, p+0.5;
  }')
fi

# 5-hour usage window — accurate server-side reset, "5h 23%, reset in 3h 22m".
# Native stdin fields (Claude Code, Claude.ai Pro/Max): rate_limits.five_hour
# carries used_percentage (0-100) and resets_at (unix epoch seconds). Absent on
# free tier and before the first API response — omit the segment then. This
# replaces the ccusage estimate, which inferred the window from local token logs.
reset=""
read -r resets_at limit_pct < <(printf '%s' "$input" | jq -r '
  [ (.rate_limits.five_hour.resets_at      // empty),
    (.rate_limits.five_hour.used_percentage // empty) ] | @tsv')
if [[ -n "${resets_at:-}" ]]; then
  now_epoch=$(date +%s)
  if (( resets_at > now_epoch )); then
    rem=$(( (resets_at - now_epoch) / 60 ))
    h=$(( rem / 60 ))
    m=$(( rem % 60 ))
    if (( h > 0 )); then reset="reset in ${h}h ${m}m"; else reset="reset in ${m}m"; fi
    if [[ -n "${limit_pct:-}" ]]; then
      reset=$(awk -v p="$limit_pct" -v r="$reset" 'BEGIN{ printf "5h %d%%, %s", p+0.5, r }')
    fi
  fi
fi

segments=()

[[ -n "$model" ]] && segments+=("$model")

[[ -n "$effort" ]] && segments+=("$effort")

[[ -n "$ctx" ]] && segments+=("$ctx")

[[ -n "$cwd" ]] && segments+=("$(basename "$cwd")")

if [[ -n "$cwd" ]]; then
  branch=$(git -C "$cwd" branch --show-current 2>/dev/null)
  [[ -n "$branch" ]] && segments+=("$branch")
fi

[[ -n "$reset" ]] && segments+=("$reset")

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
