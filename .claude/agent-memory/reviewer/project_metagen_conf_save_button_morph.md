---
name: metagen-conf-save-button-morph
description: "metagen conf detail keeps Save INSIDE the form (safe pattern); a header-Save refactor was attempted and reverted. Watch for re-introduction."
metadata:
  type: project
---

The metagen conf detail page (`src/frontend/app/(app)/metagen/conf/[id]/page.tsx`)
keeps the Save button **inside** `<MetagenConfForm>`; the header slot toggles only
`editing ? Cancel : Edit` (both `type="button"`, distinct keys `conf-cancel`/`conf-edit`)
alongside Run/Delete. This is the documented-safe arrangement and does NOT trip the
submit-on-Edit hazard.

**History (resolved):** A fix pass once moved Save into the header conditional slot
with `type="submit" form={CONF_FORM_ID}` and removed the in-form button — exactly the
pattern user-memory `project_frontend_button_submit_morph` warns about (React morphs the
reused `<button>` on Edit→Save, browser auto-submits, Edit silently PUTs). The reviewer
flagged it; the generator **reverted the whole refactor** back to the in-form Save.

**Why it matters:** If a future change again hoists Save into the header slot, the hazard
returns. jsdom can't catch it (no default-action phase) — it needs the real-browser
Playwright repro `tests/e2e/ground/metagen/conf-edit-no-submit.spec.ts`.

**How to apply:** When reviewing metagen conf header-button changes, confirm Save stays
inside the form. If a header Save is reintroduced, require distinct stable keys
(`conf-edit`/`conf-save`) AND that the Playwright Edit-doesn't-submit E2E covers it.
