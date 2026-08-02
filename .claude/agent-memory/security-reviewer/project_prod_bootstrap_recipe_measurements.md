---
name: prod-bootstrap-recipe-measurements
description: helm-charts/README.md §2 credential-bootstrap recipe — the measured shell/kubectl facts that decide whether it leaks, plus the Airflow all_admins auth bypass that makes two of the eleven keys decorative and the Fernet scenario where no code gate exists
metadata:
  type: project
---

`helm-charts/README.md` §2 is the only instruction set for creating the 11-key prod
credentials Secret. Re-reviewed 2026-08-02 (issue #124 rewrite: one generator block,
`read -r -s` for the single external value). These are **measured**, not reasoned —
do not re-derive them, but do re-check if the block's shape changes.

**Measured shell / kubectl semantics** (bash 3.2 macOS, kubectl v1.33.2):
- Heredoc expansion is **single-pass**: `$(touch /tmp/X)` or backticks *inside* an
  interpolated `${VAR}` in an unquoted `<<EOF` are written verbatim and never execute.
  `--from-env-file` never re-parses either. So unquoted `<<EOF` (required, so the
  `$(openssl …)` substitutions run) is safe for an operator-typed value.
- `read -r -s -p "" VAR` (one variable, default IFS) **trims leading/trailing spaces
  and tabs**. `IFS= read -r` would preserve them. Trimming is what you want for a
  pasted credential; the `IFS=` form would be the wrong "fix" here.
- kubectl env-file parser: splits on the **first** `=` (so `=` in a value survives);
  `#` is a comment **only at line start**; leading whitespace is stripped from the
  *line*, not the value; trailing whitespace is preserved (except `\r`); **quotes are
  NOT stripped** — a hand-edited `KEY='v'` stores the quotes; an empty value creates
  the key with `""`, which `_check_airflow_credentials_prod`'s `[[ -z ]]` then rejects.
- `cat > file` under the default umask 0022 creates **0644**; `mktemp` creates 0600.
  A post-hoc `chmod 600` therefore leaves a world-readable window, and the fixed path
  `/tmp/dataspoke-secrets.env` is also pre-creatable by a co-tenant — the exact actor
  §2's own prose names. `install.sh` uses `mktemp` everywhere (:229/933/976/1153).
- `<your-namespace>` in a bash-fenced block is a **syntax error** (`< ns >` + newline),
  not a failed redirect: the `kubectl create secret` line never runs, while the
  unconditional `rm` on the next line still deletes the file.
- `openssl rand -hex 32` → 64 hex → decodes as base64 to **48** bytes (hence Fernet
  rejects it); `base64.urlsafe_b64encode(secrets.token_bytes(32))` → 44 chars, matches
  `^[A-Za-z0-9_-]{43}=$`, decodes to exactly 32.

**Airflow's two keys are decorative under the chart default.** `values.yaml`
`core.simple_auth_manager_all_admins: "True"` (prod default, apiServer ingress enabled
on `airflow.<domain>`) means — verified in the pinned airflow package —
`simple/services/login.py::create_token` early-returns an **anonymous ADMIN JWT**
before ever looking at the body, and `simple/middleware.py::SimpleAllAdminMiddleware`
appends an admin `Authorization` header to *every* request. So anyone who reaches
`airflow.<domain>` is an Airflow admin; `DATASPOKE_AIRFLOW_USER`/`_PASSWORD` entropy
protects nothing, and the pre-flight's `!= admin` gate ("reduce brute-force exposure")
guards a login that does not exist. Any §2 text presenting those two as a protective
credential pair is missing a disclosure. `values.yaml`'s own comment already states
this — the runbook does not.

**The Fernet scenario with no code gate:** fresh credentials Secret + retained Postgres
PVC. `_check_airflow_credentials_prod`'s retained-PVC WARNING lives only in the
*missing-key* branch, so a present, well-shaped key skips it; `_ensure_airflow_fernet_
secret`'s abort-on-mismatch needs a live comparison source, and prod `uninstall.sh`
deletes `dataspoke-airflow-metadata-encryption-key` unconditionally. README prose is
the only guard. A recipe that auto-generates the key makes silent orphaning the default
path. The cheap fix is to lift install.sh's own detector into the doc:
`kubectl get pvc data-dataspoke-postgresql-0 -n <ns>`.

**Also measured:** `_derive_airflow_metadata_secret` percent-encodes both DSN halves
via `_url_encode` (`quote(safe="")`), so "hex keeps the DSN from breaking" is
defence-in-depth, not the control — see [[credential-uri-escaping-boundary]].

**How to apply:** on any §2 diff, re-check the three leak sinks (argv, history, temp
file mode+lifetime) and re-read the two claims above rather than the prose. Related:
[[operator-runbook-is-credential-surface]], [[credentials-secret-contract-key-addition]].
