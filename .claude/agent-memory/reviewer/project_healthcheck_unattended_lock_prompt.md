---
name: healthcheck-unattended-lock-prompt
description: health-check.sh prompts to release a held dev-env lock; unattended callers must pass --keep-lock or they abort/hang
metadata:
  type: project
---

`helm-charts/bin/health-check.sh` with neither `--keep-lock` nor `--force-release` hits an
interactive `read -r answer` ("Release this lock? [y/N]") whenever the dev-env lock is held by
any owner. Unattended callers must pass `--keep-lock`.

**Why:** the script runs `set -euo pipefail`, so on EOF stdin (cron/launchd) `read` returns 1 and
aborts the whole script *before* the FAILURES summary — caller sees exit 1 and reads it as "cluster
red" regardless of actual health. With a TTY it blocks; if the caller wrapped it in `out=$(... 2>&1)`
the prompt text is swallowed, so it looks like a silent hang. If stdin ever delivers `y`, it
releases *another owner's* lock. With `--keep-lock` a held lock is just an info line (no FAILURES
bump), so the caller proceeds to its own acquire and gets a clean 409 → skip.

**How to apply:** flag any non-interactive caller of health-check.sh (prauto phases, CI, hooks,
workflow scripts) that omits `--keep-lock`. Verified empirically 2026-07-17 during the prauto #62
review. Related: [[project_integration_lock_stale_skip]].
