Implement changes for GitHub issue #{number} on branch `{branch}`.

## Check for existing work

Before starting, check if prior work exists on this branch:

```bash
git log --oneline origin/{base_branch}..HEAD
```

If commits exist, continue from where they left off — do NOT redo completed work. Read the existing commits and code to understand what has been done, then pick up the remaining tasks from the plan.

## Instructions

1. Follow the implementation plan from the analysis phase (provided below).
2. Read relevant specs before writing code.
3. Follow existing code patterns.
4. Write tests for your changes.
5. Run tests to verify (pytest for Python, npx tsc for TypeScript).
6. Run formatters (ruff for Python, npx prettier for TypeScript).
7. Stage and commit with conventional commit messages.
   Use: git commit --author="{author_name} <{author_email}>"
8. Do NOT push. The orchestrator handles pushing.

## Analysis Output

{analysis_output}
