---
name: healthcheck-unattended-lock-prompt
description: health-check.sh lock-prompt facts — the read is EOF-guarded so it does NOT abort, it falls through to a counted failure and exit 1; and --profile prod can never exit 0
metadata:
  type: project
---

Two independent traps in `helm-charts/bin/health-check.sh`.

**1. The lock prompt.** With neither `--keep-lock` nor `--force-release` the script prints
"Release this lock? [y/N]" and runs `read -r answer || answer=""` whenever the dev-env lock is
held by any owner.

**Why:** the `|| answer=""` guard means EOF stdin does *not* abort the run (an earlier reading of
this memory said it did — wrong, the guard predates the 2026-08-15 rework). The empty answer
fails the `^[Yy]$` test, so the script `_fail`s, bumps FAILURES and reaches the summary at
**exit 1**. Unattended callers therefore see a *legitimate* red verdict for a lock, not a crash.
With a TTY it blocks; if the caller wrapped it in `out=$(... 2>&1)` the prompt is swallowed and it
looks like a hang. If stdin ever delivers `y`, it releases *another owner's* lock. `--keep-lock`
downgrades a held lock to an info line with no FAILURES bump.

That downgrade is a trade, not a free win: `.prauto` wants it (it meets a held lock through its
own 409-skip), but the integration-test preflight hook deliberately does **not** pass it unless
the command carries the `DATASPOKE_DEV_LOCK_PREACQUIRED=1` prefix — a stale lock silently makes
pytest skip everything and exit 0 ([[project_integration_lock_stale_skip]]), so the hook wants
that to block.

**2. `--profile prod` can never exit 0.** The DataHub, dummy-data and dev-lock probes at the
bottom of the run list are called unconditionally with no profile gate, so on a perfectly healthy
prod deployment they all `_fail`, the summary prints "N service(s) unhealthy", the hint printed is
the dev-only `install.sh --profile dev --components <name>`, and the script `exit 1`s.

**How to apply:** for any non-interactive caller, ask which of the two behaviours it *wants*
before flagging a missing `--keep-lock`; and flag any doc or skill that advertises
`--profile prod` as a first-class command without saying the non-zero exit and the dev reinstall
hint are expected there. Prod exit path verified 2026-08-05 (issue #144 part 2d); lock read guard
re-verified against HEAD 2026-08-15. See also [[health-check-exit2-tool-gap]].
