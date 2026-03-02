Generate a single conventional commit message for the following squash commit.

## Issue #{issue_number}: {issue_title}

{issue_body}

## Changed files

{diff_stat}

## Diff (truncated)

{diff}

## Rules

1. First line: `<type>: <subject>` — conventional commit format (feat, fix, docs, refactor, etc.). **Max 100 characters** — shorten the subject if needed.
2. Second line: blank
3. Body: brief description of what was done (max 5 lines). Focus on the "why" and key changes.
4. Append `(issue #{issue_number}, PR #{pr_number})` at the end of the last body sentence — no blank line before it.
5. Output ONLY the raw commit message text. No markdown fences, no explanations.
