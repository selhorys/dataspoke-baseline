Resolve the merge conflicts between `origin/{base}` and the PR branch `{branch}` for GitHub
issue #{number} ("{pr_title}").

## Context

The executor started `git merge --no-ff --no-commit origin/{base}` in this worktree, and git
could not merge these files on its own:

```
{conflicted_files}
```

`HEAD` is the PR branch, and `MERGE_HEAD` is `origin/{base}`. Every other file has already been
merged by git, and those results are final.

When you finish, the executor accepts your work only if all of the following hold:
- the merge is still in progress and `HEAD` has not moved
- no path is left unmerged
- the index entry of every file outside the list above is exactly what git merged
- no resolved file contains a conflict marker that neither side had

It then commits the merge, pushes it, and reruns the full post-PR regression. A human must
approve the merged head before the PR can be finalized.

## Implementation Plan (the PR's intent)

{plan}

## Instructions

1. For each conflicted file, work out what each side changed and why. Use
   `git diff MERGE_HEAD...HEAD -- <file>` for the PR's change,
   `git diff HEAD...MERGE_HEAD -- <file>` for the base's change, `git show HEAD:<file>` /
   `git show MERGE_HEAD:<file>` for each side's full version, and `git log` for the commit
   messages.
2. Edit each conflicted file so that **both intents survive**. Keep the base branch's changes,
   and re-apply the PR's own change on top of them. Do not take one side wholesale unless the
   other side's change is genuinely superseded, and say so in your summary when it is.
3. Remove every conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`) that the merge inserted.
4. Mark each file resolved: `git add <file>`. For a modify/delete conflict, either keep the
   file and `git add` it, or delete it with `git rm <file>`. Say which in your summary.
5. Change **only** the conflicted files listed above. Do not edit, stage, or create any other
   file, even if a test now fails because of the merge. The post-PR regression and its fix
   session handle that.
6. Run the unit tests and formatters relevant to the files you touched (ruff for Python,
   npx prettier for TypeScript), and re-stage a conflicted file that a formatter changed.
7. Do **not** commit, abort or restart the merge, check out other revisions, change git
   configuration, or push. The executor verifies and commits.
8. If a conflict cannot be resolved without a human decision, leave that file unresolved (do
   not stage it) and explain why. The executor then restores the branch and asks a human.

## Response

End with a short summary, one line per conflicted file, saying how the two sides were combined.
The executor publishes this summary as a PR comment (after redacting secrets).
