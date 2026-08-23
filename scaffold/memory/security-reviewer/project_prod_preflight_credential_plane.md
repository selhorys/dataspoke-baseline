---
name: prod-preflight-credential-plane
description: install-prod-preflight.sh resolves/generates/writes the eleven prod credentials — the controls it does and does not hold, and the two flags whose blast radius is wider than their name
metadata:
  type: project
---

`helm-charts/bin/install-prod-preflight.sh` (7 stages, populate third) is the
only script that mints prod credentials and creates the credentials Secret.

**Controls verified holding (don't re-litigate):** no `--from-literal` anywhere
on this path; the Secret comes from a `mktemp -t` file (measured mode 600 in the
user's private TMPDIR) reclaimed inline plus `trap ... EXIT INT TERM`; `printf`
is a bash builtin so no credential reaches argv; the kubectl-context compare
precedes stage-3 adopt; `NS` and `SECRET_NAME` both pass `assert_k8s_name`
before any kubectl argv; Fernet is `secrets.token_bytes(32)` urlsafe-b64 (not
`openssl rand -hex 32`); no `set -x`; the only cluster writes are
`kubectl create namespace` (flag-gated) and `kubectl create secret generic` —
no helm, no patch/apply/delete; `install.sh:_ensure_dataspoke_secrets` still
aborts rather than creating in prod.

**Wider than their name:**
- `--skip-secret` suppresses only stage 5. Stage 3 still **generates and writes
  all eleven credentials to `.env.prod`** — on the one path (ExternalSecrets /
  Vault) whose whole point is keeping them off the operator's disk, and whose
  real values will differ, so the next run reports eleven-key drift and stops.
- `--verify-only` also suppresses the `chmod 600` on the env file, so an audit
  of a world-readable `.env.prod` says nothing about its mode.

**Order hazard:** the Secret is created *then* `verify_credential_secret` runs,
so an env-file value that fails the content contract (hex Fernet key,
`placeholder-` OAuth secret, dev JWT default) is written into the cluster first
and the script then refuses to rewrite it — the operator must delete by hand.

**Glob gap — closed.** Both `helm-charts/bin/install-prod-preflight.sh` and
`helm-charts/bin/health-check.sh` are now listed in
`scaffold/roles/security-reviewer.md`'s sensitive-path globs (the authoritative copy;
`.claude/agents/security-reviewer.md` is now a thin binding). Related:
[[env-file-writer-source-execution]], [[operator-runbook-is-credential-surface]],
[[credentials-secret-contract-key-addition]].
