---
name: dataspoke-ontogen
description: Answer questions about DataSpoke Ontology Generation (UC3) on a deployed instance and make read calls against its public API. Stub — knows the route prefix and points at the deployment's live OpenAPI reference; not yet a full authoring workflow. Use for "what does ontogen expose" or basic reads.
argument-hint: "[question]"
allowed-tools: Read, Bash(dataspoke-api *), WebFetch
---

## Status: stub (TBD)

Ontology Generation has no dedicated end-user workflow yet. This skill answers questions about the
feature's public surface and makes read calls; it does not author or run inference. If
`dataspoke-api` reports no access, send the user to `/dataspoke:dataspoke-access`.

- **Route prefix**: `/api/v1/spoke/ontogen/…` (conf, seeds, `method/run`, `event`, and node/edge/
  triple results with review).
- **Authoritative contract**: the deployment's own OpenAPI — WebFetch the `redoc_url` from
  `~/.dataspoke/config.json`, or `dataspoke-api GET /openapi.json`.
- **Reads**, e.g.: `dataspoke-api GET /spoke/ontogen/result/triple`.

Do not invent behavior beyond what the API exposes. Promotion to a full skill awaits a concrete
end-user scenario.
