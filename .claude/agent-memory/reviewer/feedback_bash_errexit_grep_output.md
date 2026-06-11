---
name: bash-errexit-grep-output
description: Bash tool aborts at first non-zero exit; grep/rg no-match (exit 1) swallows output — pipe to cat or redirect to file
metadata:
  type: feedback
---

The Bash tool in this environment surfaces any non-zero exit as a hard error and
suppresses stdout, and the login shell runs with errexit so compound commands
abort at the first failing sub-command. A `grep`/`rg` that finds **no matches**
returns exit 1 — which both hides its (empty) output AND aborts the rest of a
`&&`/`;`/loop script, even with a trailing `|| true` or `; true`.

**Why:** During the dry_run-query-param review I lost several turns: multi-grep
scripts kept dying after the first no-match grep, returning "Exit code 1" with no
visible output, making it look like the command failed when it had actually run
clean (no dangling refs = the good result).

**How to apply:** When grepping for dangling references / dead code where "no
match" is the expected pass case:
- Pipe each grep through `cat` or `head` (the pipe's last stage exits 0):
  `rg -n 'pattern' path | cat`
- Or redirect each grep to a temp file and read it with the Read tool, which
  ignores exit codes: `rg -n 'pat' path > /tmp/out.txt` then Read /tmp/out.txt
- Treat a bare grep/rg "Exit code 1" as "no matches found", not as a tool failure.
- Run one grep per Bash call when you need to see each result independently.
