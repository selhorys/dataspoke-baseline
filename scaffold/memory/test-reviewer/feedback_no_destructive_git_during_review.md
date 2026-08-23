---
name: no-destructive-git-during-review
description: Never run git checkout/restore/stash on working-tree files during a review — generator deliverables are uncommitted. Promoted to scaffold/roles/test-reviewer.md §Before reviewing.
metadata:
  type: feedback
---

The standing rule now lives in `scaffold/roles/test-reviewer.md` §Before reviewing (step 7): never
run `git checkout`/`restore`/`stash`/`reset` on files in scope; back up with `cp` once at the
start, restore with `cp`, verify with `git diff --stat`.

**Incident that surfaced it:** during the spec-remediation review, `git checkout` "restoring"
after a mutation test wiped uncommitted backend fixes (`le=10000` bound, POST→201) that HEAD
lacked, briefly making a real fix look missing.
