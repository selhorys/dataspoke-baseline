---
name: scratchpad-shared-with-parallel-agents
description: The session scratchpad is shared with the generator and any parallel reviewer — namespace every harness file per-run or another agent silently overwrites your stub mid-experiment
metadata:
  type: feedback
---

Write review harnesses into a uniquely-named subdirectory (`$SCRATCHPAD/rvw-$$/…`), never into
generic paths like `$SCRATCHPAD/stub/kubectl` or `$SCRATCHPAD/rec/`.

**Why:** on issue #144 commit 1 the `k8s-helm` generator's own test artifacts (`t.sh`, `stub/`,
`stub2/`, `rec-*`) were already sitting in the scratchpad, and `security-reviewer` was running in
parallel against the same paths. A stub `kubectl` written at `$SCRATCHPAD/stub/kubectl` was replaced
between two Bash calls — the seed scripts still exited 0 and printed `OK (HTTP 200)`, but the
recordings landed under someone else's filenames and my extraction read an empty directory. The
failure mode is silent and looks like "the script made no request", which is exactly the kind of
wrong conclusion a review must not reach.

**How to apply:** any time a review needs an on-PATH fake binary, a recorder, or A/B run outputs.
Pin the directory once, echo it, and re-derive it in later calls from a file rather than a variable.
Reading the generator's leftover artifacts is fine — trusting that they are still yours is not.
Related: [[feedback_isolate_failures_concurrent_edit]].
