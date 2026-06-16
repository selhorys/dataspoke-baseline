---
name: dataspoke-metagen
description: Answer questions about DataSpoke Metadata Generation (UC4) on a deployed instance and make read calls against its public API. Stub — knows the route prefix and points at the deployment's live OpenAPI reference; not yet a full authoring/review workflow. Use for "what does metagen expose" or basic reads.
argument-hint: "[question]"
allowed-tools: Read, Bash(dataspoke-api *), WebFetch
---

## Status: stub (TBD)

Metadata Generation has no dedicated end-user workflow yet. This skill answers questions about the
feature's public surface and makes read calls; it does not run generation or review candidates. If
`dataspoke-api` reports no access, send the user to `/dataspoke:dataspoke-access`.

- **Route prefixes**: `/api/v1/spoke/metagen/…` (global conf, `method/run`, `event`, items/
  candidates) and `/api/v1/spoke/common/data/{urn}/attr/metagen/…` (per-dataset boundary, items,
  candidate review).
- **Authoritative contract**: the deployment's own OpenAPI — WebFetch the `redoc_url` from
  `~/.dataspoke/config.json`, or `dataspoke-api GET /openapi.json`.
- **Reads**, e.g.: `dataspoke-api GET /spoke/metagen/item`.

Do not invent behavior beyond what the API exposes. Promotion to a full skill awaits a concrete
end-user scenario.
