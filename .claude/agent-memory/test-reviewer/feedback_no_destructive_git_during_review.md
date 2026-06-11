---
name: no-destructive-git-during-review
description: Never run git checkout/restore/stash on working-tree files during a review — generator deliverables are uncommitted
metadata:
  type: feedback
---

When reviewing generator output, the deliverables under review live as **uncommitted working-tree changes**, not commits. NEVER run `git checkout <file>`, `git restore`, `git stash`, or `git reset` on files in scope — it silently destroys the exact work being reviewed.

**Why:** During the spec-remediation review I ran mutation tests by editing `src/api/.../validation.py`, then "restored" with `git checkout`. But the backend stage's `le=10000` and POST→201 fixes were uncommitted working-tree edits (HEAD lacked them), so `git checkout` wiped real deliverables and made me briefly believe the backend fix was missing. I had to manually re-apply both fixes to recover the true review state.

**How to apply:**
- To mutation-test an impl, back up with `cp <file> /tmp/bak` and restore with `cp /tmp/bak <file>` — never git.
- Take the cp backup ONCE at the very start, before any edit, so the backup captures the as-delivered (post-generator) state, not a half-mutated one.
- Verify restoration with `git diff --stat <file>` and confirm the expected generator changes are still present (not zeroed out).
- Prefer non-destructive sensitivity reasoning (read the Query/param definition) over live mutation when the impl path is clear; only mutate when you genuinely need empirical proof.
