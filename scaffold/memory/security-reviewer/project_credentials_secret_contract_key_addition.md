---
name: credentials-secret-contract-key-addition
description: Adding a key to the dataspoke-secrets contract has four blind spots on already-installed clusters (all four now closed in-tree — keep the checklist, not the findings), plus why dev-teardown Fernet stranding is severity-subsumed by the Postgres password regen
metadata:
  type: project
---

`_ensure_dataspoke_secrets` in `helm-charts/bin/install.sh` early-returns when the
Secret exists. So when a diff adds an Nth key to the consolidated credentials
contract, four things are true on every cluster installed before that diff, none
visible from reading the diff. **All four were closed during the #111 Fernet work
(2026-07-30) — keep this as the checklist for the next key addition, not as a list
of open findings.**

1. **The new key is never added.** Consumers read it, find empty, hard-error. The
   install fails closed — good — but the operator's only path is to delete
   `dataspoke-secrets`, regenerating *all* keys including
   `DATASPOKE_POSTGRES_PASSWORD` and stranding retained PVCs. An unhelpful
   missing-key message is a credential-destruction funnel, not a nuisance.
   *(Closed by `_ensure_fernet_key_joins_credentials_secret`, a dev-only
   adopt-then-generate patch of the existing Secret.)*
2. **"Adopt the live key before generating" is unreachable** if it reads only the
   *new* projection name. A rename means the new name does not exist yet, so
   adoption no-ops on the one install that actually changes the key. Check which
   Secret name holds the live value *today*.
3. **Teardown deletes the projection**, which is what the abort-on-mismatch guard
   compares against. Prose asserting "the next install aborts rather than silently
   re-projecting" is false across uninstall->reinstall, true only across repeated
   installs. *(Now disclosed honestly in `helm-charts/README.md` §Prod: what
   survives an uninstall — "two different guarantees".)*
4. **Guards that gate on Secret *existence* rather than on the projected *value*.**
   Both the skip branch and the comparison-*source* fallback must test the decoded
   value: `[[ -z "${existing}" ]] && kubectl get secret <legacy>`, never
   `elif kubectl get secret <legacy>`. Otherwise a new-name Secret existing with an
   empty `fernet-key` shadows a legacy Secret holding the real key and the
   abort-on-mismatch never fires. On any projection guard, ask what an
   *empty-valued* Secret does, not just a missing one.

Also check the two dev fast paths (`--components api`, `--components frontend`):
both run a **full-release** helm upgrade that pins projection names, so each must
run the `_ensure_*` secret steps itself. *(Closed — the ensure block moved inside
`_helm_upgrade_dataspoke_dev` and is duplicated inline in the frontend path.)*

**Dev-teardown severity calibration.** `bin/uninstall.sh --profile dev` deletes
`dataspoke-secrets` while the PVC prompt defaults to No (and is skipped entirely
under `--no-question`). The next install's `_ensure_dataspoke_secrets` regenerates
`DATASPOKE_POSTGRES_PASSWORD` with **no adopt path** — bitnami postgres skips
initdb on an existing PGDATA, so the retained PVC keeps the *old* password and
nothing can authenticate to it. A retained dev PVC is therefore already unusable
before Fernet enters the picture: rate Fernet-stranding findings on the *dev* path
as low/subsumed, and reserve real severity for the prod path, where the credentials
Secret is operator-owned and survives.

**Why:** found reviewing the #111 Fernet-key work — the fix for a silent-key-rotation
data-loss bug initially reproduced that exact bug on the single transition install.

**How to apply:** for any diff that changes the credentials-Secret key count, trace
the already-installed-cluster path explicitly (existing Secret + existing PVCs +
legacy projection name), not just the greenfield path the generator verified with
`helm template`. Related: [[operator-runbook-is-credential-surface]],
[[env-to-sed-helm-interpolation-boundary]].

**Sensitive-path glob gap — closed for the scripts.** `helm-charts/bin/install.sh` and
`helm-charts/bin/uninstall.sh` are now both in `scaffold/roles/security-reviewer.md`'s
glob list alongside `bin/post-install/**`, `bin/dev-peripherals/**`, and
`bin/lib/helpers.sh`. **`.gitignore` remains unlisted** — still the control that keeps
`.env.*` credentials out of history, and still worth proposing.
