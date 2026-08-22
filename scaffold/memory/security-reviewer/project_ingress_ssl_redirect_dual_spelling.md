---
name: ingress-ssl-redirect-dual-spelling
description: ssl-redirect is cert-gated on BOTH nginx controllers; DATASPOKE_KUBE_INGRESS_SCHEME is NOT a sound proxy for "this host terminates TLS", and the scheme-derived rework proved it by render
metadata:
  type: project
---

`DATASPOKE_KUBE_INGRESS_CLASS` picks a class *name* only, so in shared mode
either nginx controller may serve it, and the two read disjoint annotation
namespaces (`nginx.ingress.kubernetes.io/*` community, `nginx.org/*` NGINX
Inc.). **The chart's reach differs by knob: body size is dual-spelled;
`ssl-redirect` is community-spelled only**, so on an NGINX Inc. controller the
redirect follows that controller's default rather than the chart's setting.
Deriving the redirect deliberately is tracked as its own issue — two attempts
inside the #121 run were rejected on review, so read the facts below before
proposing a third.

1. **`ssl-redirect` is a no-op unless the server has a certificate — on both
   controllers.** Community gates the 301 on
   `and (not (empty $server.SSLCert.PemFileName)) $location.Rewrite.SSLRedirect`;
   nginx.org nests `SSLRedirect` inside `{{if $server.SSL}}`. NGINX Inc's
   default is **true**, so a mirrored `nginx.org/ssl-redirect: "false"` is an
   explicit opt-out, not a no-op, on any host that carries `tls:`.
   `force-ssl-redirect` / `redirect-to-https` are the no-cert knobs; DataSpoke
   sets neither. `nginx.org/proxy-body-size` does not exist — the NGINX Inc
   spelling is `nginx.org/client-max-body-size` (renamed repo-wide 2026-08).

2. **The two env vars are documented as ORTHOGONAL and they are.**
   `helm-charts/.env.dev.example` says so verbatim:
   `DATASPOKE_KUBE_INGRESS_SCHEME` governs *the URLs the install builds*;
   `DATASPOKE_KUBE_INGRESS_TLS_SECRET` governs *whether the Ingresses carry
   TLS*. So deriving `ssl-redirect` from the **scheme** is deriving a
   transport-security control from the wrong signal. Two reachable
   combinations put `"false"` on both spellings onto a TLS-terminating host
   (verified by `helm template`, not by reading):
   - prod: chart `values.yaml` and `values-prod.example.yaml` ship
     cert-manager `tls:` on the API and Airflow hosts **unconditionally**;
     `ingress_scheme()` silently defaults to `http`, so an operator who omits
     `DATASPOKE_KUBE_INGRESS_SCHEME=https` gets `tls: dataspoke-api-tls` +
     `ssl-redirect: "false"`.
   - dev: `DATASPOKE_KUBE_INGRESS_TLS_SECRET=<x>` + scheme `http`.
   The sound condition is `scheme == https OR ingress_tls_secret() != ""`
   (dev) / presence of a `tls:` block (prod).

3. **Where the certs are** (the map that decides inert vs real downgrade):
   prod `values.yaml` + `values-prod.example.yaml` — api, frontend AND airflow
   all carry `tls:`; dev `values-dev.yaml` api/airflow + frontend subchart —
   TLS only when `DATASPOKE_KUBE_INGRESS_TLS_SECRET` is set; langfuse
   (`tls.enabled: false`), datahub-frontend, `gms-ingress.yaml` — no TLS block.

**Enforcement asymmetry worth citing.** `DATASPOKE_KUBE_INGRESS_CLASS` is
documented REQUIRED-in-prod with no default *and* verified against the cluster
in pre-flight, precisely because it decides exposure.
`DATASPOKE_KUBE_INGRESS_SCHEME` has a silent `http` default and no prod gate,
which is why it cannot carry a transport-security control: any redirect
derivation built on it inherits that default. It governs the GMS manifest's
substituted value only.

**Emission-site inventory (check every one on any future ssl-redirect diff).**
`install.sh` `_frontend_helm_set_args`, `_helm_upgrade_dataspoke_dev`, the prod
`helm upgrade`, **the `--components frontend` dev fast path's own inline
`helm upgrade`** (this one was missed in the first rework — it is a
full-release upgrade that re-renders the API and Airflow ingresses and reverts
them to the values-file literal), `bin/dev-peripherals/datahub.sh`
(datahub-frontend + the GMS `sed` manifest), `bin/dev-peripherals/langfuse.sh`.
The prod frontend ingress is set by nobody — fail-secure, but inconsistent.

**Mechanics verified, do not re-litigate.** `--set-string
a.b.annotations.nginx\.org/ssl-redirect=v` renders correctly on all four charts
(dataspoke api, frontend subchart, apache-airflow apiServer, langfuse wrapper,
datahub-frontend) — confirmed by `helm template`. `\.` survives an unquoted
heredoc and `while IFS= read -r`. Top-level `[[ c ]] && V=x` does NOT trip
`set -e`; the same idiom as a function's **last** command makes the function
return 1.

Related: [[datahub-gms-public-virtual-host]],
[[env-to-sed-helm-interpolation-boundary]],
[[install-sh-preflight-gate-mechanics]],
[[operator-runbook-is-credential-surface]].

`helm-charts/README.md` is in the sensitive-path glob list — it is the operator
runbook, carrying the credential-bootstrap recipe and the transport-security
claims for every published host, so an error there is a real prod
misconfiguration even though no code changes.
