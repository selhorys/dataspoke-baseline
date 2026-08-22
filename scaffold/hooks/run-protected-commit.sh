#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=$(command -v python3 || true)
if [ -z "$python_bin" ]; then
  printf '%s\n' "Protected-commit hook blocked: python3 is unavailable." >&2
  exit 2
fi

set +e
"$python_bin" "$script_dir/protected-commit.py" "$@"
status=$?
set -e

if [ "$status" -eq 0 ]; then
  exit 0
fi
printf '%s\n' "Protected-commit hook blocked: classifier exited with status $status." >&2
exit 2
