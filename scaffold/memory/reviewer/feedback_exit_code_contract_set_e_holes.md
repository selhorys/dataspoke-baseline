---
name: exit-code-contract-set-e-holes
description: An "every abort in this region exits N" contract implemented via an error() override still leaks set -e aborts (source, mktemp, cd) at the default code — and on bash 3.2 `source X || handler` does not even catch a MISSING file
metadata:
  type: feedback
---

When a script claims a whole region has one exit code ("every pre-probe abort exits 2"),
do NOT audit only the sites that call the shared `error()` helper. Under `set -euo pipefail`
any *other* failing command in that region exits with its own status, bypassing the override
entirely.

**Why:** `helm-charts/bin/health-check.sh` routes exit 2 through
`error() { ...; exit "${DATASPOKE_ERROR_EXIT_CODE:-1}"; }`. Every `error()` site was verified
at 2 — but `[[ ! -f "$ENV_FILE" ]]` passes for an *existing, unreadable* file, and the
following `source "$ENV_FILE"` then dies under `set -e` at **exit 1**. The script's own usage
header and `spec/feature/HELM_CHART.md` both named "unreadable env file" as a 2.
`.prauto/lib/phases.sh` also names it a 2 in its docstring, and its `dev_env_healthy` reads a
bare 1 as cluster evidence and calls `provision_dev_env` — the exact failure the 1-vs-2 split
exists to prevent. The current health-check consumer is `dev_env_healthy` in
`.prauto/lib/phases.sh`; re-verify the 1-vs-2 contract there before re-citing this rationale.

**`source X || { ...; exit 2; }` is NOT a fix.** Measured on this machine (bash 3.2.57,
the only bash on stock macOS, which `#!/usr/bin/env bash` resolves to) with `set -e`:

| case | handler ran? | exit |
|---|---|---|
| file missing | **no** | 1 |
| file unreadable (chmod 000) | **no** | 1 |
| failing command inside the file | **no** | 1 |
| syntax error inside the file | no | 2 (bash's own status, coincidence) |
| same, without `set -e` | yes | 2 |

So both the intended exit code *and* the handler's diagnostic are lost; the operator sees
only bash's own `No such file or directory`. The working shape is the one the env-file path
already uses: `set +e; source "$F"; rc=$?; set -e` (plus a separate `-r` guard), or a
`[[ -r ... ]] ||` pre-check.

**How to apply:** enumerate the region's non-`error()` abort sites and drive each one:
`source` (all four failure modes above), `mktemp`, `chmod`, `cd "$(...)"`, and any bare
command substitution. `-f` vs `-r` is the recurring guard bug. Assignments are safe:
`X="$(f)"` and `Y="pre$(f)"` both propagate f's own status under `set -e` (measured).
See also [[verify-branch-reachability-rationales]] and [[dropped-preflight-reroutes-status]].
