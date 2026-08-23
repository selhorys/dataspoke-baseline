---
name: documented-verdict-never-printed
description: Docs that enumerate literal status tokens — grep whether those tokens actually reach the reader, and whether the "failure" ones abort the caller
metadata:
  type: feedback
---

When a doc tells the operator (or an agent) to "read the verdict" and lists literal tokens, verify
two things in the script before accepting it: (a) the token reaches the reader's terminal, and
(b) what the caller does when it is a failure token.

**Why:** issue #144 part 2d documented `seed-admin-user.sh` as "printing a one-word verdict:
`ROTATED` / `ALREADY_ROTATED` / `NO_KNOWN_PASSWORD` / `PATCH_FAILED_<code>` / `VERIFY_FAILED` /
`UNREACHABLE`". Those tokens are an *internal* protocol: the in-pod python prints them to stdout,
the shell captures them with `verdict="$(kubectl exec ...)"`, and a `case` translates each one into
prose — only the unrecognised-token branch echoes the raw word. So a reader grepping the install
output for `ROTATED` finds nothing. Worse, three of the four failure tokens route to `error()`
(`exit 1` in `bin/lib/helpers.sh`), and `install.sh` invokes the seed unguarded under `set -e`, so
those verdicts kill the install before its completion summary — the opposite of the documented
"the install finished; now read the verdict and continue".

**How to apply:** on any doc/skill diff enumerating status tokens, grep the emitting script for the
token and confirm it is on an output path rather than only in a `case`/`if` arm; then trace each
failure arm's exit status up through its caller. Same test for exit codes and log-line formats a
doc tells someone to match on. Related: [[verify-branch-reachability-rationales]].
