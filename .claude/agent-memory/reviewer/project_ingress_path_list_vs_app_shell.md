---
name: ingress-path-list-vs-app-shell
description: Narrowing the API ingress path list must be checked against app-shell.tsx's four infra links — the "API docs" item is apiBaseUrl + /redoc and dies silently; /openapi.json does NOT describe /internal
metadata:
  type: project
---

The public surface of the DataSpoke API is wider than `/api/v1`. Before
accepting any narrowed ingress path list, check these four facts:

- `API_PREFIX = "/api/v1"`; `docs_url=None`, `redoc_url="/redoc"`, and
  `openapi_url` is FastAPI's default `/openapi.json` (`src/api/main.py:58,372`).
- The health router mounts at bare `/health` and `/ready`
  (`src/api/routers/health.py`), outside `API_PREFIX`.
- `src/frontend/components/app-shell.tsx` renders four always-visible infra
  links — DataHub, Langfuse, Airflow, and **"API docs" →
  `${apiBaseUrl}/redoc`**. This is spec'd in `spec/feature/FRONTEND_BASIC.md`
  §infra links, not incidental. A path list without `/redoc` +
  `/openapi.json` leaves that nav item dead in the deployed UI, with no
  error anywhere in the install.
- Both internal routers are registered `include_in_schema=False`
  (`src/api/main.py:442-443`), so publishing `/openapi.json` does **not**
  disclose `/internal/*`. A comment justifying keeping ReDoc unpublished on
  "it describes the entire API surface" overstates the exposure.

`helm-charts/bin/health-check.sh` probes `${SCHEME}://api.${DOMAIN}/health`
through the ingress and hardcodes the `api.` host prefix, but it defaults to
`.env.dev` — the dev ingress keeps the chart's host-root `path: /`, so dev is
unaffected by prod-overlay path narrowing.

**Why:** `values-prod.example.yaml` narrowed the API ingress to
`/api/v1`, `/health`, `/ready` (closing #130's `/internal/*` exposure) with
`/redoc` + `/openapi.json` as commented opt-ins, and nothing in the overlay,
`helm-charts/README.md`, or `spec/feature/HELM_CHART.md` mentions the frontend
consequence.

**How to apply:** treat the ingress path list as an inter-component contract
with `app-shell.tsx`, not a pure security knob. Related:
[[display-url-guard-three-copies]].
