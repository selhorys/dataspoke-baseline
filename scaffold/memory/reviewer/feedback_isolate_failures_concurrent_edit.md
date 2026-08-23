---
name: isolate-failures-concurrent-edit
description: Attribute test failures to one file when a concurrent agent edits another — extract HEAD with git archive into scratchpad, overlay one file at a time
metadata:
  type: feedback
---

When reviewing a change while another agent concurrently edits a shared file (e.g. `spec/API.md`),
never attribute working-tree test failures to the reviewed file by inspection. Build isolated
scenario copies and run the suite in each:

```
git archive HEAD | tar -x -C <scratchpad>/base      # repeat per scenario
cp <worktree>/<reviewed-file> <scratchpad>/only_a/<reviewed-file>
( cd <scratchpad>/only_a && <repo>/.venv/bin/python -m pytest <suite> -q -p no:cacheprovider )
```

**Why:** on issue #86 phase 4 the generator predicted 6 failures and the working tree showed 7.
Isolation proved the reviewed file caused exactly 6 and the concurrent `spec/API.md` edit caused 3
(overlapping set) — without it the extra failure looks like a regression in the reviewed change.
`git archive` is read-only on the repo, unlike `git stash` / `git worktree add`.

**How to apply:** works whenever the tests resolve paths from `__file__` rather than cwd (this repo's
`tests/unit/spec_conformance/_api_md.py` sets `REPO_ROOT = Path(__file__).resolve().parents[3]`).
Also run the `base` scenario to confirm HEAD is green — that is what makes the delta meaningful.
Related: [[verify-generator-dead-code-claims]], [[verify-branch-reachability-rationales]].
