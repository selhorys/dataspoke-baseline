---
name: data-hub-action-button-scoping
description: On /data/[urn] the Validation panel (rendered first, open) and MetaGen panel both expose Create/Edit/Save/Delete; E2E gestures MUST scope by section or .first() hits the wrong panel
metadata:
  type: project
---

The unified hub `app/(app)/data/[urn]/page.tsx` renders CollapsiblePanels in this DOM order,
both `defaultOpen=true`: **Validation** (`ValidationDataPanel`) THEN **MetaGen**
(`MetagenDataPanel`) THEN Events. Both panels expose the SAME action-button labels — `Create`
(empty-state), `Edit`, `Delete`, `Save`, `Cancel` — in their header-right clusters.

Consequence for E2E: an UNSCOPED `page.getByRole("button",{name:"Create"|"Edit"|"Save"|"Delete"}).first()`
resolves to the **Validation** panel's button (first in the DOM), NOT MetaGen's. eu_profiles /
orders.events carry no validation conf in UC4, so the Validation panel shows its own `Create`
empty-state alongside MetaGen's `Create` — a `.first()` Create click lands on Validation and the
MetaGen boundary form never mounts (`#boundary-is-enabled` never appears -> the gesture's assertion
times out on a CORRECT impl = false negative).

**Why:** pre-refactor the boundary lived on the standalone `/metagen/data/[urn]` page where no
Validation Create/Edit existed, so `.first()` was safe. The merge to `/data/[urn]` introduced the
collision; migrated helpers that kept `.first()` are stale (same class as
[[data-hub-panel-no-section-headings]] but for action buttons, not headings).

**How to apply:** require every per-panel action gesture on `/data/[urn]` to scope to its section
first, e.g. `page.locator("section").filter({ has: page.getByRole("button",{name:"MetaGen",exact:true}) })`
(the `metagenPanel(page)` / `validationPanel` helpers). uc2 scopes correctly; watch for uc4's
`enterBoundaryEditMode` (or any helper) using unscoped `.first()` for the Create/Edit clicks while
only scoping Save/Delete -- that is the exact gap to flag.
