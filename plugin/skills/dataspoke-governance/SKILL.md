---
name: dataspoke-governance
description: Answer questions about DataSpoke Governance metrics (UC5) on a deployed instance and make read calls against its public API. Stub — knows the route prefix and points at the deployment's live OpenAPI reference; not yet a full metric-authoring workflow. Use for "what does governance expose" or basic reads.
argument-hint: "[question]"
allowed-tools: Read, Bash(dataspoke-api *), WebFetch
---

## Status: stub (TBD)

Governance has no dedicated end-user workflow yet. This skill answers questions about the feature's
public surface and makes read calls; it does not author metrics or trigger measurement. If
`dataspoke-api` reports no access, send the user to `/dataspoke:dataspoke-access`.

- **Route prefix**: `/api/v1/spoke/governance/metric/…` (metric CRUD, `method/run`, `attr/result`
  timeseries, `event`). Built-in metric types include `ingestion-freshness`, `validation-score`,
  `doc-health`.
- **Authoritative contract**: the deployment's own OpenAPI — WebFetch the `redoc_url` from
  `~/.dataspoke/config.json`, or `dataspoke-api GET /openapi.json`.
- **Reads**, e.g.: `dataspoke-api GET /spoke/governance/metric`.

Do not invent behavior beyond what the API exposes. Promotion to a full skill awaits a concrete
end-user scenario.
