Fix integration test failures for GitHub issue #{number} on branch `{branch}`.

## Integration Test Output

The following integration tests failed:

```
{test_output}
```

## Instructions

1. Read the failing test files and the source code they exercise.
2. Diagnose the root cause of each failure.
3. Fix the source code (not the tests) unless the test itself has a bug.
4. Verify your fixes by running the two integration groups **separately** — never mixed, since a
   combined run puts competing Airflow load on the shared dev cluster and flakes on timing:

   ```bash
   uv run pytest tests/integration/spot/ --tb=short
   uv run pytest tests/integration/api_wired/ --tb=short
   ```

5. Run the static gates as checks and fix what they report — do NOT use `--fix`:

   ```bash
   uv run ruff check src/ tests/
   uv run mypy src/
   ```

   When your fix touches `src/frontend/`, also run from `src/frontend/`:

   ```bash
   npx tsc --noEmit
   npx eslint src/
   ```

   When your fix touches `tests/e2e/`, also run `pnpm -C tests/e2e typecheck`.
6. Stage and commit with a conventional commit message.
   Use: git commit --author="{author_name} <{author_email}>"
7. Do NOT push. The orchestrator handles pushing.
