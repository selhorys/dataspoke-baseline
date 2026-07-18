Fix end-to-end (Playwright) test failures for GitHub issue #{number} on branch `{branch}`.

## E2E Test Output

The following E2E tests failed:

```
{test_output}
```

## Context

These are browser-driven Playwright tests in `tests/e2e/`, run against the branch's UI deployed to
the dev cluster. The frontend has already been built and deployed from this branch's source, so the
failures reflect the code as it currently stands on the branch.

You cannot deploy, and you cannot reach the cluster: `kubectl` and `helm` are not available to you.
Do not attempt to redeploy, restart pods, or re-run the suite against a new build — the orchestrator
redeploys and re-runs the suite after you commit.

## Instructions

1. Read the failing spec files under `tests/e2e/` and the UI code they drive in `src/frontend/`.
2. Diagnose the root cause of each failure. A failing selector or timeout is as likely to be a real
   UI regression as a test bug — decide which from the code, and note that a passing assertion
   against the wrong element is worse than a failing one.
3. Fix the source code (not the tests) unless the test itself has a bug.
4. Verify what you can without a cluster — the E2E suite itself needs one, so do not run it:

   ```bash
   pnpm -C src/frontend test
   pnpm -C tests/e2e typecheck
   ```

5. When your change touches `src/frontend/`, run these gates from `src/frontend/` as checks and fix
   what they report:

   ```bash
   npx tsc --noEmit
   npx eslint src/
   ```

6. Stage and commit with a conventional commit message.
   Use: git commit --author="{author_name} <{author_email}>"
7. Do NOT push. The orchestrator handles pushing.
