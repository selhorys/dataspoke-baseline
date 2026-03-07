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
4. Run `uv run pytest tests/integration/ --tb=short` to verify your fixes.
5. Run `uv run ruff check --fix .` to format.
6. Stage and commit with a conventional commit message.
   Use: git commit --author="{author_name} <{author_email}>"
7. Do NOT push. The orchestrator handles pushing.
