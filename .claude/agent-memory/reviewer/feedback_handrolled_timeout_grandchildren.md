---
name: handrolled-timeout-grandchildren
description: A background-and-kill timeout (no timeout(1) on macOS) kills only the forked subshell — the real worker (uv/redis-cli/curl) survives; verify with a probe that records its own pid
metadata:
  type: feedback
---

When a script hand-rolls a wall-clock bound because stock macOS ships no `timeout(1)`
— `cmd & pid=$!; loop; kill -9 "$pid"` — the kill reaches **only the forked subshell**.
Any process that subshell exec'd or spawned keeps running, detached.

**Why:** measured on `helm-charts/bin/health-check.sh`'s `_bounded`. A probe that spawned a
60s sleeper returned rc 124 on schedule, and the sleeper was **still alive** afterwards.
Its `_tcp_check` sibling uses the identical idiom and is fine only because
`( exec 3<>/dev/tcp/... )` has no children — so "same idiom as X" is not evidence.
Consequence there: an interactive run can leak one orphan per stalled probe, each holding
`PGPASSWORD` / `REDISCLI_AUTH` in its environment. The wall-clock contract still holds,
which is why the leak is invisible in a timing test. Callers that wrap the whole script in
`set -m` + `kill -"$pid"` (the preflight hook, `.prauto`'s `run_health_check`) reclaim it;
a bare operator run does not.

**How to apply:** for any hand-rolled bound, drive it with a probe that writes its own pid
to a file and sleeps past the deadline, then `kill -0` that pid after the wrapper returns.
The fix is `set -m` around the `&` plus `kill -TERM -"$pid"` / `kill -9 -"$pid"` on the
process **group**, which the same repo already does two levels up. See also
[[health-check-exit2-tool-gap]] and [[kubectl-unbounded-dial]].
