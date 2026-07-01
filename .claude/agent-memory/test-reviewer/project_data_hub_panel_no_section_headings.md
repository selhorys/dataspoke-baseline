---
name: data-hub-panel-no-section-headings
description: On /data/[urn] the ValidationDataPanel renders NO attr/validation/conf or event/validation headings; events fold into the unified EventsPanel
metadata:
  type: project
---

The unified per-dataset hub `/data/[urn]` (FRONTEND_BASIC.md §Per-dataset page) composes feature
BODIES, not the old standalone detail pages. The extracted bodies dropped the per-feature event
sections and some section headings:

- `ValidationDataPanel` (src/frontend/components/validation/validation-data-panel.tsx) renders the
  conf read-only/edit/create/undelete view + two CHART headings only:
  `Quality Score (attr/validation/result)` and `Variables (attr/validation/result)`. It does NOT
  render `attr/validation/conf` nor `event/validation` headings — those existed only on the old
  `/validation/data/[urn]` page (now a redirect).
- `MetagenDataPanel` renders `<h3>` headings **`Boundary Config`** and **`Generated Items`**
  (NOT `attr/metagen/boundary` / `attr/metagen/item` — that was an earlier impl; current impl
  as of 2026-07 uses the spec strings, FRONTEND_METAGEN.md L139-140), plus per-kind
  CollapsiblePanels titled `dataset.description` / `column.description`; it does NOT render
  `event/metagen`. (`ValidationDataPanel` correspondingly renders a `<h3>Config</h3>` heading.)
- Per-feature event lists all fold into the shared `EventsPanel`/`EventsTable` under the "Events"
  CollapsiblePanel; `EventsTable` renders each row's `event_type` as text, toggled by the
  EventMajorTypeFilter (default all checked). Assert event rows there, not in a feature panel.

**Why:** E2E specs migrated from `/{feature}/data/[urn]` to `/data/[urn]` keep stale
`getByRole("heading", {name:"attr/validation/conf"|"event/validation"})` assertions that pass on the
old page but FAIL on the unified hub. (Observed: uc2-01 steps 4/5/6 carried these over; COVERAGE.md +
the test docstring already said "validation events in unified Events panel" but the code did not.)

**How to apply:** when reviewing tests that navigate to `/data/[urn]`, reject heading assertions for
`attr/validation/conf` / `event/validation` / `event/metagen`. Validation-conf presence should be
asserted via the read-only content (description text, variable name/badge) or the chart headings;
per-dataset events via the unified Events panel (toggle the "Events" CollapsiblePanel button by
aria-expanded, then assert the `event_type` text). CollapsiblePanel header = `<button aria-expanded>`;
EventMajorTypeFilter checkboxes = Radix `role="checkbox"` with `aria-label`=major type + `aria-checked`.
