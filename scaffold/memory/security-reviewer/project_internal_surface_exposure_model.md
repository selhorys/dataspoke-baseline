---
name: internal-surface-exposure-model
description: /internal/* is a top-level router (not under /api/v1) whose only control is X-Internal-Token; the seed path now calls it in-pod via api_internal_request, but the chart default still publishes it at host root and no gate stops that
metadata:
  type: project
---

`/internal/admin` (`src/api/routers/admin.py:92`) and `/internal/activities`
(`src/api/routers/internal/activities.py:44`) mount at the **app root**, not under
`API_PREFIX = /api/v1`. That single fact decides every ingress-exposure question:
a path list of `/api/v1` + `/health` + `/ready` excludes `/internal/*` outright,
and no dot-segment or `%2e%2e` trick reverses it (nginx normalizes before
location matching, and Starlette does no dot-segment resolution, so a raw URI
cannot normalize *into* `/api/v1…` while resolving *to* `/internal/…`).

**Two callers, two transports** (issue #130 fix, 2026-08):
- Airflow → `/internal/activities/*` over cluster DNS
  (`config.airflow.callbackBaseUrl` defaults to `http://dataspoke-api:8002`;
  the prod example overlay does not override it).
- The installer → `api_internal_request <ns> <METHOD> <path> <body>` in
  `bin/lib/helpers.sh`: `kubectl exec deploy/dataspoke-api -c api -- python3 -c
  <static script> METHOD PATH BODY` against `http://127.0.0.1:8002`. Argv, not
  shell — `kubectl exec` uses the exec subresource command array. The token is
  read from the pod's own env, so it no longer reaches the operator machine or
  host `ps` on this path. **But dev still exports it**: `install.sh:2135`
  `_sync_env_from_secret … DATASPOKE_DEV_INTERNAL_TOKEN` writes it to `.env.dev`.

**The residual, and why it keeps being mis-stated as fixed.** Only
`values-prod.example.yaml` got the narrow path list. Three live routes to a
published `/internal/*` remain: (a) the chart's own
`helm-charts/dataspoke/values.yaml` API ingress default is `path: /`;
(b) `install.sh` prod runs with `-f values.yaml` **alone** when `--values` is
omitted — it only *info*-logs that an overlay is "typically" required, and
ingress-nginx routes by Host header, so the untouched
`api.dataspoke.example.com` rule is hittable at the controller IP with a forged
Host; (c) dev pins `path=/` at `install.sh:1859`, which is internet-facing in
`shared` ingress mode. There is no pre-flight gate on the rendered Ingress even
though `_check_airflow_credentials_prod` is the established precedent for
exactly that kind of prod refusal.

**Why it matters:** `/internal/admin/bootstrap` seeds
`dataspoke@dataspoke.local / dataspoke` (published in this public repo);
`/internal/admin/peripherals/*` writes the DataHub PAT and Langfuse secret key;
`/internal/admin/conf` writes the LLM API key. `require_internal_token`
(`src/api/auth/internal.py`) is the only control — constant-time compare, 503
`INTERNAL_AUTH_NOT_CONFIGURED` on a blank configured token, 401 otherwise, so it
does fail closed; but prod pre-flight checks that key's **presence, not
emptiness**, so a `DATASPOKE_INTERNAL_TOKEN: ""` Secret reaches that 503.

**How to apply:** for any diff touching an API ingress path list, chart ingress
defaults, or a caller of `/internal/*`, re-derive exposure from the router mount
points above rather than from the prose — `spec/API.md:909` still claims
`/internal/*` "are not exposed through ingress", which is false against the
host-root default. See [[operator-runbook-is-credential-surface]] for the
doc-vs-code rule and [[datahub-gms-public-virtual-host]] for the analogous
"public host, app-level token is the only control" shape.

**Sensitive-path glob gaps noticed here** (report, never self-edit):
`helm-charts/README.md`, `helm-charts/.env.*.example`, and
`spec/feature/HELM_CHART.md` are the operator-facing claim set for this surface
and match no glob.
