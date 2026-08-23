---
name: env-to-sed-helm-interpolation-boundary
description: helm-charts .env values flow unvalidated into sed replacements, helm --set tokens and kubectl apply stdin; the repo validates some (ingress_tls_secret) and not others (ingress_class, ingress_domain)
metadata:
  type: project
---

`helm-charts/.env.{dev,prod}` values are an install-time **trust boundary**, not
inert config. Every `DATASPOKE_KUBE_*` value reaches at least one of:

- a `sed` s/// **replacement** (`bin/dev-peripherals/datahub.sh` renders
  `dev-peripherals/datahub/gms-ingress.yaml` and pipes it to `kubectl apply -f -`;
  `bin/dev-peripherals/nginx-ingress.sh` renders the nginx-ingress values)
- a `helm --set` / `--set-string` **token** (comma = next assignment, dot = path
  separator, so one value can inject unrelated chart values)
- a bash **args array built by a heredoc** (`_frontend_helm_set_args` in
  `bin/install.sh` emits one token per line and callers `while IFS= read -r`, so an
  embedded newline becomes a standalone `helm` flag)

**Why:** the repo already knows this — `ingress_tls_secret()` in
`bin/lib/helpers.sh` validates DNS-1123 with the comment *"it is interpolated into
`helm --set` tokens, so an unvalidated value could inject extra flags via a comma or
newline."* But `ingress_class()` (added for issue #80) and
`DATASPOKE_KUBE_INGRESS_DOMAIN` have no such guard while reaching strictly more
sinks, including the `sed` → `kubectl apply` path where an injected `s///` command
can append arbitrary YAML documents applied with the installer's cluster creds.
Note `datahub.sh` uses the `/` sed delimiter while `nginx-ingress.sh` uses `|`.

**How to apply:** when any `bin/` script gains a new `${DATASPOKE_KUBE_*}`
interpolation, check which of the three sinks it reaches and whether a validating
helper wraps it. Absence of validation is a finding even though `.env` is
operator-authored and gitignored — the in-repo precedent makes it an inconsistency,
not a judgment call. Related: [[operator-runbook-is-credential-surface]].

**Sensitive-path glob status — closed.** `helm-charts/bin/lib/helpers.sh`,
`helm-charts/dev-peripherals/**/*.yaml`, `helm-charts/bin/build-image.sh`, and
`helm-charts/dataspoke/templates/**`+`subcharts/**/templates/**` are all now in
`scaffold/roles/security-reviewer.md`'s glob list (see
[[image-digest-stamping-attestation]] for when the last two landed).
