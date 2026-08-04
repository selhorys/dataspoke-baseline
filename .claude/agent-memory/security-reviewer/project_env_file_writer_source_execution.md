---
name: env-file-writer-source-execution
description: env_file_set_var is the single .env rewriter and writes values UNQUOTED; the newline guard landed but $(...)/backtick/space still execute on the next `source`, and prod adopt now feeds it cluster bytes
metadata:
  type: project
---

`helm-charts/bin/lib/helpers.sh env_file_set_var` is the one rewriter behind
`upsert_env_var`, install.sh's `_write_env_var` / `_sync_env_from_secret`, and
`install-prod-preflight.sh` stage 3 (prod credential populate). It writes with
awk `print prefix value` — **no quoting of the value**.

**Closed:** the newline/CR rejection now exists (`*$'\n'*` / `*$'\r'*` → hard
error), with the `source`-executes rationale written out. That was the earlier
finding; don't re-report it.

**Still open, measured 2026-08-05:** writing `p$(id -un > PWNED)w` produced the
literal line `DATASPOKE_PROD_POSTGRES_PASSWORD=p$(id -un > PWNED)w`; sourcing it
**executed the substitution** and left the variable as `pw` (silent credential
corruption on top of RCE). `a b#c$HOME` + backticks likewise splits into a
command. So the guard is newline-only; every other shell metacharacter passes.

**Why it matters more now:** the prod populate adopts each of the eleven keys
out of the live credentials Secret and writes it back verbatim. `install.sh:188`
sources the same file unconditionally, and it is the *second* command of the
documented two-command prod sequence — so cluster-Secret bytes reach the
operator's shell with their kubeconfig. `report_credential_secret_drift`'s awk
already strips one matched pair of surrounding quotes, so single-quoting the
written value (`'` → `'\''`) is the compatible fix on the read side; its awk
does **not** un-escape `'\''` yet.

**How to apply:** on any diff touching this function or adding a writer/populate
caller, require shell-safe quoting on write plus a symmetric unquote on every
reader. Related: [[env-to-sed-helm-interpolation-boundary]],
[[credential-uri-escaping-boundary]], [[prod-preflight-credential-plane]].
