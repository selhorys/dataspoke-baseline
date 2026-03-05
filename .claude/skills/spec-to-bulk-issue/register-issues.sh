#!/usr/bin/env bash
# register-issues.sh — Register issue markdown files to GitHub with prauto:ready label.
#
# Usage:
#   register-issues.sh <file> [<file> ...]
#   register-issues.sh issues/02_postgresql-models-migrations.md issues/03_infrastructure-clients.md
#
# Each file must follow the issues/ markdown format:
#   Line 1: # Title
#   Line 3: the actual title text
#   A line containing exactly "# Body"
#   Everything after that line is the issue body.
#
# Requires: gh CLI authenticated with repo scope.

set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 <issue-file> [<issue-file> ...]" >&2
  exit 1
fi

# Verify gh auth
if ! gh auth status &>/dev/null; then
  echo "ERROR: gh auth failed. Run 'gh auth login' first." >&2
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner')
if [[ -z "$REPO" ]]; then
  echo "ERROR: could not determine repo name." >&2
  exit 1
fi

echo "Repo: $REPO"
echo ""

# Print header
printf "%-50s %-8s %s\n" "File" "Issue#" "URL"
printf "%-50s %-8s %s\n" "----" "------" "---"

for FILE in "$@"; do
  if [[ ! -f "$FILE" ]]; then
    echo "SKIP: $FILE (not found)" >&2
    continue
  fi

  # Extract title (line 3)
  TITLE=$(sed -n '3p' "$FILE")

  # Extract body (everything after the "# Body" line)
  BODY=$(sed -n '/^# Body$/,$ { /^# Body$/d; p; }' "$FILE")

  if [[ -z "$TITLE" ]]; then
    echo "SKIP: $FILE (no title on line 3)" >&2
    continue
  fi

  URL=$(gh issue create \
    --repo "$REPO" \
    --title "$TITLE" \
    --label "prauto:ready" \
    --body "$BODY" 2>&1)

  # Extract issue number from URL
  ISSUE_NUM=$(echo "$URL" | grep -oE '[0-9]+$' || echo "?")

  printf "%-50s %-8s %s\n" "$(basename "$FILE")" "#$ISSUE_NUM" "$URL"
done
