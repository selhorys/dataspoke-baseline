---
name: feedback-review-method
description: Test-review method for this repo — mutate src and re-run to verify discrimination claims; grep the test tree for citations orphaned by spec edits. Promoted to scaffold/roles/test-reviewer.md §Verification methodology.
metadata:
  type: feedback
---

All 6 techniques this note documented (independent mutation-testing beyond the generator's table,
grep for citations orphaned by a spec edit, static-scan-closes-symbol-swap-only, refuting
"untestable without patching import machinery" claims, frontend rsync-to-scratchpad
mutation-testing under a read-only launch, and Python data-file mutation via a scratchpad mirror)
now live in `scaffold/roles/test-reviewer.md` §Verification methodology.
