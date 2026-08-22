#!/usr/bin/env bash
# Explicit ruff-check helper for any coding-agent role's self-verification step. It is a normal
# command, not a lifecycle hook: exit 0 on a clean
# check (or missing tooling), exit 1 with ruff's output on stderr otherwise.
#
# Usage: scaffold/bin/lint-python.sh <file.py>

set -euo pipefail

file="${1:?usage: lint-python.sh <file.py>}"

case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac

[[ -f "$file" ]] || exit 0

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v uv >/dev/null 2>&1 || exit 0

if ! output=$(cd "$repo_root" && uv run ruff check "$file" 2>&1); then
  printf 'ruff check failed for %s — fix before continuing:\n%s\n' "$file" "$output" >&2
  exit 1
fi
