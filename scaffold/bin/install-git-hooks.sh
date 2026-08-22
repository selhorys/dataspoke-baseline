#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
hook_path="$repo_root/.githooks/pre-commit"

if [ ! -x "$hook_path" ]; then
  printf '%s\n' "error: expected executable hook at $hook_path" >&2
  exit 1
fi

configured=$(git -C "$repo_root" config --local --get core.hooksPath || true)
if [ -n "$configured" ] && [ "$configured" != ".githooks" ]; then
  printf '%s\n' \
    "error: core.hooksPath is already '$configured'; refusing to overwrite it" >&2
  exit 1
fi

git -C "$repo_root" config --local core.hooksPath .githooks
printf '%s\n' "Configured core.hooksPath=.githooks for $repo_root"
