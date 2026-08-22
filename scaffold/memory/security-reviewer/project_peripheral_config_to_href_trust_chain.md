---
name: peripheral-config-to-href-trust-chain
description: peripheral_config DB values (datahub frontend_url, langfuse host/project_id) are rendered directly into browser <a href> — validate at both the PATCH write boundary and the read boundary
metadata:
  type: project
---

`peripheral_config.settings` (JSONB) values now reach the browser as raw `href`
attributes: `GET /spoke/common/peripheral-links` (any authenticated role) serves
`datahub_url` / `langfuse_url` / `langfuse_project_id`, and the frontend renders
them via `components/app-shell.tsx` (`href={langfuseUrl}`, `href={datahubUrl}/login`),
`components/datahub-dataset-link.tsx`, and `components/ontogen/evidence-link.tsx`.

**Why:** before issue #66 these links came from chart env values (a trusted,
non-API plane). Promoting them to a DB plane writable through
`PATCH /admin/peripherals/*` (Admin) and `PATCH /internal/admin/peripherals/*`
(internal token) makes them attacker-influenceable config that crosses into a
rendering context — an Admin-to-Reader privilege boundary, since Admin otherwise
cannot execute script in another user's browser.

**How to apply:** any diff touching these fields must be checked for scheme
validation on *both* sides. Write side: `DatahubPeripheralPatchRequest.frontend_url`
has `pattern=r"^$|^https?://"`; `LangfusePeripheralPatchRequest.host` and
`project_id` have length limits only. Read side: `PeripheralLinksResponse` fields
are bare `str` — values written by direct SQL or by an older schema are served
verbatim. React 19 throws on `javascript:` hrefs, but that is a client-library
accident, not a control this repo owns. See [[pydantic-v2-pattern-anchoring]].
