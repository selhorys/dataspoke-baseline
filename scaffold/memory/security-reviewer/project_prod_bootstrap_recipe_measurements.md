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

**Airflow's two keys WERE decorative — no longer, as of the issue #138 change.**
`dataspoke/values.yaml` now ships `core.simple_auth_manager_all_admins: "False"`
for prod (dev's `values-dev.yaml` still pins `"True"`), and install.sh seeds a
passwords file via a prod-only api-server init container, so
`DATASPOKE_AIRFLOW_{USER,PASSWORD}` are consulted at every Airflow login. The
old anonymous-admin reading still applies to **any overlay that sets all_admins
back on** — and to the True-equivalent spellings the pre-flight fails to
recognise. See [[airflow-extraenv-tpl-injection-surface]] for the measured
3.1.8 login behaviour, the accepted boolean spellings, and the template-
injection path that can re-enable the anonymous route from the Secret itself.
NOTE: the 3.2.0 `SimpleAllAdminMiddleware` referenced in the old text does not
exist at the pinned 3.1.8 — read the 3.1.8 wheel, not the uv cache.

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
