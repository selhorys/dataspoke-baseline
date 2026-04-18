#!/usr/bin/env bash
# Statusline: model · effort · cwd · git-branch · 5-hour block reset
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

# ccusage 5-hour block reset countdown — compact "reset in Xh Ym".
# Usage % is intentionally omitted: claude.ai computes it on a cost-weighted
# budget that doesn't align with ccusage's raw token total.
# Silent on any failure so the statusline keeps rendering.
# Install once: `npm i -g ccusage` (needs node >= 18) or `brew install bun && bun add -g ccusage`.
reset=""
blocks_json=""
if command -v ccusage >/dev/null 2>&1; then
  blocks_json=$(ccusage blocks --active --json 2>/dev/null || true)
elif command -v bunx >/dev/null 2>&1; then
  blocks_json=$(bunx ccusage@latest blocks --active --json 2>/dev/null || true)
fi

if [[ -n "$blocks_json" ]]; then
  end_iso=$(printf '%s' "$blocks_json" | jq -r '.blocks[0].endTime // empty' 2>/dev/null)
  if [[ -n "$end_iso" ]]; then
    # Parse the ISO8601 endTime as UTC (macOS date -j treats bare strings as local time).
    end_epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "${end_iso%.*}" +%s 2>/dev/null \
                || date -d "$end_iso" +%s 2>/dev/null)
    now_epoch=$(date +%s)
    if [[ -n "$end_epoch" ]] && (( end_epoch > now_epoch )); then
      rem=$(( (end_epoch - now_epoch) / 60 ))
      h=$(( rem / 60 ))
      m=$(( rem % 60 ))
      if (( h > 0 )); then
        reset="reset in ${h}h ${m}m"
      else
        reset="reset in ${m}m"
      fi
    fi
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
