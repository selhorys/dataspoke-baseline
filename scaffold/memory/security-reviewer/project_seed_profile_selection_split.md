---
name: seed-profile-selection-split
description: The two post-install seed scripts pick dev-vs-prod independently, so one env file can be prod to one and dev to the other — plus the `set -a` fan-out that ships every credential to kubectl
metadata:
  type: project
---

`helm-charts/bin/post-install/seed-peripheral-config.sh` and `seed-runtime-config.sh`
each decide their own profile from *different* variables, with no shared marker and no
cross-check:

- peripheral: prod iff `DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL`; dev iff `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE`
- runtime: prod iff any `DATASPOKE_PROD_LLM_*`; dev iff any `DATASPOKE_DEV_LLM_*`

**Why it matters:** the dev branch of `seed-runtime-config.sh` PATCHes all four
`stub_*` flags true. Measured on 2026-08-05: an env file holding
`DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL` (so the peripheral script calls it prod),
no `DATASPOKE_PROD_LLM_*` (the LLM block is a *supported deferral*), and one leftover
`DATASPOKE_DEV_LLM_API_KEY` takes the dev branch and puts production on stub Redis /
LLM / pgvector / notifications, answering `200`. The tell in the output is a
"skipping LLM provider/model PATCH" line immediately followed by "Stub service flags
seeded" — the branch was entered on an API key alone. The symmetric case exists on the
peripheral side: a leftover `DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE` seeds dev in-cluster
DNS and the dev ingress domain into a prod `peripheral_config` (see
[[peripheral-config-to-href-trust-chain]]).

Two secondary facts measured in the same pass:

- `set -a; source "$ENV_FILE"; set +a` exports the **whole** env file, so `kubectl`
  and its client-go exec credential plugin (`gke-gcloud-auth-plugin`, `aws eks
  get-token`, `kubelogin` — separate binaries) inherit every credential, not just the
  ones the payload builder was asked for. At the pre-`set -a` shape they inherited
  none. Scope it by exporting only the named vars inside `build_payload`'s own
  command-substitution subshell.
- `API_INTERNAL_REQUEST_BODY_STDIN=1` (`bin/lib/helpers.sh`) does work under macOS
  `/bin/bash` 3.2: argv carries `-`, the body rides `kubectl exec -i`. It also closes
  the *kube-apiserver audit log* channel, which the comments do not mention — argv mode
  puts the body in the exec subresource query string.

**How to apply:** any diff to the seed scripts, `.env.prod.example`, or the prod
runbook — check that the profile is one decision shared by both scripts, keyed on
something a prod file cannot carry, not per-script and per-block. A spec sentence of
the form "the prod path sets no `stub_*` flag" is a claim about a *branch*, not about
`ENV_FILE=.env.prod`; operators read it as the latter.
