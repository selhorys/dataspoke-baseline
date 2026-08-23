---
name: scratchpad-shared-with-parallel-agents
description: The session scratchpad is shared with the generator and any parallel reviewer — namespace every harness file per-run. Promoted to scaffold/roles/reviewer.md §Reviewer calibration.
metadata:
  type: feedback
---

The standing rule now lives in `scaffold/roles/reviewer.md` §Reviewer calibration: namespace any
scratchpad harness file per-run (`$SCRATCHPAD/rvw-$$/…`), never a generic path.

**Incident that surfaced it:** on issue #144 commit 1, a stub `kubectl` written at
`$SCRATCHPAD/stub/kubectl` was overwritten by the `k8s-helm` generator's own parallel test
artifacts between two Bash calls — seed scripts still exited 0, but the recording landed under
someone else's filenames and the extraction read an empty directory.

Related: [[isolate-failures-concurrent-edit]].
