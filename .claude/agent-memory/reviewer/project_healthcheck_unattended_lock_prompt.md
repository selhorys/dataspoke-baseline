---
name: healthcheck-unattended-lock-prompt
description: health-check.sh gotchas — the interactive lock prompt aborts unattended callers, and --profile prod always exits 1 with a dev-only reinstall hint
metadata:
  type: project
---

Two independent traps in `helm-charts/bin/health-check.sh`.

**1. The lock prompt.** With neither `--keep-lock` nor `--force-release` the script hits an
interactive `read -r answer` ("Release this lock? [y/N]") whenever the dev-env lock is held by
any owner. Unattended callers must pass `--keep-lock`.

**Why:** the script runs `set -euo pipefail`, so on EOF stdin (cron/launchd) `read` returns 1 and
aborts the whole script *before* the FAILURES summary — caller sees exit 1 and reads it as "cluster
red" regardless of actual health. With a TTY it blocks; if the caller wrapped it in `out=$(... 2>&1)`
the prompt text is swallowed, so it looks like a silent hang. If stdin ever delivers `y`, it
releases *another owner's* lock. With `--keep-lock` a held lock is just an info line (no FAILURES
bump), so the caller proceeds to its own acquire and gets a clean 409 → skip.

**2. `--profile prod` can never exit 0.** The DataHub, dummy-data and dev-lock probes at the
bottom of the run list are called unconditionally with no profile gate, so on a perfectly healthy
prod deployment they all `_fail`, the summary prints "N service(s) unhealthy", the hint printed is
the dev-only `install.sh --profile dev --components <name>`, and the script `exit 1`s. The prod
lock probe never reaches the prompt above (its TCP check fails first and returns early).

**How to apply:** flag any non-interactive caller of health-check.sh (prauto phases, CI, hooks,
workflow scripts) that omits `--keep-lock`; and flag any doc or skill that advertises
`--profile prod` as a first-class command without saying the non-zero exit and the dev reinstall
hint are expected there. Lock behaviour verified 2026-07-17 (prauto #62); prod exit path verified
2026-08-05 (issue #144 part 2d). Related: [[project_integration_lock_stale_skip]].
