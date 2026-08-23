---
name: bash-errexit-grep-output
description: Bash tool aborts at first non-zero exit; grep/rg no-match (exit 1) swallows output. Promoted to scaffold/roles/reviewer.md §Before reviewing.
metadata:
  type: feedback
---

The standing rule now lives in `scaffold/roles/reviewer.md` §Before reviewing (step 6): treat a
bare `grep`/`rg` exit code of 1 as "no matches found", not a tool failure.

**Incident that surfaced it:** during the dry_run-query-param review, multi-grep scripts kept
dying after the first no-match grep, returning "Exit code 1" with no visible output, making a
clean (no dangling refs) result look like a failure and costing several turns.
