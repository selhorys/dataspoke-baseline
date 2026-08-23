---
name: trap-int-term-swallows-ctrl-c
description: A cleanup trap installed on EXIT INT TERM that does not exit makes the script SURVIVE Ctrl-C and finish with exit 0; EXIT alone already fires on SIGINT
metadata:
  type: feedback
---

When a script writes `trap 'cleanup' EXIT INT TERM`, do not read the extra
signals as belt-and-braces — reproduce them. A non-exiting INT handler
*replaces* bash's default die-on-SIGINT, so the handler runs and execution
resumes at the next command. The run continues past the operator's Ctrl-C and
can print its success banner and exit 0.

**Why:** verified on this machine (bash 3.2, group SIGINT via
`os.killpg`, mirroring a terminal Ctrl-C):

| traps | observed |
|---|---|
| `EXIT INT TERM`, handler does not exit | handler ran, script **continued**, exit 0 |
| `EXIT` only | handler ran, script died, exit `-2` (killed by SIGINT) |
| `EXIT` + `trap 'exit 130' INT` | handler ran, exit 130 |

So the usual rationale in the code comment — "trap INT too, or the EXIT trap
won't run on Ctrl-C" — is false: EXIT alone already runs. Trapping INT without
exiting only removes the operator's ability to stop the run. Worse for a
cleanup handler that frees a resource the rest of the script still uses (a
credential temp file, a lock dir): it is released mid-flight while the script
keeps going.

**How to apply:** any script with a signal list on `trap`. Required fix shape
is `trap 'cleanup' EXIT` plus `trap 'exit 130' INT` / `trap 'exit 143' TERM`
(the EXIT trap then fires on the way out). Reproduce with a `sleep` stand-in
and `start_new_session=True` + `killpg` rather than `kill -INT <pid>` — a
single-pid SIGINT does not reproduce the terminal case.
Related: [[isolate-failures-concurrent-edit]].
